"""V1 karar arama ve getirme için resmî Bedesten uyarlayıcısı.

Bu modül yalnızca protokol çevirisi yapar: normalleştirilmiş servis isteklerini
kabul eder, sağlayıcının ölçülmüş kayan pencere sınırını uygular ve yalın
modeller üretir. FastMCP aracı kaydetmez veya HTTP istemcisi oluşturmaz.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import math
import re
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from mutalaamcp.domain.models import Court, DecisionHit, FrozenModel, PageInfo
from mutalaamcp.net import SafeHttpClient, UpstreamRateLimited, UpstreamUnavailable

SEARCH_URL = "https://bedesten.adalet.gov.tr/emsal-karar/searchDocuments"
DOCUMENT_URL = "https://bedesten.adalet.gov.tr/emsal-karar/getDocumentContent"
SOURCE_URL_TEMPLATE = "https://mevzuat.adalet.gov.tr/ictihat/{document_id}"
_ORIGIN_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://mevzuat.adalet.gov.tr",
    "Referer": "https://mevzuat.adalet.gov.tr/",
}
_COURT_ITEM_TYPES: Mapping[Court, str] = {
    Court.YARGITAY: "YARGITAYKARARI",
    Court.DANISTAY: "DANISTAYKARAR",
    Court.ISTINAF: "ISTINAFHUKUK",
    Court.YEREL: "YERELHUKUK",
    Court.KYB: "KYB",
}
_ITEM_TYPE_COURTS = {value: key for key, value in _COURT_ITEM_TYPES.items()}


def _normalize_name(value: str) -> str:
    return re.sub(
        r"\s+", " ", value.casefold().replace("ı", "i").replace("İ", "i")
    ).strip()


_BIRIM_ADI_MAPPING: dict[str, str] = {
    **{f"H{index}": f"{index}. Hukuk Dairesi" for index in range(1, 24)},
    **{f"C{index}": f"{index}. Ceza Dairesi" for index in range(1, 24)},
    "HGK": "Hukuk Genel Kurulu",
    "CGK": "Ceza Genel Kurulu",
    "BGK": "Büyük Genel Kurul",
    "HBK": "Hukuk Genel Kurulu",
    "CBK": "Ceza Genel Kurulu",
    **{f"D{index}": f"{index}. Daire" for index in range(1, 18)},
    "DBGK": "Dava Daireleri Genel Kurulu",
    "IDDK": "İdari Dava Daireleri Kurulu",
    "VDDK": "Vergi Dava Daireleri Kurulu",
    "IBK": "İçtihatları Birleştirme Kurulu",
    "IIK": "İçtihatları Birleştirme Kurulu",
    "DBK": "Danıştay Başkanlar Kurulu",
}
_REVERSE_BIRIM_ADI_MAPPING = {
    _normalize_name(name): code for code, name in _BIRIM_ADI_MAPPING.items()
}


class DecisionSearchRequest(FrozenModel):
    """Bedesten çevirisinden önce doğrulanmış V1 karar arama parametreleri."""

    query: str | None = Field(default=None, min_length=1)
    courts: tuple[Court, ...] = Field(min_length=1)
    chamber: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    esas_year: int | None = Field(default=None, ge=1000, le=9999)
    esas_sequence: int | None = Field(default=None, ge=1)
    karar_year: int | None = Field(default=None, ge=1000, le=9999)
    karar_sequence: int | None = Field(default=None, ge=1)
    sort: Literal["tarih_yeniden_eskiye", "tarih_eskiden_yeniye"] = (
        "tarih_yeniden_eskiye"
    )
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=10)
    include_snippet: bool = False

    @field_validator("query")
    @classmethod
    def _reject_unsupported_query_syntax(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if re.search(r"[*?]|\\|(?<!\w)/(?:\\.|[^/])+/[A-Za-z]*(?!\w)|~(?:\d+)?", value):
            raise ValueError(
                "joker karakter, düzenli ifade, bulanık ve yakınlık söz dizimleri desteklenmez"
            )
        return value

    @field_validator("chamber")
    @classmethod
    def _known_chamber(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.upper()
        if normalized not in _BIRIM_ADI_MAPPING:
            raise ValueError("bilinmeyen daire kodu")
        return normalized

    @model_validator(mode="after")
    def _request_invariants(self) -> DecisionSearchRequest:
        if len(set(self.courts)) != len(self.courts):
            raise ValueError("courts yinelenen değerler içermemelidir")
        if not self.query and self.esas_year is None and self.karar_year is None:
            raise ValueError("query veya eksiksiz bir E/K çifti gereklidir")
        if (self.esas_year is None) != (self.esas_sequence is None):
            raise ValueError("esas_year ile esas_sequence birlikte sağlanmalıdır")
        if (self.karar_year is None) != (self.karar_sequence is None):
            raise ValueError("karar_year ile karar_sequence birlikte sağlanmalıdır")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from, date_to değerinden sonra olamaz")
        return self


@dataclass(frozen=True, slots=True)
class BedestenSearchResult:
    hits: tuple[DecisionHit, ...]
    page: PageInfo
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


@dataclass(frozen=True, slots=True)
class BedestenDocumentPayload:
    data: bytes
    mime_type: str
    source_url: str
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


class ProviderProtocolError(UpstreamUnavailable):
    """Resmî uç nokta yanıt verdi ancak bilinen yanıt zarfıyla eşleşmedi."""


class DecisionRateLimiter(Protocol):
    """Karar sağlayıcısının gerektirdiği asgari hız sınırlayıcı sözleşmesi."""

    async def acquire(self) -> None: ...

    async def pause(self, retry_after: float | None) -> None: ...


class RollingWindowRateLimiter:
    """Bedesten kararları için tam 10 istekli kayan pencere sınırlayıcısı."""

    def __init__(self, *, capacity: int = 10, window_seconds: float = 30.0) -> None:
        if capacity < 1 or window_seconds <= 0:
            raise ValueError("capacity ve window_seconds pozitif olmalıdır")
        self._capacity = capacity
        self._window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                while (
                    self._timestamps
                    and self._timestamps[0] <= now - self._window_seconds
                ):
                    self._timestamps.popleft()
                if now < self._blocked_until:
                    delay = self._blocked_until - now
                elif len(self._timestamps) < self._capacity:
                    self._timestamps.append(now)
                    return
                else:
                    delay = self._timestamps[0] + self._window_seconds - now
            await asyncio.sleep(max(delay, 0.001))

    async def pause(self, retry_after: float | None) -> None:
        """Sonraki istekten önce yerel kapasiteyi boşaltarak üst kaynaktaki 429'u uygula."""
        delay = (
            min(max(retry_after if retry_after is not None else 30.0, 1.0), 60.0) + 0.5
        )
        async with self._lock:
            self._timestamps.clear()
            self._blocked_until = max(self._blocked_until, time.monotonic() + delay)


