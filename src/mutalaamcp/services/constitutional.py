"""Önbellekli V1 Anayasa Mahkemesi arama ve belge işlemleri."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date as CalendarDate
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from mutalaamcp.cache.singleflight import SingleFlight
from mutalaamcp.cache.store import CacheRecord, CacheStore, Freshness
from mutalaamcp.conversion.documents import convert_document
from mutalaamcp.domain.articles import chunk_markdown
from mutalaamcp.domain.ids import AnayasaId, InvalidDocumentId, parse_document_id
from mutalaamcp.domain.models import (
    ConstitutionalHit,
    ConstitutionalKind,
    FrozenModel,
    PageInfo,
    RevalidationError,
)
from mutalaamcp.net import UpstreamNotFound, UpstreamUnavailable
from mutalaamcp.providers.anayasa.client import (
    INDIVIDUAL_ORIGIN,
    NORM_ORIGIN,
    SEARCH_PATH,
    AnayasaClient,
    AnayasaNotFound,
    AnayasaProtocolError,
    AnayasaRecord,
    AnayasaSearchPage,
)
from mutalaamcp.services.common import (
    SEARCH_CACHE_TTL,
    ChunkOutOfRangeError,
    DocumentSuccess,
    canonical_parameter_hash,
    error_envelope,
    unavailable_revalidation_error,
    utc_now,
)

_SEARCH_CACHE_NAMESPACE = "anayasa-search-v1"
_DOCUMENT_CACHE_NAMESPACE = "anayasa-document-v1"
_DOCUMENT_PAGE_MAX_CHARS = 12_000
_CITATION_PATTERN = r"^[1-9][0-9]{3}/[1-9][0-9]*$"
_QUERY_IDENTIFIER_WARNING = (
    "Kimlik filtrelemesi geçerli AYM SPA sorgusuna derlenir ve tam eşleşmeli bir "
    "üst kaynak kimlik filtresi değildir."
)
_STALE_MESSAGE = "Önbelleğe alınan içerik, üst kaynak yeniden doğrulaması kullanılamadığı için güncel değildir."


class ConstitutionalWarning(FrozenModel):
    """Anayasal karar araması uyarılarının söz dağarcığı."""

    code: Literal["stale_content", "query_backed_identifier_filter"]
    message: str = Field(min_length=1)


class ConstitutionalSearchSuccess(FrozenModel):
    """``anayasa_karari_ara`` için herkese açık başarı yanıt zarfı."""

    ok: Literal[True] = True
    hits: tuple[ConstitutionalHit, ...]
    page: PageInfo
    warnings: tuple[ConstitutionalWarning, ...] = ()
    fetched_at: datetime | None = None
    validated_at: datetime | None = None
    expires_at: datetime | None = None
    revalidation_error: RevalidationError | None = None

    @model_validator(mode="after")
    def _stale_fields_are_consistent(self) -> ConstitutionalSearchSuccess:
        stale = any(warning.code == "stale_content" for warning in self.warnings)
        if stale:
            if (
                self.fetched_at is None
                or self.validated_at is None
                or self.expires_at is None
                or self.revalidation_error is None
            ):
                raise ValueError(
                    "güncel olmayan arama başarısı önbellek zaman damgalarını ve revalidation_error gerektirir"
                )
        elif self.revalidation_error is not None:
            raise ValueError(
                "revalidation_error yalnızca güncel olmayan arama başarısı için geçerlidir"
            )
        return self


class ConstitutionalSearchRequest(FrozenModel):
    """Belirlenimci sorgu derlemesi dahil, doğrulanmış herkese açık arama girdileri."""

    kind: ConstitutionalKind
    query: str | None = None
    esas_no: str | None = Field(default=None, pattern=_CITATION_PATTERN)
    karar_no: str | None = Field(default=None, pattern=_CITATION_PATTERN)
    application_number: str | None = Field(default=None, pattern=_CITATION_PATTERN)
    page: int = Field(default=1, ge=1, strict=True)
    page_size: int = Field(default=10, ge=1, le=10, strict=True)
    include_snippet: bool = False

    @field_validator(
        "query", "esas_no", "karar_no", "application_number", mode="before"
    )
    @classmethod
    def _strip_nonempty_strings(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("boş olamaz")
        return stripped

    @model_validator(mode="after")
    def _validate_kind_specific_inputs(self) -> ConstitutionalSearchRequest:
        if self.kind is ConstitutionalKind.NORM_REVIEW:
            if self.application_number is not None:
                raise ValueError(
                    "application_number yalnızca bireysel_basvuru için geçerlidir"
                )
            if self.query is None and self.esas_no is None and self.karar_no is None:
                raise ValueError(
                    "norm_denetimi için query, esas_no veya karar_no gereklidir"
                )
        else:
            if self.esas_no is not None or self.karar_no is not None:
                raise ValueError(
                    "esas_no ve karar_no yalnızca norm_denetimi için geçerlidir"
                )
            if self.query is None and self.application_number is None:
                raise ValueError(
                    "bireysel_basvuru için query veya application_number gereklidir"
                )
        return self

    @property
    def compiled_query(self) -> str:
        terms: list[str] = []
        if self.query is not None:
            terms.append(self.query)
        if self.kind is ConstitutionalKind.NORM_REVIEW:
            if self.esas_no is not None:
                terms.append(self.esas_no)
            if self.karar_no is not None:
                terms.append(self.karar_no)
        elif self.application_number is not None:
            terms.append(self.application_number)
        return " ".join(terms)

    @property
    def has_identifier_filter(self) -> bool:
        return (
            self.esas_no is not None
            or self.karar_no is not None
            or self.application_number is not None
        )


class ConstitutionalDocumentRequest(FrozenModel):
    """Doğrulanmış herkese açık AYM belge getirme girdileri."""

    id: str = Field(min_length=1, strict=True)
    page: int = Field(default=1, ge=1, strict=True)
    refresh: bool = False

    @field_validator("id")
    @classmethod
    def _require_anayasa_id(cls, value: str) -> str:
        try:
            parsed = parse_document_id(value)
        except InvalidDocumentId as exc:
            raise ValueError(
                "kanonik bir anayasa:nd|bb:YYYY:N belge kimliği olmalıdır"
            ) from exc
        if not isinstance(parsed, AnayasaId):
            raise ValueError("kanonik bir anayasa:nd|bb:YYYY:N belge kimliği olmalıdır")  # noqa: TRY004
        return parsed.format()


@dataclass(frozen=True, slots=True)
class _SearchResolution:
    page: AnayasaSearchPage
    cache_record: CacheRecord
    stale_error: UpstreamUnavailable | None = None


class ConstitutionalService:
    """Güncel AYM SPA sağlayıcısı üzerinde V1 servis cephesi.

    Aramaların 24 saatlik önbellek penceresi vardır ve yalnızca üst kaynağın
    ağ/5xx hatasından sonra güncel olmayan önbellek yanıtı sunabilir. AYM belge
    kayıtlarının planlanmış son kullanma zamanı yoktur: ``refresh`` yeni bir
    API çözümlemesini zorlar; hata oluşursa güncel olmayan başarılı belge yerine
    hata döndürülür.
    """

    def __init__(
        self,
        provider: AnayasaClient,
        cache: CacheStore,
        singleflight: SingleFlight[object] | None = None,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._singleflight = singleflight or SingleFlight()

    async def search(
        self,
        *,
        kind: ConstitutionalKind | str,
        query: str | None = None,
        esas_no: str | None = None,
        karar_no: str | None = None,
        application_number: str | None = None,
        page: int = 1,
        page_size: int = 10,
        include_snippet: bool = False,
    ) -> dict[str, Any]:
        """Yalnızca norm denetimi veya bireysel başvuru kayıtlarında arama yapar."""
        try:
            request = ConstitutionalSearchRequest.model_validate(
                {
                    "kind": kind,
                    "query": query,
                    "esas_no": esas_no,
                    "karar_no": karar_no,
                    "application_number": application_number,
                    "page": page,
                    "page_size": page_size,
                    "include_snippet": include_snippet,
                }
            )
        except Exception as exc:  # noqa: BLE001 - maps all failures to ErrorEnvelope
            return error_envelope(exc, upstream="anayasa")

        key = _search_cache_key(request)
        try:
            resolution = await self._resolve_search(request, key)
            warnings: list[ConstitutionalWarning] = []
            if request.has_identifier_filter:
                warnings.append(
                    ConstitutionalWarning(
                        code="query_backed_identifier_filter",
                        message=_QUERY_IDENTIFIER_WARNING,
                    )
                )
            if resolution.stale_error is not None:
                warnings.append(
                    ConstitutionalWarning(code="stale_content", message=_STALE_MESSAGE)
                )
            success = ConstitutionalSearchSuccess(
                hits=_hits_from_records(
                    resolution.page.records, include_snippet=request.include_snippet
                ),
                page=_search_page_info(
                    total=resolution.page.total_records,
                    page=request.page,
                    page_size=request.page_size,
                ),
                warnings=tuple(warnings),
                fetched_at=(
                    resolution.cache_record.fetched_at
                    if resolution.stale_error is not None
                    else None
                ),
                validated_at=(
                    resolution.cache_record.validated_at
                    if resolution.stale_error is not None
                    else None
                ),
                expires_at=(
                    resolution.cache_record.expires_at
                    if resolution.stale_error is not None
                    else None
                ),
                revalidation_error=(
                    unavailable_revalidation_error(resolution.stale_error)
                    if resolution.stale_error is not None
                    else None
                ),
            )
            return _serialize_success(success)
        except AnayasaNotFound as exc:
            return error_envelope(
                UpstreamNotFound(str(exc)),
                upstream="anayasa",
                not_found_as_unavailable=True,
            )
        except Exception as exc:  # noqa: BLE001 - maps all failures to ErrorEnvelope
            return error_envelope(
                exc, upstream="anayasa", not_found_as_unavailable=True
            )

    async def get_document(
        self,
        *,
        id: str,
        page: int = 1,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Kanonik bir ``anayasa:`` kimliğiyle sayfalanmış bir belge getirir."""
        try:
            request = ConstitutionalDocumentRequest.model_validate(
                {"id": id, "page": page, "refresh": refresh}
            )
        except Exception as exc:  # noqa: BLE001 - maps all failures to ErrorEnvelope
            return error_envelope(exc, upstream="anayasa")

        document_id = parse_document_id(request.id)
        assert isinstance(document_id, AnayasaId)
        try:
            record = (
                None
                if request.refresh
                else self._cache.get(_DOCUMENT_CACHE_NAMESPACE, request.id)
            )
            if record is None:
                record = await self._fetch_document(
                    document_id=document_id, refresh=request.refresh
                )
            payload = _document_success(record, requested_page=request.page).model_dump(
                mode="json"
            )
            payload.pop("revalidation_error", None)
            return payload
        except AnayasaNotFound as exc:
            return error_envelope(UpstreamNotFound(str(exc)), upstream="anayasa")
        except Exception as exc:  # noqa: BLE001 - maps all failures to ErrorEnvelope
            return error_envelope(exc, upstream="anayasa")

    async def _resolve_search(
        self, request: ConstitutionalSearchRequest, key: str
    ) -> _SearchResolution:
        cached = self._cache.get(_SEARCH_CACHE_NAMESPACE, key, now=utc_now())
        if cached is not None and cached.freshness is Freshness.FRESH:
            return _SearchResolution(page=_page_from_cache(cached), cache_record=cached)

        async def fetch() -> _SearchResolution:
            current = self._cache.get(_SEARCH_CACHE_NAMESPACE, key, now=utc_now())
            if current is not None and current.freshness is Freshness.FRESH:
                return _SearchResolution(
                    page=_page_from_cache(current), cache_record=current
                )
            try:
                upstream_page = await self._provider.search(
                    kind=request.kind,
                    query=request.compiled_query,
                    page=request.page,
                    size=request.page_size,
                )
            except UpstreamUnavailable as exc:
                if (
                    current is not None
                    and current.freshness is Freshness.EXPIRED
                    and exc.stale_eligible
                    and (exc.status_code is None or exc.status_code >= 500)
                ):
                    return _SearchResolution(
                        page=_page_from_cache(current),
                        cache_record=current,
                        stale_error=exc,
                    )
                raise

            moment = utc_now()
            stored = self._cache.put(
                namespace=_SEARCH_CACHE_NAMESPACE,
                key=key,
                source_url=_search_source_url(request.kind),
                content=_serialize_search_page(upstream_page),
                fetched_at=moment,
                validated_at=moment,
                expires_at=moment + SEARCH_CACHE_TTL,
                mime_type="application/json",
            )
            return _SearchResolution(page=upstream_page, cache_record=stored)

        result = await self._singleflight.do(("anayasa-search", key), fetch)
        assert isinstance(result, _SearchResolution)
        return result

    async def _fetch_document(
        self, *, document_id: AnayasaId, refresh: bool
    ) -> CacheRecord:
        key = document_id.format()

        async def fetch() -> CacheRecord:
            if not refresh:
                current = self._cache.get(_DOCUMENT_CACHE_NAMESPACE, key)
                if current is not None:
                    return current
            provider_record = await self._provider.get_document(document_id)
            if provider_record.document_id != document_id:
                raise AnayasaProtocolError(
                    "sağlayıcı farklı bir ad alanından belge döndürdü"
                )
            if provider_record.html_content is None:
                raise AnayasaProtocolError("sağlayıcı karar HTML'si döndürmedi")
            converted = await convert_document(
                provider_record.html_content.encode("utf-8"),
                "text/html; charset=utf-8",
                provider_record.source_url,
            )
            moment = utc_now()
            return self._cache.put(
                namespace=_DOCUMENT_CACHE_NAMESPACE,
                key=key,
                source_url=provider_record.source_url,
                content=converted.markdown,
                content_hash=converted.content_hash,
                fetched_at=moment,
                validated_at=moment,
                expires_at=None,
                mime_type=converted.mime_type,
                conversion=converted.conversion,
            )

        result = await self._singleflight.do(("anayasa-document", key), fetch)
        assert isinstance(result, CacheRecord)
        return result


