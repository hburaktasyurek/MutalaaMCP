"""HTTP-only Bedesten provider for the V1 legislation endpoints."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote, urlparse

from mutalaamcp.domain.models import LegislationHit, LegislationType, PageInfo
from mutalaamcp.net import SafeHttpClient

BASE_URL = "https://bedesten.adalet.gov.tr/mevzuat"
_APPLICATION_NAME = "UyapMevzuat"
_TYPES = tuple(member.value for member in LegislationType)
_ALLOWED_SOURCE_HOSTS = frozenset(
    {
        "bedesten.adalet.gov.tr",
        "mevzuat.adalet.gov.tr",
        "mevzuat.gov.tr",
        "www.mevzuat.gov.tr",
    }
)

_TURKISH_TITLE_LETTERS = frozenset("ÇĞİÖŞÜçğıöşüÂÎÛâîû")
_TITLE_LITERAL_PUNCTUATION = frozenset({".", ",", "'", "’", "(", ")", "-", "–"})


class LegislationProviderError(RuntimeError):
    """A Bedesten response could not satisfy the legislation protocol."""


class LegislationNotFound(LegislationProviderError):
    """Bedesten explicitly reported the requested legislation or article missing."""


class InvalidLegislationType(ValueError):
    """An input type is outside the fixed twelve-type V1 vocabulary."""


class InvalidLegislationTitle(ValueError):
    """A title uses search syntax outside Bedesten's supported title dialect."""

    code = "invalid_params"

    def __init__(self, message: str) -> None:
        self.field_errors = {"title": (message,)}
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SearchPage:
    hits: tuple[LegislationHit, ...]
    page: PageInfo
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


@dataclass(frozen=True, slots=True)
class ProviderOutline:
    """Raw outline payload and revalidation metadata returned by Bedesten."""

    tree: object | None
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


@dataclass(frozen=True, slots=True)
class ProviderDocument:
    """Unconverted bytes and revalidation metadata returned by Bedesten."""

    content: bytes | None
    mime_type: str | None
    source_url: str
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