class BedestenDecisionProvider:
    """V1 karar isteklerini doğrulanmış Bedesten POST yanıt zarflarına çevirir."""

    search_url = SEARCH_URL
    document_url = DOCUMENT_URL

    def __init__(
        self,
        http: SafeHttpClient,
        *,
        limiter: DecisionRateLimiter | None = None,
    ) -> None:
        self._http = http
        self._limiter = limiter or RollingWindowRateLimiter()

    async def search(
        self,
        request: DecisionSearchRequest,
        *,
        conditional_headers: Mapping[str, str] | None = None,
    ) -> BedestenSearchResult:
        await self._limiter.acquire()
        headers = {**_ORIGIN_HEADERS, **(conditional_headers or {})}
        try:
            response = await self._http.request(
                "POST",
                SEARCH_URL,
                headers=headers,
                json=_search_envelope(request),
            )
        except UpstreamRateLimited as exc:
            await self._limiter.pause(exc.retry_after)
            raise
        if response.status_code == 304:
            return BedestenSearchResult(
                (),
                _empty_page(request),
                response.headers.get("etag"),
                response.headers.get("last-modified"),
                not_modified=True,
            )
        payload = _json_payload(response, "karar araması")
        _require_success(payload, "karar araması")
        records = _search_records(payload)
        total_records = _total_records(payload)
        _start_records(payload)
        hits = tuple(_normalize_hit(record, request) for record in records)
        page = PageInfo(
            page=request.page,
            page_size=request.page_size,
            total_records=total_records,
            total_pages=math.ceil(total_records / request.page_size)
            if total_records
            else 0,
            has_more=request.page < math.ceil(total_records / request.page_size)
            if total_records
            else False,
        )
        return BedestenSearchResult(
            hits=hits,
            page=page,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )

    async def get_document(
        self,
        document_id: str,
        *,
        conditional_headers: Mapping[str, str] | None = None,
    ) -> BedestenDocumentPayload:
        await self._limiter.acquire()
        headers = {**_ORIGIN_HEADERS, **(conditional_headers or {})}
        try:
            response = await self._http.request(
                "POST",
                DOCUMENT_URL,
                headers=headers,
                json={
                    "applicationName": "UyapMevzuat",
                    "data": {"documentId": document_id},
                },
            )
        except UpstreamRateLimited as exc:
            await self._limiter.pause(exc.retry_after)
            raise
        source_url = SOURCE_URL_TEMPLATE.format(document_id=document_id)
        if response.status_code == 304:
            return BedestenDocumentPayload(
                b"",
                "",
                source_url,
                response.headers.get("etag"),
                response.headers.get("last-modified"),
                not_modified=True,
            )
        data, mime_type = _document_content(response)
        return BedestenDocumentPayload(
            data=data,
            mime_type=mime_type,
            source_url=source_url,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )


def _search_envelope(request: DecisionSearchRequest) -> dict[str, Any]:
    data: dict[str, Any] = {
        "pageSize": request.page_size,
        "pageNumber": request.page,
        "itemTypeList": [_COURT_ITEM_TYPES[court] for court in request.courts],
        "sortFields": ["KARAR_TARIHI"],
        "sortDirection": "desc" if request.sort == "tarih_yeniden_eskiye" else "ASC",
    }
    if request.query:
        data["phrase"] = request.query
    if request.chamber:
        data["birimAdi"] = _BIRIM_ADI_MAPPING[request.chamber]
    if request.date_from:
        data["kararTarihiStart"] = f"{request.date_from.isoformat()}T00:00:00.000Z"
    if request.date_to:
        data["kararTarihiEnd"] = f"{request.date_to.isoformat()}T23:59:59.000Z"
    if request.esas_year is not None:
        data["esasNoYil"] = str(request.esas_year)
        data["esasNoSira"] = str(request.esas_sequence)
    if request.karar_year is not None:
        data["kararNoYil"] = str(request.karar_year)
        data["kararNoSira"] = str(request.karar_sequence)
    return {"data": data, "applicationName": "UyapMevzuat", "paging": True}


def _json_payload(response: Any, operation: str) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderProtocolError(
            f"Bedesten {operation} geçersiz JSON döndürdü"
        ) from exc
    if not isinstance(payload, Mapping):
        raise ProviderProtocolError(
            f"Bedesten {operation} geçersiz bir yanıt zarfı döndürdü"
        )
    return payload


def _require_success(payload: Mapping[str, Any], operation: str) -> None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping) or metadata.get("FMTY") != "SUCCESS":
        raise ProviderProtocolError(
            f"Bedesten {operation} yanıtında metadata.FMTY SUCCESS değil"
        )


def _search_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ProviderProtocolError(
            "Bedesten karar araması yanıtında geçerli bir data alanı yok"
        )
    return data