def _serialize_success(success: ConstitutionalSearchSuccess) -> dict[str, Any]:
    """Güncel olmayanlara özgü metaveriyi çıkarırken gerekli boş değerli sonuç alanlarını korur."""
    payload = success.model_dump(mode="json")
    for field in (
        "fetched_at",
        "validated_at",
        "expires_at",
        "revalidation_error",
    ):
        if payload[field] is None:
            payload.pop(field)
    return payload


def _search_cache_key(request: ConstitutionalSearchRequest) -> str:
    return canonical_parameter_hash(
        {
            "provider": "anayasa-kbb-v1",
            "kind": request.kind,
            "query": request.compiled_query,
            "page": request.page,
            "page_size": request.page_size,
        }
    )


def _search_source_url(kind: ConstitutionalKind) -> str:
    return (
        NORM_ORIGIN if kind is ConstitutionalKind.NORM_REVIEW else INDIVIDUAL_ORIGIN
    ) + SEARCH_PATH


def _serialize_search_page(page: AnayasaSearchPage) -> str:
    return json.dumps(
        {
            "total_records": page.total_records,
            "page": page.page,
            "page_size": page.page_size,
            "records": [
                {
                    "id": record.document_id.format(),
                    "kind": record.kind.value,
                    "provider_id": record.provider_id,
                    "source_url": record.source_url,
                    "title": record.title,
                    "summary": record.summary,
                    "esas_no": record.esas_no,
                    "karar_no": record.karar_no,
                    "application_number": record.application_number,
                    "decision_date": (
                        record.decision_date.isoformat()
                        if record.decision_date is not None
                        else None
                    ),
                    "snippet": record.snippet,
                }
                for record in page.records
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _page_from_cache(record: CacheRecord) -> AnayasaSearchPage:
    try:
        payload = json.loads(record.content)
        if not isinstance(payload, Mapping):
            raise TypeError("arama önbelleği yükü bir nesne değildir")
        raw_records = payload["records"]
        if not isinstance(raw_records, list):
            raise TypeError("arama önbelleği kayıtları bir dizi değildir")
        return AnayasaSearchPage(
            records=tuple(_record_from_cache(item) for item in raw_records),
            total_records=_non_negative_int(payload["total_records"], "total_records"),
            page=_positive_int(payload["page"], "page"),
            page_size=_positive_int(payload["page_size"], "page_size"),
        )
    except (KeyError, TypeError, ValueError, InvalidDocumentId) as exc:
        raise AnayasaProtocolError("anayasal arama önbelleği bozuk") from exc


def _record_from_cache(value: object) -> AnayasaRecord:
    if not isinstance(value, Mapping):
        raise TypeError("önbelleğe alınmış kayıt bir nesne değildir")
    parsed = parse_document_id(_required_string(value, "id"))
    if not isinstance(parsed, AnayasaId):
        raise TypeError("önbelleğe alınmış kayıt id'si anayasa değildir")
    kind = ConstitutionalKind(_required_string(value, "kind"))
    expected_kind = (
        ConstitutionalKind.NORM_REVIEW
        if parsed.kind == "nd"
        else ConstitutionalKind.INDIVIDUAL_APPLICATION
    )
    if kind is not expected_kind:
        raise ValueError("önbelleğe alınmış kayıt türü kimliğiyle çelişiyor")
    raw_date = _nullable_string(value, "decision_date")
    return AnayasaRecord(
        document_id=parsed,
        kind=kind,
        provider_id=_required_string(value, "provider_id"),
        source_url=_required_string(value, "source_url"),
        title=_nullable_string(value, "title"),
        summary=_nullable_string(value, "summary"),
        esas_no=_nullable_string(value, "esas_no"),
        karar_no=_nullable_string(value, "karar_no"),
        application_number=_nullable_string(value, "application_number"),
        decision_date=CalendarDate.fromisoformat(raw_date)
        if raw_date is not None
        else None,
        snippet=_nullable_string(value, "snippet"),
    )


def _hits_from_records(
    records: tuple[AnayasaRecord, ...], *, include_snippet: bool
) -> tuple[ConstitutionalHit, ...]:
    return tuple(
        ConstitutionalHit(
            id=record.document_id.format(),
            kind=record.kind,
            source_url=record.source_url,
            title=record.title,
            snippet=record.snippet if include_snippet else None,
            esas_no=record.esas_no,
            karar_no=record.karar_no,
            application_number=record.application_number,
            date=record.decision_date,
            summary=record.summary,
        )
        for record in records
    )


def _search_page_info(*, total: int, page: int, page_size: int) -> PageInfo:
    total_pages = (total + page_size - 1) // page_size
    return PageInfo(
        page=page,
        page_size=page_size,
        total_records=total,
        total_pages=total_pages,
        has_more=page < total_pages,
    )


def _document_success(record: CacheRecord, *, requested_page: int) -> DocumentSuccess:
    if record.conversion is None:
        raise AnayasaProtocolError("anayasal belge önbelleğinde dönüşüm metaverisi yok")
    chunks = chunk_markdown(record.content, max_chars=_DOCUMENT_PAGE_MAX_CHARS)
    total_pages = len(chunks)
    if requested_page > total_pages:
        raise ChunkOutOfRangeError(page=requested_page, total_pages=total_pages)
    markdown = chunks[requested_page - 1]
    return DocumentSuccess(
        id=record.key,
        source="anayasa",
        source_url=record.source_url,
        markdown=markdown,
        mime_type=record.mime_type,
        conversion=record.conversion,
        content_hash=record.content_hash,
        page=PageInfo(
            page=requested_page,
            page_size=len(markdown),
            total_records=1,
            total_pages=total_pages,
            has_more=requested_page < total_pages,
        ),
        fetched_at=record.fetched_at,
        validated_at=record.validated_at,
        expires_at=None,
    )


def _required_string(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{field} boş olmayan bir metin olmalıdır")
    return item


def _nullable_string(value: Mapping[str, object], field: str) -> str | None:
    item = value.get(field)
    if item is None:
        return None
    if not isinstance(item, str):
        raise TypeError(f"{field} bir metin veya null olmalıdır")
    return item


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} pozitif bir tam sayı olmalıdır")
    return value


def _non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} negatif olmayan bir tam sayı olmalıdır")
    return value