class BedestenLegislationProvider:
    """Maps the fixed V1 legislation contract to Bedesten's JSON envelopes."""

    def __init__(self, http: SafeHttpClient) -> None:
        self._http = http

    async def search(
        self,
        *,
        query: str | None = None,
        title: str | None = None,
        number: str | None = None,
        types: Iterable[LegislationType | str] | None = None,
        exact_title: bool = False,
        gazette_date_from: date | None = None,
        gazette_date_to: date | None = None,
        gazette_issue: str | None = None,
        page: int = 1,
        page_size: int = 20,
        include_snippet: bool = False,
        conditional_headers: Mapping[str, str] | None = None,
    ) -> SearchPage:
        """Search all twelve types, preserving validators for HTTP 304."""
        if page < 1:
            raise ValueError("page değeri en az 1 olmalıdır")
        if not 1 <= page_size <= 20:
            raise ValueError("page_size değeri 1 ile 20 arasında olmalıdır")
        if not any(value and value.strip() for value in (query, title, number)):
            raise ValueError("query, title veya number alanlarından biri gereklidir")
        normalized_title = (
            _validate_title(title) if title is not None and title.strip() else None
        )
        if exact_title and normalized_title is None:
            raise ValueError("exact_title için title gereklidir")

        selected_types = self._normalize_types(types)
        data: dict[str, Any] = {
            "pageSize": page_size,
            "pageNumber": page,
            "sortFields": ["RESMI_GAZETE_TARIHI"],
            "sortDirection": "desc",
            "mevzuatTurList": list(selected_types),
        }
        if query and query.strip():
            data["phrase"] = query.strip()
        if normalized_title is not None:
            data["mevzuatAdi"] = normalized_title
        if number and number.strip():
            data["mevzuatNo"] = number.strip()
        if exact_title:
            data["tamCumle"] = True
        if gazette_date_from is not None:
            data["resmiGazeteTarihiStart"] = _gazette_start(gazette_date_from)
        if gazette_date_to is not None:
            data["resmiGazeteTarihiEnd"] = _gazette_end(gazette_date_to)
        if gazette_issue and gazette_issue.strip():
            data["resmiGazeteSayisi"] = gazette_issue.strip()

        response = await self._request(
            "/searchDocuments",
            {"applicationName": _APPLICATION_NAME, "paging": True, "data": data},
            conditional_headers=conditional_headers,
        )
        if response.status_code == 304:
            return SearchPage(
                hits=(),
                page=PageInfo(
                    page=page,
                    page_size=page_size,
                    total_records=0,
                    total_pages=0,
                    has_more=False,
                ),
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                not_modified=True,
            )
        payload = _response_json(response)
        self._require_success(payload)
        body = _data_mapping(payload)
        if "mevzuatList" not in body:
            raise LegislationProviderError(
                "Bedesten arama yanıtında mevzuatList alanı eksik."
            )
        documents = body["mevzuatList"]
        if not isinstance(documents, list):
            raise LegislationProviderError(
                "Bedesten arama yanıtındaki mevzuatList alanı dizi değil."
            )
        if "total" not in body:
            raise LegislationProviderError(
                "Bedesten arama yanıtında total alanı eksik."
            )
        total = _as_nonnegative_int(body["total"], "total")
        hits = tuple(
            _normalize_hit(document, include_snippet=include_snippet)
            for document in documents
            if isinstance(document, Mapping)
        )
        if len(hits) != len(documents):
            raise LegislationProviderError(
                "Bedesten arama yanıtında nesne olmayan sonuç öğesi var."
            )
        total_pages = (total + page_size - 1) // page_size
        return SearchPage(
            hits=hits,
            page=PageInfo(
                page=page,
                page_size=page_size,
                total_records=total,
                total_pages=total_pages,
                has_more=page < total_pages,
            ),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )

    async def get_document(
        self,
        mevzuat_id: str,
        *,
        conditional_headers: Mapping[str, str] | None = None,
    ) -> ProviderDocument:
        return await self._get_content(
            document_type="MEVZUAT",
            upstream_id=mevzuat_id,
            source_url=_canonical_source_url(mevzuat_id),
            conditional_headers=conditional_headers,
        )

    async def get_article(
        self,
        madde_id: str,
        *,
        legislation_id: str,
        conditional_headers: Mapping[str, str] | None = None,
    ) -> ProviderDocument:
        return await self._get_content(
            document_type="MADDE",
            upstream_id=madde_id,
            source_url=_canonical_source_url(legislation_id),
            conditional_headers=conditional_headers,
        )

    async def get_outline(
        self,
        mevzuat_id: str,
        *,
        conditional_headers: Mapping[str, str] | None = None,
    ) -> ProviderOutline:
        """Return raw tree plus cache validators, including HTTP 304 metadata."""
        response = await self._request(
            "/mevzuatMaddeTree",
            {"applicationName": _APPLICATION_NAME, "data": {"mevzuatId": mevzuat_id}},
            conditional_headers=conditional_headers,
        )
        if response.status_code == 304:
            return ProviderOutline(
                tree=None,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                not_modified=True,
            )
        payload = _response_json(response)
        self._require_success(payload)
        data = payload.get("data")
        if data is None:
            data = []
        elif not isinstance(data, (Mapping, list)):
            raise LegislationProviderError(
                "Bedesten madde ağacı yanıtındaki data alanı nesne veya dizi değil."
            )
        return ProviderOutline(
            tree=data,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )

    async def _get_content(
        self,
        *,
        document_type: str,
        upstream_id: str,
        source_url: str,
        conditional_headers: Mapping[str, str] | None,
    ) -> ProviderDocument:
        response = await self._request(
            "/getDocumentContent",
            {
                "applicationName": _APPLICATION_NAME,
                "data": {"documentType": document_type, "id": upstream_id},
            },
            conditional_headers=conditional_headers,
        )
        if response.status_code == 304:
            return ProviderDocument(
                content=None,
                mime_type=None,
                source_url=source_url,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                not_modified=True,
            )
        payload = _response_json(response)
        self._require_success(payload)
        data = _data_mapping(payload)
        content = data.get("content")
        if not isinstance(content, str) or not content:
            raise LegislationProviderError(
                "Bedesten içerik yanıtında content alanı eksik."
            )
        mime_type = (
            _first_string(data, "mimeType", "mimetype", "contentType") or "text/html"
        )
        raw_url = _first_string(data, "url", "sourceUrl", "sourceURL")
        return ProviderDocument(
            content=_decode_document_content(content),
            mime_type=mime_type,
            source_url=_canonical_source_url(
                upstream_id, raw_url=raw_url, fallback=source_url
            ),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )

    async def _request(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        conditional_headers: Mapping[str, str] | None,
    ):
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "AdaletApplicationName": _APPLICATION_NAME,
            "Origin": "https://mevzuat.adalet.gov.tr",
            "Referer": "https://mevzuat.adalet.gov.tr/",
        }
        if conditional_headers:
            headers.update(conditional_headers)
        return await self._http.request(
            "POST", f"{BASE_URL}{path}", json=payload, headers=headers
        )

    @staticmethod
    def _normalize_types(
        types: Iterable[LegislationType | str] | None,
    ) -> tuple[str, ...]:
        if types is None:
            return _TYPES
        if isinstance(types, str):
            raise InvalidLegislationType(
                "types, dizge değil mevzuat türleri koleksiyonu olmalıdır"
            )
        normalized: list[str] = []
        for raw in types:
            value = raw.value if isinstance(raw, LegislationType) else raw
            if not isinstance(value, str) or value not in _TYPES:
                raise InvalidLegislationType(f"Desteklenmeyen mevzuat türü: {value!r}.")
            if value in normalized:
                raise InvalidLegislationType(f"Yinelenen mevzuat türü: {value}.")
            normalized.append(value)
        if not normalized:
            raise InvalidLegislationType("types en az bir mevzuat türü içermelidir")
        return tuple(normalized)

    @staticmethod
    def _require_success(payload: Mapping[str, object]) -> None:
        if "metadata" not in payload:
            raise LegislationProviderError("Bedesten yanıtında metadata alanı eksik.")
        metadata = payload["metadata"]
        if not isinstance(metadata, Mapping):
            raise LegislationProviderError(
                "Bedesten yanıtındaki metadata alanı nesne değil."
            )
        if metadata.get("FMTY") == "SUCCESS":
            return
        if _metadata_reports_not_found(metadata):
            raise LegislationNotFound("İstenen belge Bedesten'de bulunamadı.")
        raise LegislationProviderError(
            "Bedesten yanıtında metadata.FMTY SUCCESS değil."
        )