def _search_records(payload: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    data = _search_data(payload)
    records = data.get("emsalKararList")
    if not isinstance(records, list) or not all(
        isinstance(item, Mapping) for item in records
    ):
        raise ProviderProtocolError(
            "Bedesten karar araması yanıtında geçerli bir emsalKararList yok"
        )
    return records


def _total_records(payload: Mapping[str, Any]) -> int:
    data = _search_data(payload)
    value = data.get("total")
    if isinstance(value, bool):
        raise ProviderProtocolError(
            "Bedesten karar araması yanıtındaki total alanı tamsayı değil"
        )
    if isinstance(value, int):
        total = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        try:
            total = int(value)
        except ValueError as exc:
            raise ProviderProtocolError(
                "Bedesten karar araması yanıtındaki total alanı tamsayı değil"
            ) from exc
    else:
        raise ProviderProtocolError(
            "Bedesten karar araması yanıtındaki total alanı tamsayı değil"
        )
    if total < 0:
        raise ProviderProtocolError(
            "Bedesten karar araması yanıtındaki total alanı negatif olamaz"
        )
    return total


def _start_records(payload: Mapping[str, Any]) -> int:
    data = _search_data(payload)
    value = data.get("start")
    if isinstance(value, bool):
        raise ProviderProtocolError(
            "Bedesten karar araması yanıtındaki start alanı tamsayı değil"
        )
    if isinstance(value, int):
        start = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        try:
            start = int(value)
        except ValueError as exc:
            raise ProviderProtocolError(
                "Bedesten karar araması yanıtındaki start alanı tamsayı değil"
            ) from exc
    else:
        raise ProviderProtocolError(
            "Bedesten karar araması yanıtındaki start alanı tamsayı değil"
        )
    if start < 0:
        raise ProviderProtocolError(
            "Bedesten karar araması yanıtındaki start alanı negatif olamaz"
        )
    return start


def _normalize_hit(
    record: Mapping[str, Any], request: DecisionSearchRequest
) -> DecisionHit:
    document_id = _required_text(
        record, "documentId", "document_id", "id", "emsalKararId"
    )
    item_type = _item_type_text(record)
    court = _court_from_record(item_type, record, request)
    chamber = _normalize_chamber(
        _text(record, "birimAdi", "birim_adi", "chamber", "daire")
    )
    esas_no = _decision_number(record, "esas")
    karar_no = _decision_number(record, "karar")
    title = _text(record, "title", "baslik", "kararAdi", "documentName", "adi")
    snippet = _snippet(record, request.query) if request.include_snippet else None
    return DecisionHit(
        id=f"bedesten:{document_id}",
        court=court,
        chamber=chamber,
        esas_no=esas_no,
        karar_no=karar_no,
        date=_parse_date(_text(record, "kararTarihi", "karar_tarihi", "date", "tarih")),
        title=title,
        snippet=snippet,
        source_url=SOURCE_URL_TEMPLATE.format(document_id=document_id),
    )


def _required_text(record: Mapping[str, Any], *keys: str) -> str:
    value = _text(record, *keys)
    if not value or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ProviderProtocolError(
            "Bedesten sonucu geçerli bir belge kimliği içermiyor"
        )
    return value


def _text(record: Mapping[str, Any], *keys: str) -> str | None:
    lower = {str(key).casefold(): value for key, value in record.items()}
    for key in keys:
        value = lower.get(key.casefold())
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return None


def _item_type_text(record: Mapping[str, Any]) -> str | None:
    lower = {str(key).casefold(): value for key, value in record.items()}
    for key in ("itemType", "item_type", "kararTuru", "karar_turu", "mahkemeTuru"):
        value = lower.get(key.casefold())
        if isinstance(value, Mapping):
            nested = {
                str(nested_key).casefold(): nested_value
                for nested_key, nested_value in value.items()
            }
            text = None
            for nested_key in ("name", "code", "value"):
                nested_value = nested.get(nested_key)
                if nested_value is None or isinstance(nested_value, Mapping):
                    continue
                candidate = str(nested_value).strip()
                if candidate:
                    text = candidate
                    break
        elif value is None:
            text = None
        else:
            text = str(value).strip()
        if text:
            return text
    return None


def _court_from_record(
    item_type: str | None,
    record: Mapping[str, Any],
    request: DecisionSearchRequest,
) -> Court:
    if item_type:
        normalized = _normalize_name(item_type).replace(" ", "")
        for prefix, court in _ITEM_TYPE_COURTS.items():
            if _normalize_name(prefix).replace(" ", "") in normalized:
                return court
    court_name = _text(record, "court", "mahkeme", "mahkemeAdi", "birimAdi")
    if court_name:
        normalized = _normalize_name(court_name)
        for token, court in (
            ("yargitay", Court.YARGITAY),
            ("danistay", Court.DANISTAY),
            ("istinaf", Court.ISTINAF),
            ("yerel", Court.YEREL),
            ("kyb", Court.KYB),
        ):
            if token in normalized:
                return court
    if len(request.courts) == 1:
        return request.courts[0]
    raise ProviderProtocolError("Bedesten sonucu çözümlenebilir bir mahkeme içermiyor")


def _normalize_chamber(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _normalize_name(value)
    if normalized in _REVERSE_BIRIM_ADI_MAPPING:
        return _REVERSE_BIRIM_ADI_MAPPING[normalized]
    without_court = re.sub(r"^(yargitay|danistay)\s+", "", normalized).strip()
    return _REVERSE_BIRIM_ADI_MAPPING.get(without_court, value)


def _decision_number(record: Mapping[str, Any], prefix: str) -> str | None:
    direct = _text(record, f"{prefix}No", f"{prefix}_no", prefix)
    if direct:
        return direct
    year = _text(record, f"{prefix}NoYil", f"{prefix}_no_yil", f"{prefix}Yil")
    sequence = _text(record, f"{prefix}NoSira", f"{prefix}_no_sira", f"{prefix}Sira")
    if year and sequence:
        return f"{year}/{sequence}"
    return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    for parser in (
        date.fromisoformat,
        lambda raw: _date_from_format(raw, "%d.%m.%Y"),
        lambda raw: _date_from_format(raw, "%d/%m/%Y"),
    ):
        try:
            return parser(value[:10])
        except ValueError:
            continue
    return None


def _date_from_format(value: str, format: str) -> date:
    parsed = time.strptime(value, format)
    return date(parsed.tm_year, parsed.tm_mon, parsed.tm_mday)


def _snippet(record: Mapping[str, Any], query: str | None) -> str | None:
    text = _text(record, "snippet", "highlight", "ozet", "summary", "metin", "text")
    if not text:
        return None
    collapsed = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
    if not collapsed:
        return None
    needle = ""
    if query:
        terms = re.findall(r"[\wçğıöşüÇĞİÖŞÜ]+", query)
        if terms:
            needle = terms[0].casefold()
    index = collapsed.casefold().find(needle) if needle else 0
    start = max(0, index - 80)
    end = min(len(collapsed), start + 280)
    result = collapsed[start:end].strip()
    if start:
        result = "…" + result
    if end < len(collapsed):
        result += "…"
    return result or None


def _document_content(response: Any) -> tuple[bytes, str]:
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if content_type and content_type not in {
        "application/json",
        "text/json",
        "application/problem+json",
    }:
        return bytes(response.content), content_type
    payload = _json_payload(response, "belge getirme")
    _require_success(payload, "belge getirme")
    content, mime_type = _extract_document_value(payload)
    try:
        decoded = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProviderProtocolError(
            "Bedesten belge getirme yanıtında geçersiz Base64 içerik"
        ) from exc
    return decoded, mime_type or "text/html"


def _extract_document_value(
    payload: Mapping[str, Any],
) -> tuple[str, str | None]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ProviderProtocolError(
            "Bedesten belge getirme yanıtında geçerli bir data alanı yok"
        )
    content = data.get("content")
    if not isinstance(content, str) or not content:
        raise ProviderProtocolError(
            "Bedesten belge getirme yanıtında belge içeriği yok"
        )
    mime_type = _text(
        data,
        "mimeType",
        "mime_type",
        "contentType",
        "content_type",
        "fileType",
    )
    return content, mime_type


def _empty_page(request: DecisionSearchRequest) -> PageInfo:
    return PageInfo(
        page=request.page,
        page_size=request.page_size,
        total_records=0,
        total_pages=0,
        has_more=False,
    )
