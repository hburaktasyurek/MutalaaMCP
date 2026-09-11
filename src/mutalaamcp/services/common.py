"""Ortak türlenmiş araç yanıt zarfları, önbellek anahtarları ve yeniden doğrulama yardımcıları."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import ValidationError, model_validator

from mutalaamcp.domain.errors import ErrorBody, ErrorCode, ErrorDetails, ErrorEnvelope
from mutalaamcp.domain.models import (
    AbsoluteUri,
    ConversionMethod,
    DecisionHit,
    DocumentIdStr,
    FrozenModel,
    PageInfo,
    RevalidationError,
    Sha256Hash,
    ToolWarning,
)
from mutalaamcp.net import (
    ResponseTooLarge,
    SafeHttpError,
    UnsafeUrlError,
    UpstreamNotFound,
    UpstreamRateLimited,
    UpstreamUnavailable,
)

SEARCH_CACHE_TTL = timedelta(hours=24)
STALE_CONTENT_MESSAGE = "Önbelleğe alınan içerik, üst kaynak yeniden doğrulaması kullanılamadığı için güncel değildir."


@dataclass(frozen=True, slots=True)
class InvalidParamsError(ValueError):
    """Pydantic ile doğal olarak ifade edilemeyen herkese açık girdi doğrulaması."""

    field_errors: Mapping[str, tuple[str, ...]]
    message: str = "İstek parametreleri geçersiz."

    def __post_init__(self) -> None:
        if not self.field_errors:
            raise ValueError("field_errors boş olmamalıdır")


@dataclass(frozen=True, slots=True)
class ChunkOutOfRangeError(ValueError):
    """İstenen Markdown sayfası mevcut değildir ve asla sınırlandırılmamalıdır."""

    page: int
    total_pages: int

    def __str__(self) -> str:
        return f"İstenen sayfa {self.page}, 1..{self.total_pages} aralığı dışındadır."


class _StaleAwareSuccess(FrozenModel):
    """Araç girdi şemasında ifade edilemeyen ortak güncel olmayan yanıt değişmezleri."""

    warnings: tuple[ToolWarning, ...] = ()
    fetched_at: datetime | None = None
    validated_at: datetime | None = None
    revalidation_error: RevalidationError | None = None

    @model_validator(mode="after")
    def _stale_fields_are_consistent(self) -> _StaleAwareSuccess:
        stale = any(warning.code == "stale_content" for warning in self.warnings)
        if stale:
            if (
                self.fetched_at is None
                or self.validated_at is None
                or self.revalidation_error is None
            ):
                raise ValueError(
                    "güncel olmayan içerik fetched_at, validated_at ve revalidation_error gerektirir"
                )
        elif self.revalidation_error is not None:
            raise ValueError(
                "revalidation_error yalnızca güncel olmayan içerik için geçerlidir"
            )
        return self


class DecisionSearchSuccess(_StaleAwareSuccess):
    """Herkese açık ``karar_ara`` işlemi için kesin başarı yanıt zarfı."""

    ok: Literal[True] = True
    hits: tuple[DecisionHit, ...]
    page: PageInfo
    expires_at: datetime | None = None


class DocumentSuccess(_StaleAwareSuccess):
    """Sayfalanmış ad alanlı bir belge için kesin başarı yanıt zarfı."""

    ok: Literal[True] = True
    id: DocumentIdStr
    source: Literal["bedesten", "anayasa"]
    source_url: AbsoluteUri
    markdown: str
    mime_type: str | None
    conversion: ConversionMethod
    content_hash: Sha256Hash
    page: PageInfo
    fetched_at: datetime
    validated_at: datetime
    expires_at: datetime | None


def canonical_parameter_hash(
    parameters: Mapping[str, Any],
    *,
    unordered_fields: Iterable[str] = (),
) -> str:
    """Ham sorgu metnini tutmadan kanonik istek parametrelerini karmalar."""
    unordered = frozenset(unordered_fields)
    canonical = _canonicalize(dict(parameters), unordered=unordered, key=None)
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def conditional_headers(etag: str | None, last_modified: str | None) -> dict[str, str]:
    """Yalnızca kaydedilmiş metaveriden koşullu yeniden doğrulama üst bilgileri oluşturur."""
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def error_envelope(
    error: Exception,
    *,
    source_url: str | None = None,
    upstream: Literal["bedesten", "anayasa"] | None = None,
    not_found_as_unavailable: bool = False,
) -> dict[str, Any]:
    """İç hataları sözleşmenin tek kanonik hata yanıt zarfına eşler."""
    if isinstance(error, ValidationError):
        error_messages: dict[str, list[str]] = {}
        for item in error.errors(include_url=False):
            location = item.get("loc", ())
            field = ".".join(str(part) for part in location) or "request"
            message = str(item.get("msg", "geçersiz değer"))
            context = item.get("ctx")
            cause = context.get("error") if isinstance(context, Mapping) else None
            if (
                item.get("type") == "value_error"
                and isinstance(context, Mapping)
                and isinstance(cause, ValueError)
                and message == f"Value error, {cause}"
            ):
                message = str(cause) or "geçersiz değer"
            error_messages.setdefault(field, []).append(message)
        return _error(
            ErrorCode.INVALID_PARAMS,
            "İstek parametreleri geçersiz.",
            field_errors={
                key: tuple(messages) for key, messages in error_messages.items()
            },
        )
    if isinstance(error, InvalidParamsError):
        return _error(
            ErrorCode.INVALID_PARAMS,
            _explained_error("İstek parametreleri geçersiz.", error.message),
            field_errors=dict(error.field_errors),
        )
    if isinstance(error, ChunkOutOfRangeError):
        return _error(
            ErrorCode.CHUNK_OUT_OF_RANGE,
            _explained_error("İstenen belge sayfası mevcut değil.", error),
        )
    code = getattr(error, "code", None)
    if code == ErrorCode.INVALID_PARAMS.value:
        raw_field_errors = getattr(error, "field_errors", {"request": (str(error),)})
        field_errors = {
            str(field): tuple(str(message) for message in messages)
            for field, messages in dict(raw_field_errors).items()
        }
        return _error(
            ErrorCode.INVALID_PARAMS,
            _explained_error("İstek parametreleri geçersiz.", error),
            field_errors=field_errors,
        )
    if code == ErrorCode.CHUNK_OUT_OF_RANGE.value:
        return _error(
            ErrorCode.CHUNK_OUT_OF_RANGE,
            _explained_error("İstenen belge sayfası mevcut değil.", error),
        )
    if isinstance(error, UpstreamRateLimited):
        return _error(
            ErrorCode.UPSTREAM_RATE_LIMITED,
            _explained_error("Üst kaynak istek hızını sınırladı.", error),
            retry_after=error.retry_after if error.retry_after is not None else 30.0,
            upstream=upstream,
        )
    if isinstance(error, UpstreamNotFound):
        message = (
            "Üst kaynak istenen belgeyi bulamadı."
            if not_found_as_unavailable
            else "İstenen belge üst kaynakta bulunamadı."
        )
        return _error(
            (
                ErrorCode.UPSTREAM_UNAVAILABLE
                if not_found_as_unavailable
                else ErrorCode.NOT_FOUND
            ),
            _explained_error(message, error),
            upstream=upstream,
        )
    if isinstance(error, ResponseTooLarge):
        return _error(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            _explained_error(
                "Üst kaynak yanıtı izin verilen boyut sınırını aştı.", error
            ),
            upstream=upstream,
        )
    if isinstance(error, UpstreamUnavailable):
        return _error(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            _explained_error("Üst kaynak şu anda kullanılamıyor.", error),
            upstream=upstream,
        )
    if isinstance(error, UnsafeUrlError):
        return _error(
            ErrorCode.NOT_CONFIGURED,
            _explained_error("Güvenli olmayan üst kaynak URL'si reddedildi.", error),
            upstream=upstream,
        )
    if isinstance(error, SafeHttpError):
        return _error(
            ErrorCode.NOT_CONFIGURED,
            _explained_error(
                "Üst kaynak HTTP isteği güvenli biçimde tamamlanamadı.", error
            ),
            upstream=upstream,
        )
    if getattr(error, "code", None) == ErrorCode.OCR_REQUIRED.value:
        return _error(
            ErrorCode.OCR_REQUIRED,
            _explained_error("Belgeyi dönüştürmek için yerel OCR gereklidir.", error),
            source_url=source_url or getattr(error, "source_url", None),
        )
    if getattr(error, "code", None) == ErrorCode.UNSUPPORTED_FORMAT.value:
        return _error(
            ErrorCode.UNSUPPORTED_FORMAT,
            _explained_error("Belge dönüştürülemedi.", error),
            source_url=source_url or getattr(error, "source_url", None),
        )
    return _error(
        ErrorCode.UPSTREAM_UNAVAILABLE,
        _explained_error("Üst kaynak işlemi başarısız oldu.", error),
        upstream=upstream,
    )


def stale_warning() -> ToolWarning:
    return ToolWarning(code="stale_content", message=STALE_CONTENT_MESSAGE)


def unavailable_revalidation_error(error: UpstreamUnavailable) -> RevalidationError:
    """Yalnızca uygun ve ulaşılamayan bir üst kaynak için güncel olmayan başarı hatası oluşturur."""
    if (
        not isinstance(error, UpstreamUnavailable)
        or not error.stale_eligible
        or (error.status_code is not None and error.status_code < 500)
    ):
        raise ValueError(
            "güncel olmayan yeniden doğrulama uygun, ulaşılamayan bir üst kaynak gerektirir"
        )
    return RevalidationError(
        code="upstream_unavailable",
        message=_explained_error("Üst kaynak şu anda kullanılamıyor.", error),
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


def _explained_error(explanation: str, error: Exception | str) -> str:
    """Her açık hata iletisini Türkçe bir açıklamayla başlatır."""
    detail = str(error).strip()
    if detail == explanation:
        return explanation
    return f"{explanation} Ayrıntı: {detail}" if detail else explanation


def _error(
    code: ErrorCode,
    message: str,
    *,
    field_errors: dict[str, tuple[str, ...]] | None = None,
    retry_after: float | None = None,
    source_url: str | None = None,
    upstream: Literal["bedesten", "anayasa"] | None = None,
) -> dict[str, Any]:
    details = ErrorDetails(
        field_errors=field_errors,
        retry_after=retry_after,
        source_url=source_url,
        upstream=upstream,
    )
    if details.model_dump() == {}:
        details = None  # type: ignore[assignment]
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message,
            retryable=code
            in {ErrorCode.UPSTREAM_RATE_LIMITED, ErrorCode.UPSTREAM_UNAVAILABLE},
            details=details,
        )
    )
    return envelope.model_dump(mode="json")


def _canonicalize(value: Any, *, unordered: frozenset[str], key: str | None) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if hasattr(value, "value") and value.__class__.__module__.startswith("mutalaamcp"):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(name): _canonicalize(item, unordered=unordered, key=str(name))
            for name, item in sorted(value.items())
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_canonicalize(item, unordered=unordered, key=None) for item in value]
        if key in unordered:
            return sorted(
                items,
                key=lambda item: json.dumps(
                    item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            )
        return items
    return value