def _metadata_reports_not_found(metadata: Mapping[str, object]) -> bool:
    """Classify a missing-record response without exposing upstream metadata."""
    code = _first_string(metadata, "FMC", "code", "errorCode")
    if code is not None:
        normalized_code = code.casefold().replace("-", "_")
        if "not_found" in normalized_code or normalized_code in {"404", "404_notfound"}:
            return True
    detail = _first_string(metadata, "FMU", "FMTE", "message", "error")
    return detail is not None and (
        "bulunamad" in detail.casefold() or "not found" in detail.casefold()
    )


def _normalize_hit(
    document: Mapping[str, object], *, include_snippet: bool
) -> LegislationHit:
    opaque = _first_string(document, "mevzuatId", "mevzuat_id", "id", "documentId")
    if opaque is None:
        raise LegislationProviderError(
            "Bedesten arama sonucunda mevzuatId alanı eksik."
        )
    legislation_type = _normalize_type(document.get("mevzuatTur"))
    title = _first_string(document, "mevzuatAdi", "mevzuat_adi", "title", "name")
    if title is None:
        raise LegislationProviderError(
            f"Bedesten mevzuat sonucunda mevzuatAdi alanı eksik: {opaque}."
        )
    number = _first_string(document, "mevzuatNo", "mevzuat_no", "number")
    gazette_date = _parse_date(
        _first_string(document, "resmiGazeteTarihi", "officialGazetteDate")
    )
    gazette_issue = _first_string(document, "resmiGazeteSayisi", "officialGazetteIssue")
    raw_url = _first_string(document, "url", "sourceUrl", "sourceURL")
    return LegislationHit(
        id=f"mevzuat:{opaque}",
        legislation_type=legislation_type,
        number=number,
        title=title,
        official_gazette_date=gazette_date,
        official_gazette_issue=gazette_issue,
        snippet=_first_string(document, "snippet", "highlight", "description")
        if include_snippet
        else None,
        source_url=_canonical_source_url(opaque, raw_url=raw_url),
    )


