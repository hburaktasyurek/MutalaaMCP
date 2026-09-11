"""Katı durum ve hata eşlemesiyle Mütalaa etkinleştirme API istemcisi."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, cast
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mutalaamcp import __version__
from mutalaamcp.domain.errors import ErrorCode
from mutalaamcp.settings import normalize_auth_origin

_ACTIVATE_PATH = "/api/mcp/v1/activate"
_ACTIVE = "active"
_PLAN = "mcp_local_free"
_FEATURE = "local_research"
type _AllowedTerms = tuple[str, str, int, str]


_STATUS_CODES: dict[int, dict[str, ErrorCode]] = {
    401: {"invalid_or_expired_token": ErrorCode.SESSION_EXPIRED},
    403: {
        "email_verification_required": ErrorCode.EMAIL_VERIFICATION_REQUIRED,
        "terms_required": ErrorCode.TERMS_REQUIRED,
        "account_disabled": ErrorCode.ACCOUNT_DISABLED,
    },
    409: {"account_link_required": ErrorCode.ACCOUNT_LINK_REQUIRED},
    429: {"rate_limited": ErrorCode.UPSTREAM_RATE_LIMITED},
    503: {"temporarily_unavailable": ErrorCode.UPSTREAM_UNAVAILABLE},
}

_ACTIVATION_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.SESSION_EXPIRED: "Oturumun süresi doldu veya oturum geçersiz.",
    ErrorCode.EMAIL_VERIFICATION_REQUIRED: "E-posta doğrulaması gerekli.",
    ErrorCode.TERMS_REQUIRED: "Devam etmek için koşulları kabul etmeniz gerekli.",
    ErrorCode.ACCOUNT_DISABLED: "Hesabınız devre dışı bırakılmış.",
    ErrorCode.ACCOUNT_LINK_REQUIRED: "Hesap bağlantısı gerekli.",
    ErrorCode.UPSTREAM_RATE_LIMITED: "Kimlik doğrulama hizmeti şu anda çok fazla istek alıyor.",
    ErrorCode.UPSTREAM_UNAVAILABLE: "Kimlik doğrulama hizmetine şu anda erişilemiyor.",
}
_REMOTE_DETAIL_LABEL = "Uzak hizmet ayrıntısı"


def activation_error_message(code: ErrorCode, remote_detail: str | None = None) -> str:
    """Etkinleştirme hatası için kararlı Türkçe metni, varsa uzak ayrıntıyla döndür."""
    message = _ACTIVATION_MESSAGES.get(code, "Etkinleştirme isteği başarısız oldu.")
    if remote_detail is None:
        return message
    return f"{message} {_REMOTE_DETAIL_LABEL}: {remote_detail}"


class ActivationError(Exception):
    """Eşlenmiş etkinleştirme API hatası."""

    def __init__(
        self,
        code: ErrorCode,
        remote_detail: str | None = None,
        *,
        http_status: int,
        terms_url: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.code = code
        self.remote_detail = remote_detail
        self.message = activation_error_message(code, remote_detail)
        self.http_status = http_status
        self.terms_url = terms_url
        self.retry_after = retry_after
        super().__init__(self.message)


class ActivationProtocolError(Exception):
    """Etkinleştirme yanıtı v1 sözleşmesiyle eşleşmedi."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        self.http_status = http_status
        super().__init__(message)


class ActivationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    status: Literal["active"]
    plan: Literal["mcp_local_free"]
    features: tuple[str, ...]
    checked_at: str = Field(min_length=1)

    @field_validator("features", mode="before")
    @classmethod
    def _exact_features(cls, value: object) -> object:
        if value != [_FEATURE] and value != (_FEATURE,):
            raise ValueError("features tam olarak ['local_research'] olmalıdır")
        return tuple(value) if not isinstance(value, tuple) else value

    @field_validator("checked_at")
    @classmethod
    def _checked_at_is_datetime(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                "checked_at bir ISO-8601 tarih-saat değeri olmalıdır"
            ) from exc
        return value


