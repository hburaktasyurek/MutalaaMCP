"""Cached V1 decision search and document retrieval service."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from mutalaamcp.cache.singleflight import SingleFlight
from mutalaamcp.cache.store import CacheRecord, CacheStore, Freshness
from mutalaamcp.conversion.documents import convert_document
from mutalaamcp.domain.articles import chunk_markdown
from mutalaamcp.domain.ids import BedestenId, InvalidDocumentId, parse_document_id
from mutalaamcp.domain.models import DecisionHit, PageInfo
from mutalaamcp.net import UpstreamUnavailable
from mutalaamcp.providers.bedesten.decisions import (
    SEARCH_URL,
    BedestenDecisionProvider,
    BedestenSearchResult,
    DecisionSearchRequest,
    ProviderProtocolError,
)
from mutalaamcp.services.common import (
    SEARCH_CACHE_TTL,
    ChunkOutOfRangeError,
    DecisionSearchSuccess,
    DocumentSuccess,
    InvalidParamsError,
    canonical_parameter_hash,
    conditional_headers,
    error_envelope,
    stale_warning,
    unavailable_revalidation_error,
    utc_now,
)

_SEARCH_NAMESPACE = "search_decisions:v1"
_DOCUMENT_NAMESPACE = "document:bedesten:v1"
_DOCUMENT_PAGE_TARGET_CHARS = 12_000


class DecisionService:
    """Public service boundary for compact search results and cached Markdown.

    Search cache keys are SHA-256 hashes of canonical parameters and therefore
    do not persist a raw user query in cache metadata.  Decision documents use
    their stable namespaced ID as a non-expiring cache key; only ``refresh``
    makes them contact the upstream again.
    """

    def __init__(
        self,
        provider: BedestenDecisionProvider,
        cache: CacheStore,
        *,
        singleflight: SingleFlight[object] | None = None,
        ocr: Any | None = None,
        document_page_target_chars: int = _DOCUMENT_PAGE_TARGET_CHARS,
    ) -> None:
        if document_page_target_chars < 1:
            raise ValueError("document_page_target_chars must be positive")
        self._provider = provider
        self._cache = cache
        self._singleflight = singleflight or SingleFlight()
        self._ocr = ocr
        self._document_page_target_chars = document_page_target_chars

    async def search(
        self,
        request: DecisionSearchRequest | Mapping[str, Any] | None = None,
        /,
        **parameters: Any,
    ) -> dict[str, Any]:
        """Search official decisions, returning the contract envelope in all cases."""
        try:
            parsed = _parse_search_request(request, parameters)
            key = canonical_parameter_hash(
                parsed.model_dump(mode="json"),
                unordered_fields=("courts",),
            )
            record = self._cache.get(_SEARCH_NAMESPACE, key)
            if record is not None and record.freshness is Freshness.FRESH:
                return _search_success_from_record(record)
            result = await self._singleflight.do(
                ("search_decisions", key),
                lambda: self._refresh_search(parsed, key),
            )
            assert isinstance(result, dict)
            return result
        except Exception as exc:  # noqa: BLE001 - Public service boundary maps every internal exception to an ErrorEnvelope.
            return error_envelope(
                exc, upstream="bedesten", not_found_as_unavailable=True
            )

    async def get_document(
        self,
        document_id: str,
        *,
        page: int = 1,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Return one Markdown page for a Bedesten search ID, never a raw URL."""
        try:
            opaque_id, canonical_id = _parse_bedesten_document_id(document_id)
            if not isinstance(page, int) or isinstance(page, bool) or page < 1:
                raise InvalidParamsError(
                    {"page": ("1 veya daha büyük bir tam sayı olmalıdır.",)}
                )
            if not isinstance(refresh, bool):
                raise InvalidParamsError(
                    {"refresh": ("Geçerli bir mantıksal değer olmalıdır.",)}
                )
            cached = self._cache.get(_DOCUMENT_NAMESPACE, canonical_id)
            if cached is not None and not refresh:
                return _document_success_from_record(
                    cached,
                    canonical_id,
                    page,
                    page_target_chars=self._document_page_target_chars,
                )
            record = await self._singleflight.do(
                ("bedesten_document", canonical_id),
                lambda: self._refresh_document(
                    opaque_id,
                    canonical_id,
                    cached=cached,
                ),
            )
            assert isinstance(record, CacheRecord)
            return _document_success_from_record(
                record,
                canonical_id,
                page,
                page_target_chars=self._document_page_target_chars,
            )
        except Exception as exc:  # noqa: BLE001 - Public service boundary maps every internal exception to an ErrorEnvelope.
            return error_envelope(exc, upstream="bedesten")

    async def _refresh_search(
        self, request: DecisionSearchRequest, key: str
    ) -> dict[str, Any]:
        cached = self._cache.get(_SEARCH_NAMESPACE, key)
        if cached is not None and cached.freshness is Freshness.FRESH:
            return _search_success_from_record(cached)
        headers = (
            conditional_headers(cached.etag, cached.last_modified) if cached else {}
        )
        try:
            result = await self._provider.search(request, conditional_headers=headers)
        except UpstreamUnavailable as exc:
            if (
                cached is not None
                and cached.freshness is Freshness.EXPIRED
                and exc.stale_eligible
                and (exc.status_code is None or exc.status_code >= 500)
                and not isinstance(exc, ProviderProtocolError)
            ):
                return _search_success_from_record(cached, revalidation_error=exc)
            raise
        if result.not_modified:
            if cached is None:
                raise ProviderProtocolError(
                    "Bedesten, önbelleğe alınmış karar araması olmadan 304 yanıtı "
                    "döndürdü."
                )
            _search_success_from_record(cached)
            revalidation_metadata: dict[str, object] = {
                "expires_at": utc_now() + SEARCH_CACHE_TTL,
            }
            if result.etag is not None:
                revalidation_metadata["etag"] = result.etag
            if result.last_modified is not None:
                revalidation_metadata["last_modified"] = result.last_modified
            updated = self._cache.mark_validated(
                _SEARCH_NAMESPACE,
                key,
                validated_at=utc_now(),
                **revalidation_metadata,
            )
            return _search_success_from_record(updated)
        return self._store_search_result(key, result)

    def _store_search_result(
        self, key: str, result: BedestenSearchResult
    ) -> dict[str, Any]:
        now = utc_now()
        content = json.dumps(
            {
                "hits": [hit.model_dump(mode="json") for hit in result.hits],
                "page": result.page.model_dump(mode="json"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        record = self._cache.put(
            namespace=_SEARCH_NAMESPACE,
            key=key,
            source_url=SEARCH_URL,
            content=content,
            fetched_at=now,
            validated_at=now,
            expires_at=now + SEARCH_CACHE_TTL,
            etag=result.etag,
            last_modified=result.last_modified,
            mime_type="application/json",
        )
        return _search_success_from_record(record)

    async def _refresh_document(
        self,
        opaque_id: str,
        canonical_id: str,
        *,
        cached: CacheRecord | None,
    ) -> CacheRecord:
        headers = (
            conditional_headers(cached.etag, cached.last_modified) if cached else {}
        )
        payload = await self._provider.get_document(
            opaque_id, conditional_headers=headers
        )
        if payload.not_modified:
            if cached is None:
                raise ProviderProtocolError(
                    "Bedesten, önbelleğe alınmış belge olmadan 304 yanıtı döndürdü."
                )
            _document_success_from_record(
                cached,
                canonical_id,
                1,
                page_target_chars=self._document_page_target_chars,
            )
            revalidation_metadata: dict[str, object] = {"expires_at": None}
            if payload.etag is not None:
                revalidation_metadata["etag"] = payload.etag
            if payload.last_modified is not None:
                revalidation_metadata["last_modified"] = payload.last_modified
            updated = self._cache.mark_validated(
                _DOCUMENT_NAMESPACE,
                canonical_id,
                validated_at=utc_now(),
                **revalidation_metadata,
            )
            return updated
        converted = await convert_document(
            payload.data,
            payload.mime_type,
            payload.source_url,
            ocr=self._ocr,
        )
        now = utc_now()
        record = self._cache.put(
            namespace=_DOCUMENT_NAMESPACE,
            key=canonical_id,
            source_url=payload.source_url,
            content=converted.markdown,
            content_hash=converted.content_hash,
            fetched_at=now,
            validated_at=now,
            expires_at=None,
            etag=payload.etag,
            last_modified=payload.last_modified,
            mime_type=converted.mime_type,
            conversion=converted.conversion,
        )
        return record


def _parse_search_request(
    request: DecisionSearchRequest | Mapping[str, Any] | None,
    parameters: Mapping[str, Any],
) -> DecisionSearchRequest:
    if isinstance(request, DecisionSearchRequest):
        if parameters:
            raise InvalidParamsError(
                {
                    "request": (
                        "request, anahtar sözcük parametreleriyle birlikte kullanılamaz.",
                    )
                }
            )
        return request
    payload: dict[str, Any] = {}
    if request is not None:
        if not isinstance(request, Mapping):
            raise InvalidParamsError({"request": ("bir nesne olmalıdır.",)})
        payload.update(request)
    payload.update(parameters)
    return DecisionSearchRequest.model_validate(payload)


def _parse_bedesten_document_id(document_id: str) -> tuple[str, str]:
    if not isinstance(document_id, str):
        raise InvalidParamsError({"id": ("bir Bedesten belge kimliği olmalıdır.",)})
    try:
        parsed = parse_document_id(document_id)
    except InvalidDocumentId as exc:
        raise InvalidParamsError({"id": (str(exc),)}) from exc
    if not isinstance(parsed, BedestenId):
        raise InvalidParamsError({"id": ("bir Bedesten belge kimliği olmalıdır.",)})
    return parsed.opaque, parsed.format()


def _search_success_from_record(
    record: CacheRecord,
    *,
    revalidation_error: UpstreamUnavailable | None = None,
) -> dict[str, Any]:
    try:
        if not isinstance(record.content, str) or not record.content:
            raise TypeError("Önbellek içeriği boş veya metin değil.")
        payload = json.loads(record.content)
        hits = tuple(DecisionHit.model_validate(hit) for hit in payload["hits"])
        page = PageInfo.model_validate(payload["page"])
        if not isinstance(
            record.content_hash, str
        ) or record.content_hash != _content_hash(record.content):
            raise ValueError("Önbellek içerik karması eşleşmiyor.")
    except (
        KeyError,
        TypeError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ) as exc:
        raise ProviderProtocolError(
            "Önbellekteki Bedesten karar arama kaydı geçersiz."
        ) from exc
    if revalidation_error is None:
        success = DecisionSearchSuccess(ok=True, hits=hits, page=page, warnings=())
    else:
        success = DecisionSearchSuccess(
            ok=True,
            hits=hits,
            page=page,
            warnings=(stale_warning(),),
            fetched_at=record.fetched_at,
            validated_at=record.validated_at,
            expires_at=record.expires_at,
            revalidation_error=unavailable_revalidation_error(revalidation_error),
        )
    # Preserve nullable hit fields; only stale-response metadata is optional.
    payload = success.model_dump(mode="json")
    for field in (
        "fetched_at",
        "validated_at",
        "expires_at",
        "revalidation_error",
    ):
        if payload.get(field) is None:
            payload.pop(field, None)
    return payload


def _document_success_from_record(
    record: CacheRecord,
    canonical_id: str,
    requested_page: int,
    *,
    page_target_chars: int,
) -> dict[str, Any]:
    _validate_document_record(record)
    conversion = record.conversion
    assert conversion is not None
    pages = _markdown_pages(record.content, target_chars=page_target_chars)
    if requested_page > len(pages):
        raise ChunkOutOfRangeError(requested_page, len(pages))
    markdown = pages[requested_page - 1]
    page = PageInfo(
        page=requested_page,
        page_size=len(markdown),
        total_records=1,
        total_pages=len(pages),
        has_more=requested_page < len(pages),
    )
    success = DocumentSuccess(
        ok=True,
        id=canonical_id,
        source="bedesten",
        source_url=record.source_url,
        markdown=markdown,
        mime_type=record.mime_type,
        conversion=conversion,
        content_hash=record.content_hash,
        page=page,
        warnings=(),
        fetched_at=record.fetched_at,
        validated_at=record.validated_at,
        expires_at=None,
    )
    payload = success.model_dump(mode="json")
    if payload["revalidation_error"] is None:
        payload.pop("revalidation_error")
    return payload


def _validate_document_record(record: CacheRecord) -> None:
    if record.conversion is None:
        raise ProviderProtocolError(
            "Önbelleğe alınmış Bedesten belgesinde dönüşüm metaverisi yok."
        )
    if not isinstance(record.content, str) or not record.content:
        raise ProviderProtocolError(
            "Önbelleğe alınmış Bedesten belge içeriği geçersiz."
        )
    if not isinstance(record.content_hash, str) or record.content_hash != _content_hash(
        record.content
    ):
        raise ProviderProtocolError(
            "Önbelleğe alınmış Bedesten belge içerik doğrulamasını geçemedi."
        )


def _content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _markdown_pages(markdown: str, *, target_chars: int) -> tuple[str, ...]:
    """Chunk only at legal article and paragraph boundaries, never raw offsets."""
    return chunk_markdown(markdown, max_chars=target_chars)