def _normalize_type(value: object) -> LegislationType:
    if isinstance(value, Mapping):
        value = _first_string(value, "name", "code", "value")
    if not isinstance(value, str):
        raise LegislationProviderError(
            "Bedesten arama sonucunda mevzuatTur alanı eksik."
        )
    try:
        return LegislationType(value)
    except ValueError as exc:
        raise LegislationProviderError(
            f"Bedesten desteklenmeyen mevzuatTur değeri döndürdü: {value!r}."
        ) from exc


def _response_json(response: object) -> Mapping[str, object]:
    try:
        payload = response.json()  # type: ignore[attr-defined]
    except (json.JSONDecodeError, ValueError) as exc:
        raise LegislationProviderError("Bedesten yanıtı geçerli JSON değil.") from exc
    if not isinstance(payload, Mapping):
        raise LegislationProviderError("Bedesten yanıtının JSON kökü nesne değil.")
    return payload


def _data_mapping(payload: Mapping[str, object]) -> Mapping[str, object]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise LegislationProviderError("Bedesten yanıtındaki data alanı nesne değil.")
    return data


def _decode_document_content(content: str) -> bytes:
    try:
        return base64.b64decode(content, validate=True)
    except ValueError:
        return content.encode("utf-8")


def _first_string(mapping: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


def _validate_title(value: str) -> str:
    """Başlık aramasını sözcükler, Türkçe noktalama işaretleri ve belgelenen jokerlerle sınırlar."""
    title = value.strip()
    for index, character in enumerate(title):
        if character == "?":
            continue
        if character == "*":
            if (
                index == 0
                or not _is_title_word_character(title[index - 1])
                or (index + 1 < len(title) and title[index + 1] != " ")
            ):
                raise InvalidLegislationTitle(
                    "title alanında * yalnızca sözcük sonunda kullanılabilir."
                )
            continue
        if character in {"-", "–"}:
            if (
                index == 0
                or index + 1 == len(title)
                or not _is_title_word_character(title[index - 1])
                or not _is_title_word_character(title[index + 1])
            ):
                raise InvalidLegislationTitle(
                    "title alanında tire yalnızca sözcük içinde kullanılabilir."
                )
            continue
        if (
            character == " "
            or _is_title_word_character(character)
            or character in _TITLE_LITERAL_PUNCTUATION
        ):
            continue
        raise InvalidLegislationTitle(
            "title alanı desteklenmeyen arama söz dizimi veya noktalama içeriyor."
        )
    return title


def _is_title_word_character(character: str) -> bool:
    return (character.isascii() and character.isalnum()) or (
        character in _TURKISH_TITLE_LETTERS
    )


def _as_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise LegislationProviderError(
            f"Bedesten yanıtındaki {field} alanı tamsayı değil."
        )
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        try:
            parsed = int(value)
        except ValueError as exc:
            raise LegislationProviderError(
                f"Bedesten yanıtındaki {field} alanı tamsayı değil."
            ) from exc
    else:
        raise LegislationProviderError(
            f"Bedesten yanıtındaki {field} alanı tamsayı değil."
        )
    if parsed < 0:
        raise LegislationProviderError(
            f"Bedesten yanıtındaki {field} alanı negatif olamaz."
        )
    return parsed


def _parse_day_first_date(value: str) -> date:
    day, month, year = value.split("/")
    return date(year=int(year), month=int(month), day=int(day))


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    for parser in (lambda raw: date.fromisoformat(raw[:10]), _parse_day_first_date):
        try:
            return parser(value)
        except ValueError:
            continue
    raise LegislationProviderError(
        f"Bedesten geçersiz resmî gazete tarihi döndürdü: {value!r}."
    )


def _gazette_start(value: date) -> str:
    if value == date.min:
        return f"{value.isoformat()}T00:00:00.000Z"
    return f"{(value - timedelta(days=1)).isoformat()}T21:00:00.000Z"


def _gazette_end(value: date) -> str:
    return f"{value.isoformat()}T21:00:00.000Z"


def _canonical_source_url(
    upstream_id: str,
    *,
    raw_url: str | None = None,
    fallback: str | None = None,
) -> str:
    if raw_url:
        parsed = urlparse(raw_url)
        if parsed.scheme == "https" and parsed.hostname in _ALLOWED_SOURCE_HOSTS:
            return raw_url
    if fallback:
        return fallback
    return f"{BASE_URL}/mevzuatDetay/{quote(upstream_id, safe='._-')}"