class ActivationClient:
    """WorkOS erişim belirteciyle ``/api/mcp/v1/activate`` uç noktasına POST yapar. Sunucu gizli anahtarı kullanmaz."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        activation_base_url: str,
        allowed_terms_base_url: str,
    ) -> None:
        self._http = http_client
        self._base = normalize_auth_origin(
            activation_base_url, field="etkinleştirme temel URL'si"
        ).rstrip("/")
        self._allowed_terms = _parse_allowed_terms_base(allowed_terms_base_url)

    async def activate(self, access_token: str) -> ActivationRecord:
        if not access_token:
            raise ValueError("access_token boş olmamalıdır")
        response = await self._http.post(
            f"{self._base}{_ACTIVATE_PATH}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "User-Agent": f"MutalaaMCP/{__version__}",
            },
            json={"client_version": __version__},
        )
        payload = _json_object(response, http_status=response.status_code)
        if response.status_code == 200:
            try:
                return ActivationRecord.model_validate(payload)
            except ValueError as exc:
                raise ActivationProtocolError(
                    f"Etkinleştirme 200 yanıt gövdesi geçerli etkin bir kayıt değil: {exc}",
                    http_status=200,
                ) from exc
        raise _mapped_error(response, payload, allowed_terms=self._allowed_terms)


def _json_object(response: httpx.Response, *, http_status: int) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ActivationProtocolError(
            "Etkinleştirme yanıtı JSON değildi",
            http_status=http_status,
        ) from exc
    if not isinstance(payload, dict):
        raise ActivationProtocolError(
            "Etkinleştirme yanıtının JSON'u bir nesne olmalıdır",
            http_status=http_status,
        )
    return cast(dict[str, Any], payload)


def _error_code(payload: Mapping[str, Any]) -> str | None:
    code = payload.get("code")
    if isinstance(code, str) and code:
        return code
    nested = payload.get("error")
    if isinstance(nested, Mapping):
        nested_code = nested.get("code")
        if isinstance(nested_code, str) and nested_code:
            return nested_code
    return None


def _remote_error_detail(payload: Mapping[str, Any]) -> str | None:
    message = payload.get("message")
    if isinstance(message, str) and message:
        return message
    nested = payload.get("error")
    if isinstance(nested, Mapping):
        nested_message = nested.get("message")
        if isinstance(nested_message, str) and nested_message:
            return nested_message
    return None


def _terms_url(payload: Mapping[str, Any], allowed_terms: _AllowedTerms) -> str | None:
    url = payload.get("terms_url")
    if isinstance(url, str) and url:
        return _allowed_terms_url(url, allowed_terms)
    details = payload.get("details")
    if isinstance(details, Mapping):
        nested = details.get("terms_url")
        if isinstance(nested, str) and nested:
            return _allowed_terms_url(nested, allowed_terms)
    error = payload.get("error")
    if isinstance(error, Mapping):
        error_url = error.get("terms_url")
        if isinstance(error_url, str) and error_url:
            return _allowed_terms_url(error_url, allowed_terms)
        error_details = error.get("details")
        if isinstance(error_details, Mapping):
            nested_url = error_details.get("terms_url")
            if isinstance(nested_url, str) and nested_url:
                return _allowed_terms_url(nested_url, allowed_terms)
    return None


def _retry_after(response: httpx.Response, payload: Mapping[str, Any]) -> float | None:
    for source in (
        payload.get("retry_after"),
        _nested_retry_after(payload),
        response.headers.get("Retry-After"),
    ):
        parsed = _parse_retry_after(source)
        if parsed is not None:
            return parsed
    return None


def _nested_retry_after(payload: Mapping[str, Any]) -> object:
    details = payload.get("details")
    if isinstance(details, Mapping) and "retry_after" in details:
        return details.get("retry_after")
    error = payload.get("error")
    if isinstance(error, Mapping):
        if "retry_after" in error:
            return error.get("retry_after")
        error_details = error.get("details")
        if isinstance(error_details, Mapping):
            return error_details.get("retry_after")
    return None


def _parse_retry_after(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if value < 0:
            raise ActivationProtocolError("retry_after >= 0 olmalıdır")
        return float(value)
    if isinstance(value, str) and value:
        try:
            parsed = float(value)
        except ValueError:
            return None
        if parsed < 0:
            raise ActivationProtocolError("retry_after >= 0 olmalıdır")
        return parsed
    return None


def _parse_allowed_terms_base(value: str) -> _AllowedTerms:
    normalized = normalize_auth_origin(value, field="allowed_terms_base_url")
    parsed = urlsplit(normalized)
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("allowed_terms_base_url bir ana makine içermelidir")
    try:
        port = (
            443
            if parsed.port is None and parsed.scheme == "https"
            else (80 if parsed.port is None else parsed.port)
        )
    except ValueError as exc:
        raise ValueError(
            "allowed_terms_base_url geçersiz bir bağlantı noktası içeriyor"
        ) from exc
    try:
        path = _normalize_url_path(parsed.path)
    except ValueError as exc:
        raise ValueError("allowed_terms_base_url yolu geçersiz") from exc
    return parsed.scheme, hostname.rstrip(".").lower(), port, path


def _normalize_url_path(path: str) -> str:
    decoded = unquote(path or "/")
    if any(ord(ch) < 32 or ch == "\\" for ch in decoded):
        raise ValueError("yol geçersiz karakterler içeriyor")
    segments: list[str] = []
    for segment in decoded.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise ValueError("yol '..' içeremez")
        segments.append(segment)
    return "/" + "/".join(segments)


def _path_under(path: str, allowed_path: str) -> bool:
    if allowed_path == "/":
        return path.startswith("/")
    return path == allowed_path or path.startswith(allowed_path + "/")


def _allowed_terms_url(value: str, allowed_terms: _AllowedTerms) -> str:
    if not value or any(ch.isspace() for ch in value):
        raise ActivationProtocolError(
            "terms_url izin verilen bir HTTPS koşullar URL'si değildir"
        )
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ActivationProtocolError(
            "terms_url izin verilen bir HTTPS koşullar URL'si değildir"
        )
    hostname = parsed.hostname
    if not hostname:
        raise ActivationProtocolError(
            "terms_url izin verilen bir HTTPS koşullar URL'si değildir"
        )
    try:
        port = (
            443
            if parsed.port is None and parsed.scheme == "https"
            else (80 if parsed.port is None else parsed.port)
        )
    except ValueError as exc:
        raise ActivationProtocolError(
            "terms_url izin verilen bir HTTPS koşullar URL'si değildir"
        ) from exc
    allowed_scheme, allowed_host, allowed_port, allowed_path = allowed_terms
    if (
        parsed.scheme != allowed_scheme
        or hostname.rstrip(".").lower() != allowed_host
        or port != allowed_port
    ):
        raise ActivationProtocolError(
            "terms_url izin verilen bir HTTPS koşullar URL'si değildir"
        )
    try:
        path = _normalize_url_path(parsed.path)
    except ValueError as exc:
        raise ActivationProtocolError(
            "terms_url izin verilen bir HTTPS koşullar URL'si değildir"
        ) from exc
    if not _path_under(path, allowed_path):
        raise ActivationProtocolError(
            "terms_url izin verilen bir HTTPS koşullar URL'si değildir"
        )
    return value


def _mapped_error(
    response: httpx.Response,
    payload: Mapping[str, Any],
    *,
    allowed_terms: _AllowedTerms,
) -> Exception:
    allowed = _STATUS_CODES.get(response.status_code)
    if allowed is None:
        return ActivationProtocolError(
            f"Beklenmeyen etkinleştirme HTTP durum kodu {response.status_code}",
            http_status=response.status_code,
        )
    code = _error_code(payload)
    if code not in allowed:
        return ActivationProtocolError(
            f"Etkinleştirme HTTP {response.status_code} gövde kodu {code!r}, v1 eşlemesinde yer almıyor",
            http_status=response.status_code,
        )
    error_code = allowed[code]
    remote_detail = _remote_error_detail(payload)
    terms_url: str | None = None
    retry_after: float | None = None
    if error_code is ErrorCode.TERMS_REQUIRED:
        try:
            terms_url = _terms_url(payload, allowed_terms)
        except ActivationProtocolError:
            return ActivationProtocolError(
                "terms_url izin verilen bir HTTPS koşullar URL'si değildir",
                http_status=response.status_code,
            )
        if terms_url is None:
            return ActivationProtocolError(
                "terms_required, terms_url gerektirir",
                http_status=response.status_code,
            )
    if error_code is ErrorCode.UPSTREAM_RATE_LIMITED:
        retry_after = _retry_after(response, payload)
        if retry_after is None:
            return ActivationProtocolError(
                "rate_limited, retry_after gerektirir",
                http_status=response.status_code,
            )
    return ActivationError(
        error_code,
        remote_detail,
        http_status=response.status_code,
        terms_url=terms_url,
        retry_after=retry_after,
    )
