"""Cached V1 legislation operations over the Bedesten provider."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from math import ceil
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError

from mutalaamcp.cache.singleflight import SingleFlight
from mutalaamcp.cache.store import CacheRecord, CacheStore, Freshness
from mutalaamcp.conversion.documents import convert_document
from mutalaamcp.domain.articles import (
    Article,
    chunk_markdown,
    extract_outline_articles,
    find_article,
    normalize_article_number,
    normalize_outline,
    outline_from_markdown,
    outline_needs_titles,
)
from mutalaamcp.domain.boolean import (
    BooleanQuery,
    BooleanQueryError,
    parse_boolean_query,
    turkish_fold,
)
from mutalaamcp.domain.ids import InvalidDocumentId, MevzuatId, parse_document_id
from mutalaamcp.domain.models import (
    AbsoluteUri,
    ConversionMethod,
    FrozenModel,
    LegislationHit,
    LegislationType,
    PageInfo,
    RevalidationError,
    ToolWarning,
)
from mutalaamcp.net import UpstreamNotFound, UpstreamUnavailable
from mutalaamcp.providers.bedesten.legislation import (
    BedestenLegislationProvider,
    LegislationNotFound,
    LegislationProviderError,
    ProviderDocument,
    ProviderOutline,
    SearchPage,
    _validate_title,
)
from mutalaamcp.services.common import (
    SEARCH_CACHE_TTL,
    DocumentSuccess,
    _StaleAwareSuccess,
    canonical_parameter_hash,
    conditional_headers,
    error_envelope,
    stale_warning,
    unavailable_revalidation_error,
    utc_now,
)

_DOCUMENT_PAGE_CHARS = 12_000
_SEARCH_NAMESPACE = "bedesten_legislation_search"
_DOCUMENT_NAMESPACE = "bedesten_legislation_document"
_ARTICLE_NAMESPACE = "bedesten_legislation_article"
_OUTLINE_NAMESPACE = "bedesten_legislation_outline"
_SEARCH_SOURCE_URL = "https://bedesten.adalet.gov.tr/mevzuat/searchDocuments"


class LegislationRequestError(ValueError):
    """A non-Pydantic request failure that maps to ``invalid_params``."""

    code = "invalid_params"

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field_errors = {field: (message,)}


class _LocalizedValidationError(ValueError):
    """Pydantic validation errors normalized for the public Turkish envelope."""

    code = "invalid_params"

    def __init__(self, field_errors: Mapping[str, tuple[str, ...]]) -> None:
        super().__init__("")
        self.field_errors = field_errors


class ChunkOutOfRangeError(ValueError):
    """A requested article/document page has no corresponding chunk."""

    code = "chunk_out_of_range"


class SearchLegislationRequest(FrozenModel):
    query: str | None = Field(default=None, min_length=1)
    title: str | None = Field(default=None, min_length=1)
    number: str | None = Field(default=None, min_length=1)
    types: tuple[LegislationType, ...] | None = None
    exact_title: bool = False
    gazette_date_from: date | None = None
    gazette_date_to: date | None = None
    gazette_issue: str | None = Field(default=None, min_length=1)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=20)
    include_snippet: bool = False

    @field_validator("title")
    @classmethod
    def _title_uses_bedesten_dialect(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return value
        try:
            return _validate_title(value)
        except ValueError as exc:
            raise PydanticCustomError(
                "invalid_legislation_title", "{message}", {"message": str(exc)}
            ) from exc

    @field_validator("types")
    @classmethod
    def _types_are_unique(
        cls, value: tuple[LegislationType, ...] | None
    ) -> tuple[LegislationType, ...] | None:
        if value is not None and len(set(value)) != len(value):
            raise PydanticCustomError(
                "duplicate_legislation_types",
                "types alanı yinelenen değerler içermemelidir.",
            )
        return value

    @model_validator(mode="after")
    def _search_contract(self) -> SearchLegislationRequest:
        if not any((self.query, self.title, self.number)):
            raise PydanticCustomError(
                "search_selector_required",
                "query, title veya number alanlarından biri gereklidir.",
            )
        if self.exact_title and not self.title:
            raise PydanticCustomError(
                "exact_title_requires_title",
                "exact_title için title gereklidir.",
            )
        if (
            self.gazette_date_from
            and self.gazette_date_to
            and self.gazette_date_from > self.gazette_date_to
        ):
            raise PydanticCustomError(
                "gazette_date_order",
                "gazette_date_from, gazette_date_to değerinden sonra olamaz.",
            )
        return self


class _LegislationIdRequest(FrozenModel):
    legislation_id: str = Field(min_length=1)

    @field_validator("legislation_id")
    @classmethod
    def _must_be_mevzuat_id(cls, value: str) -> str:
        try:
            parsed = parse_document_id(value)
        except InvalidDocumentId as exc:
            raise PydanticCustomError(
                "invalid_legislation_id", "{message}", {"message": str(exc)}
            ) from exc
        if not isinstance(parsed, MevzuatId):
            raise PydanticCustomError(
                "invalid_legislation_id",
                "legislation_id bir mevzuat belge kimliği olmalıdır.",
            )
        return parsed.format()


class GetLegislationRequest(_LegislationIdRequest):
    page: int = Field(default=1, ge=1)
    refresh: bool = False


class GetOutlineRequest(_LegislationIdRequest):
    refresh: bool = False


class GetArticleRequest(GetLegislationRequest):
    article_number: str = Field(min_length=1)

    @field_validator("article_number")
    @classmethod
    def _article_number_is_published_label(cls, value: str) -> str:
        try:
            return normalize_article_number(value)
        except (TypeError, ValueError) as exc:
            raise PydanticCustomError(
                "invalid_article_number", "{message}", {"message": str(exc)}
            ) from exc


class WithinLegislationRequest(_LegislationIdRequest):
    query: str = Field(min_length=1, max_length=500)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=50)
    include_snippet: bool = True
    refresh: bool = False


class LegislationSearchSuccess(_StaleAwareSuccess):
    ok: Literal[True] = True
    hits: tuple[LegislationHit, ...]
    page: PageInfo
    expires_at: datetime | None = None


class WithinHit(FrozenModel):
    article_number: str = Field(min_length=1)
    title: str | None = None
    snippet: str | None = None
    match_count: int = Field(ge=1)


class WithinSearchSuccess(_StaleAwareSuccess):
    ok: Literal[True] = True
    legislation_id: str
    hits: tuple[WithinHit, ...]
    page: PageInfo
    expires_at: datetime | None = None


class ArticleSuccess(_StaleAwareSuccess):
    ok: Literal[True] = True
    legislation_id: str
    article_number: str
    title: str | None = None
    source: Literal["bedesten"] = "bedesten"
    source_url: AbsoluteUri
    markdown: str
    mime_type: str | None
    conversion: ConversionMethod
    content_hash: str
    page: PageInfo
    fetched_at: datetime
    validated_at: datetime
    expires_at: datetime


class OutlineSuccess(_StaleAwareSuccess):
    ok: Literal[True] = True
    legislation_id: str
    source: Literal["bedesten"] = "bedesten"
    nodes: tuple[dict[str, object], ...]
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _CachedPayload:
    record: CacheRecord
    stale_error: UpstreamUnavailable | None = None

    @property
    def warnings(self) -> tuple[ToolWarning, ...]:
        return (stale_warning(),) if self.stale_error is not None else ()

    @property
    def revalidation_error(self) -> RevalidationError | None:
        if self.stale_error is None:
            return None
        return unavailable_revalidation_error(self.stale_error)


class LegislationService:
    """Service boundary for Bedesten legislation search and retrieval.

    Every cache entry stores normalized output; request cache keys are hashes of
    canonical parameters, so neither raw queries nor original document bytes
    are retained as cache keys.
    """

    def __init__(
        self,
        provider: BedestenLegislationProvider,
        cache: CacheStore,
        singleflight: SingleFlight[_CachedPayload] | None = None,
        ocr: Any | None = None,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._singleflight = singleflight or SingleFlight()
        self._ocr = ocr

    async def search(
        self,
        request: SearchLegislationRequest | None = None,
        /,
        **params: object,
    ) -> dict[str, object]:
        try:
            candidate = _request_model(SearchLegislationRequest, request, params)
            cached = await self._load_search(candidate)
            return _serialize_success(self._search_success(cached))
        except Exception as error:  # noqa: BLE001 - public service boundary maps all failures to ErrorEnvelope
            return _map_error(error, search_endpoint=True)

    async def get_document(
        self,
        legislation_id: str,
        *,
        page: int = 1,
        refresh: bool = False,
    ) -> dict[str, object]:
        try:
            request = GetLegislationRequest(
                legislation_id=legislation_id, page=page, refresh=refresh
            )
            payload = await self._load_document(
                _opaque_id(request.legislation_id), refresh=request.refresh
            )
            return _serialize_success(self._document_success(request, payload))
        except Exception as error:  # noqa: BLE001 - public service boundary maps all failures to ErrorEnvelope
            return _map_error(error)

    async def get_article(
        self,
        legislation_id: str,
        article_number: str,
        *,
        page: int = 1,
        refresh: bool = False,
    ) -> dict[str, object]:
        try:
            request = GetArticleRequest(
                legislation_id=legislation_id,
                article_number=article_number,
                page=page,
                refresh=refresh,
            )
            opaque = _opaque_id(request.legislation_id)
            outline = await self._load_outline(opaque, refresh=request.refresh)
            upstream_article_ids = {
                entry.article_id
                for entry in extract_outline_articles(_json_payload(outline.record))
                if entry.number == request.article_number and entry.article_id
            }
            upstream_article_id = (
                next(iter(upstream_article_ids))
                if len(upstream_article_ids) == 1
                else None
            )
            if upstream_article_id is None:
                document = await self._load_document(opaque, refresh=request.refresh)
                article = find_article(document.record.content, request.article_number)
                if article is None:
                    raise UpstreamNotFound("İstenen madde bulunamadı.")
                return _serialize_success(
                    self._article_from_document(request, document, article)
                )
            payload = await self._load_article(
                opaque,
                request.article_number,
                upstream_article_id,
                refresh=request.refresh,
            )
            article = find_article(
                payload.record.content, request.article_number
            ) or Article(
                number=request.article_number,
                title=None,
                markdown=payload.record.content,
            )
            return _serialize_success(
                self._article_from_document(request, payload, article)
            )
        except Exception as error:  # noqa: BLE001 - public service boundary maps all failures to ErrorEnvelope
            return _map_error(error)

    async def search_in_legislation(
        self,
        legislation_id: str,
        query: str,
        *,
        page: int = 1,
        page_size: int = 25,
        include_snippet: bool = True,
        refresh: bool = False,
    ) -> dict[str, object]:
        try:
            request = WithinLegislationRequest(
                legislation_id=legislation_id,
                query=query,
                page=page,
                page_size=page_size,
                include_snippet=include_snippet,
                refresh=refresh,
            )
            expression = _parse_query(request.query)
            payload = await self._load_document(
                _opaque_id(request.legislation_id), refresh=request.refresh
            )
            hits = tuple(
                WithinHit(
                    article_number=article.number,
                    title=article.title,
                    snippet=_article_snippet(article, expression)
                    if request.include_snippet
                    else None,
                    match_count=expression.match_count(article.markdown),
                )
                for article in _matching_articles(payload.record.content, expression)
            )
            total = len(hits)
            total_pages = ceil(total / request.page_size) if total else 0
            start = (request.page - 1) * request.page_size
            return _serialize_success(
                WithinSearchSuccess(
                    legislation_id=request.legislation_id,
                    hits=hits[start : start + request.page_size],
                    page=PageInfo(
                        page=request.page,
                        page_size=request.page_size,
                        total_records=total,
                        total_pages=total_pages,
                        has_more=request.page < total_pages,
                    ),
                    expires_at=payload.record.expires_at,
                    warnings=payload.warnings,
                    fetched_at=(
                        payload.record.fetched_at if payload.stale_error else None
                    ),
                    validated_at=(
                        payload.record.validated_at if payload.stale_error else None
                    ),
                    revalidation_error=payload.revalidation_error,
                )
            )
        except Exception as error:  # noqa: BLE001 - public service boundary maps all failures to ErrorEnvelope
            return _map_error(error)

    async def get_outline(
        self,
        legislation_id: str,
        *,
        refresh: bool = False,
    ) -> dict[str, object]:
        try:
            request = GetOutlineRequest(legislation_id=legislation_id, refresh=refresh)
            payload = await self._load_outline(
                _opaque_id(request.legislation_id), refresh=request.refresh
            )
            raw_outline = _json_payload(payload.record)
            nodes = normalize_outline(raw_outline)
            warnings = payload.warnings
            if outline_needs_titles(raw_outline):
                # A derived tree must carry the source document's freshness and
                # warnings, not pretend to be newer than the text it came from.
                document = await self._load_document(
                    _opaque_id(request.legislation_id), refresh=request.refresh
                )
                derived = outline_from_markdown(document.record.content)
                if derived:
                    nodes = derived
                    payload = document
                    warnings = document.warnings + (
                        ToolWarning(
                            code="outline_from_document",
                            message="Bölüm ve madde ağacı tam metindeki açık başlıklardan oluşturuldu.",
                        ),
                    )
                else:
                    warnings = warnings + (
                        ToolWarning(
                            code="outline_incomplete",
                            message="Üst kaynak ağacında başlıklar eksik; tam metinden güvenilir bölüm ağacı oluşturulamadı.",
                        ),
                    )
            return _serialize_success(
                OutlineSuccess(
                    legislation_id=request.legislation_id,
                    nodes=tuple(nodes),
                    expires_at=payload.record.expires_at,
                    warnings=warnings,
                    fetched_at=(
                        payload.record.fetched_at if payload.stale_error else None
                    ),
                    validated_at=(
                        payload.record.validated_at if payload.stale_error else None
                    ),
                    revalidation_error=payload.revalidation_error,
                )
            )
        except Exception as error:  # noqa: BLE001 - public service boundary maps all failures to ErrorEnvelope
            return _map_error(error)

    async def _load_search(self, request: SearchLegislationRequest) -> _CachedPayload:
        parameters = request.model_dump(mode="json")
        key = canonical_parameter_hash(parameters, unordered_fields=("types",))
        cached = self._cache.get(_SEARCH_NAMESPACE, key, now=utc_now())
        if cached is not None and cached.freshness is Freshness.FRESH:
            _validate_search_record(cached)
            return _CachedPayload(cached)
        return await self._singleflight.do(
            (_SEARCH_NAMESPACE, key),
            lambda: self._refresh_search(request, key, cached),
        )

    async def _refresh_search(
        self,
        request: SearchLegislationRequest,
        key: str,
        cached: CacheRecord | None,
    ) -> _CachedPayload:
        try:
            result = await self._provider.search(
                **request.model_dump(),
                conditional_headers=conditional_headers(
                    cached.etag if cached else None,
                    cached.last_modified if cached else None,
                ),
            )
            if result is None or result.not_modified:
                if cached is None:
                    raise LegislationProviderError(
                        "Bedesten, önbelleğe alınmış arama olmadan 304 yanıtı döndürdü."
                    )
                _validate_search_record(cached)
                return _CachedPayload(
                    self._mark_validated(
                        _SEARCH_NAMESPACE,
                        key,
                        cached,
                        result.etag if result is not None else None,
                        result.last_modified if result is not None else None,
                    )
                )
            content = _serialize_search(result)
            if cached is not None and _same_json(cached.content, content):
                _validate_search_record(cached)
                return _CachedPayload(
                    self._mark_validated(
                        _SEARCH_NAMESPACE,
                        key,
                        cached,
                        result.etag,
                        result.last_modified,
                    )
                )
            moment = utc_now()
            record = self._cache.put(
                namespace=_SEARCH_NAMESPACE,
                key=key,
                source_url=_SEARCH_SOURCE_URL,
                content=content,
                fetched_at=moment,
                validated_at=moment,
                expires_at=moment + SEARCH_CACHE_TTL,
                etag=result.etag,
                last_modified=result.last_modified,
            )
            return _CachedPayload(record)
        except UpstreamUnavailable as error:
            if (
                cached is not None
                and cached.freshness is Freshness.EXPIRED
                and _stale_eligible(error)
            ):
                _validate_search_record(cached)
                return _CachedPayload(cached, error)
            raise

    async def _load_document(self, opaque: str, *, refresh: bool) -> _CachedPayload:
        return await self._load_converted(
            namespace=_DOCUMENT_NAMESPACE,
            key=opaque,
            refresh=refresh,
            fetch=lambda headers: self._provider.get_document(
                opaque, conditional_headers=headers
            ),
        )

    async def _load_article(
        self,
        opaque: str,
        article_number: str,
        upstream_article_id: str,
        *,
        refresh: bool,
    ) -> _CachedPayload:
        key = canonical_parameter_hash(
            {"legislation_id": opaque, "article_number": article_number}
        )
        return await self._load_converted(
            namespace=_ARTICLE_NAMESPACE,
            key=key,
            refresh=refresh,
            fetch=lambda headers: self._provider.get_article(
                upstream_article_id,
                legislation_id=opaque,
                conditional_headers=headers,
            ),
        )

    async def _load_converted(
        self,
        *,
        namespace: str,
        key: str,
        refresh: bool,
        fetch: Any,
    ) -> _CachedPayload:
        cached = self._cache.get(namespace, key, now=utc_now())
        if not refresh and cached is not None and cached.freshness is Freshness.FRESH:
            _validate_converted_record(cached)
            return _CachedPayload(cached)

        async def materialize() -> _CachedPayload:
            try:
                remote: ProviderDocument = await fetch(
                    conditional_headers(
                        cached.etag if cached else None,
                        cached.last_modified if cached else None,
                    )
                )
                if remote.not_modified:
                    if cached is None:
                        raise LegislationProviderError(
                            "Bedesten, önbelleğe alınmış içerik olmadan 304 yanıtı "
                            "döndürdü."
                        )
                    _validate_converted_record(cached)
                    return _CachedPayload(
                        self._mark_validated(
                            namespace, key, cached, remote.etag, remote.last_modified
                        )
                    )
                if remote.content is None or remote.mime_type is None:
                    raise LegislationProviderError("Bedesten içerik yanıtı eksik.")
                if self._ocr is None:
                    converted = await convert_document(
                        remote.content, remote.mime_type, remote.source_url
                    )
                else:
                    converted = await convert_document(
                        remote.content,
                        remote.mime_type,
                        remote.source_url,
                        ocr=self._ocr,
                    )
                if cached is not None and converted.content_hash == cached.content_hash:
                    _validate_converted_record(cached)
                    return _CachedPayload(
                        self._mark_validated(
                            namespace, key, cached, remote.etag, remote.last_modified
                        )
                    )
                moment = utc_now()
                return _CachedPayload(
                    self._cache.put(
                        namespace=namespace,
                        key=key,
                        source_url=remote.source_url,
                        content=converted.markdown,
                        content_hash=converted.content_hash,
                        fetched_at=moment,
                        validated_at=moment,
                        expires_at=moment + SEARCH_CACHE_TTL,
                        etag=remote.etag,
                        last_modified=remote.last_modified,
                        mime_type=converted.mime_type,
                        conversion=converted.conversion,
                    )
                )
            except UpstreamUnavailable as error:
                if (
                    not refresh
                    and cached is not None
                    and cached.freshness is Freshness.EXPIRED
                    and _stale_eligible(error)
                ):
                    _validate_converted_record(cached)
                    return _CachedPayload(cached, error)
                raise

        return await self._singleflight.do((namespace, key, refresh), materialize)

    async def _load_outline(self, opaque: str, *, refresh: bool) -> _CachedPayload:
        cached = self._cache.get(_OUTLINE_NAMESPACE, opaque, now=utc_now())
        if not refresh and cached is not None and cached.freshness is Freshness.FRESH:
            _validate_outline_record(cached)
            return _CachedPayload(cached)

        async def materialize() -> _CachedPayload:
            try:
                remote: (
                    ProviderOutline | tuple[object, str | None, str | None] | None
                ) = await self._provider.get_outline(
                    opaque,
                    conditional_headers=conditional_headers(
                        cached.etag if cached else None,
                        cached.last_modified if cached else None,
                    ),
                )
                if remote is None or (
                    isinstance(remote, ProviderOutline) and remote.not_modified
                ):
                    if cached is None:
                        raise LegislationProviderError(
                            "Bedesten, önbelleğe alınmış madde ağacı olmadan 304 "
                            "yanıtı döndürdü."
                        )
                    _validate_outline_record(cached)
                    return _CachedPayload(
                        self._mark_validated(
                            _OUTLINE_NAMESPACE,
                            opaque,
                            cached,
                            remote.etag
                            if isinstance(remote, ProviderOutline)
                            else None,
                            remote.last_modified
                            if isinstance(remote, ProviderOutline)
                            else None,
                        )
                    )
                if isinstance(remote, ProviderOutline):
                    tree, etag, last_modified = (
                        remote.tree,
                        remote.etag,
                        remote.last_modified,
                    )
                else:
                    tree, etag, last_modified = remote
                if tree is None:
                    tree = []
                content = json.dumps(
                    tree, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                _deserialize_outline(content)
                if cached is not None and _same_json(cached.content, content):
                    _validate_outline_record(cached)
                    return _CachedPayload(
                        self._mark_validated(
                            _OUTLINE_NAMESPACE, opaque, cached, etag, last_modified
                        )
                    )
                moment = utc_now()
                return _CachedPayload(
                    self._cache.put(
                        namespace=_OUTLINE_NAMESPACE,
                        key=opaque,
                        source_url=_source_url(opaque),
                        content=content,
                        fetched_at=moment,
                        validated_at=moment,
                        expires_at=moment + SEARCH_CACHE_TTL,
                        etag=etag,
                        last_modified=last_modified,
                    )
                )
            except UpstreamUnavailable as error:
                if (
                    not refresh
                    and cached is not None
                    and cached.freshness is Freshness.EXPIRED
                    and _stale_eligible(error)
                ):
                    _validate_outline_record(cached)
                    return _CachedPayload(cached, error)
                raise

        return await self._singleflight.do(
            (_OUTLINE_NAMESPACE, opaque, refresh), materialize
        )

    def _mark_validated(
        self,
        namespace: str,
        key: str,
        cached: CacheRecord,
        etag: str | None,
        last_modified: str | None,
    ) -> CacheRecord:
        moment = utc_now()
        return self._cache.mark_validated(
            namespace,
            key,
            validated_at=moment,
            expires_at=moment + SEARCH_CACHE_TTL,
            etag=etag if etag is not None else cached.etag,
            last_modified=last_modified
            if last_modified is not None
            else cached.last_modified,
        )

    def _search_success(self, payload: _CachedPayload) -> LegislationSearchSuccess:
        result = _deserialize_search(payload.record.content)
        return LegislationSearchSuccess(
            hits=result.hits,
            page=result.page,
            expires_at=payload.record.expires_at,
            warnings=payload.warnings,
            fetched_at=payload.record.fetched_at if payload.stale_error else None,
            validated_at=payload.record.validated_at if payload.stale_error else None,
            revalidation_error=payload.revalidation_error,
        )

    def _document_success(
        self, request: GetLegislationRequest, payload: _CachedPayload
    ) -> DocumentSuccess:
        _validate_converted_record(payload.record)
        markdown, page = _page_markdown(payload.record.content, request.page)
        conversion = payload.record.conversion
        if conversion is None:
            raise LegislationProviderError(
                "Önbelleğe alınmış belgede dönüşüm metaverisi yok."
            )
        return DocumentSuccess(
            id=request.legislation_id,
            source="bedesten",
            source_url=payload.record.source_url,
            markdown=markdown,
            mime_type=payload.record.mime_type,
            conversion=conversion,
            content_hash=payload.record.content_hash,
            page=page,
            fetched_at=payload.record.fetched_at,
            validated_at=payload.record.validated_at,
            expires_at=payload.record.expires_at,
            warnings=payload.warnings,
            revalidation_error=payload.revalidation_error,
        )

    def _article_from_document(
        self,
        request: GetArticleRequest,
        payload: _CachedPayload,
        article: Article,
    ) -> ArticleSuccess:
        _validate_converted_record(payload.record)
        markdown, page = _page_markdown(article.markdown, request.page)
        conversion = payload.record.conversion
        if conversion is None or payload.record.expires_at is None:
            raise LegislationProviderError(
                "Önbelleğe alınmış maddede dönüşüm metaverisi eksik."
            )
        return ArticleSuccess(
            legislation_id=request.legislation_id,
            article_number=article.number,
            title=article.title,
            source_url=payload.record.source_url,
            markdown=markdown,
            mime_type=payload.record.mime_type,
            conversion=conversion,
            page=page,
            fetched_at=payload.record.fetched_at,
            validated_at=payload.record.validated_at,
            expires_at=payload.record.expires_at,
            warnings=payload.warnings,
            revalidation_error=payload.revalidation_error,
            content_hash=_content_hash(article.markdown),
        )


def _request_model[T: FrozenModel](
    model: type[T],
    request: T | None,
    params: Mapping[str, object],
) -> T:
    if request is not None:
        if params:
            raise LegislationRequestError(
                "request",
                "request, anahtar sözcük parametreleriyle birlikte kullanılamaz.",
            )
        return request
    return model.model_validate(params)


def _serialize_success(success: FrozenModel) -> dict[str, object]:
    """Preserve required nullable fields while omitting absent stale metadata."""
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


def _opaque_id(legislation_id: str) -> str:
    parsed = parse_document_id(legislation_id)
    if not isinstance(parsed, MevzuatId):
        raise LegislationRequestError(
            "legislation_id", "legislation_id bir mevzuat belge kimliği olmalıdır."
        )
    return parsed.opaque


def _parse_query(query: str) -> BooleanQuery:
    try:
        return parse_boolean_query(query)
    except BooleanQueryError as error:
        raise LegislationRequestError("query", str(error)) from error


def _serialize_search(result: SearchPage) -> str:
    return json.dumps(
        {
            "hits": [hit.model_dump(mode="json") for hit in result.hits],
            "page": result.page.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _deserialize_search(content: str) -> SearchPage:
    try:
        payload = json.loads(content)
        if not isinstance(payload, Mapping):
            raise TypeError("Önbellek verisi bir nesne değil.")
        raw_hits = payload["hits"]
        raw_page = payload["page"]
        if not isinstance(raw_hits, list) or not isinstance(raw_page, Mapping):
            raise TypeError("Önbellek verisi hatalı biçimlendirilmiş.")
        return SearchPage(
            hits=tuple(LegislationHit.model_validate(hit) for hit in raw_hits),
            page=PageInfo.model_validate(raw_page),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise LegislationProviderError(
            "Önbellekteki mevzuat arama verisi hatalı biçimlendirilmiş."
        ) from error


def _validate_search_record(record: CacheRecord) -> SearchPage:
    """Reject search cache rows whose body, hash, or structured payload is invalid."""
    if not isinstance(record.content, str):
        raise LegislationProviderError("Önbellekteki mevzuat arama içeriği geçersiz.")
    if not isinstance(record.content_hash, str) or record.content_hash != _content_hash(
        record.content
    ):
        raise LegislationProviderError(
            "Önbellekteki mevzuat arama içerik doğrulamasını geçemedi."
        )
    return _deserialize_search(record.content)


def _validate_converted_record(record: CacheRecord) -> None:
    """Reject cache records that cannot safely represent converted Markdown."""
    if not isinstance(record.content, str) or not record.content:
        raise LegislationProviderError("Önbelleğe alınmış belge içeriği geçersiz.")
    if record.conversion is None:
        raise LegislationProviderError(
            "Önbelleğe alınmış belgede dönüşüm metaverisi yok."
        )
    if not isinstance(record.content_hash, str) or record.content_hash != _content_hash(
        record.content
    ):
        raise LegislationProviderError(
            "Önbelleğe alınmış belge içerik doğrulamasını geçemedi."
        )


def _deserialize_outline(content: str) -> object:
    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as error:
        raise LegislationProviderError(
            "Önbellekteki mevzuat madde ağacı verisi hatalı biçimlendirilmiş."
        ) from error
    if not isinstance(payload, (Mapping, list)):
        raise LegislationProviderError(
            "Önbellekteki mevzuat madde ağacı verisi hatalı biçimlendirilmiş."
        )
    return payload


def _validate_outline_record(record: CacheRecord) -> object:
    """Reject outline cache rows whose body, hash, or JSON shape is invalid."""
    if not isinstance(record.content, str):
        raise LegislationProviderError(
            "Önbellekteki mevzuat madde ağacı içeriği geçersiz."
        )
    if not isinstance(record.content_hash, str) or record.content_hash != _content_hash(
        record.content
    ):
        raise LegislationProviderError(
            "Önbellekteki mevzuat madde ağacı içerik doğrulamasını geçemedi."
        )
    return _deserialize_outline(record.content)


def _json_payload(record: CacheRecord) -> object:
    return _deserialize_outline(record.content)


def _page_markdown(markdown: str, requested_page: int) -> tuple[str, PageInfo]:
    chunks = chunk_markdown(markdown, max_chars=_DOCUMENT_PAGE_CHARS)
    if requested_page > len(chunks):
        raise ChunkOutOfRangeError(
            f"İstenen {requested_page}. sayfa, kullanılabilir belge parçalarının dışında."
        )
    chunk = chunks[requested_page - 1]
    return (
        chunk,
        PageInfo(
            page=requested_page,
            page_size=len(chunk),
            total_records=1,
            total_pages=len(chunks),
            has_more=requested_page < len(chunks),
        ),
    )


def _matching_articles(markdown: str, query: BooleanQuery) -> tuple[Article, ...]:
    return tuple(
        article
        for article in _article_source(markdown)
        if query.matches(article.markdown)
    )


def _content_hash(markdown: str) -> str:
    return "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _article_source(markdown: str) -> tuple[Article, ...]:
    from mutalaamcp.domain.articles import segment_articles

    return segment_articles(markdown)


def _article_snippet(article: Article, query: BooleanQuery) -> str:
    folded_article = turkish_fold(article.markdown)
    for literal in query.literals():
        index = folded_article.find(turkish_fold(literal))
        if index >= 0:
            start = max(0, index - 100)
            end = min(len(article.markdown), index + len(literal) + 180)
            return " ".join(article.markdown[start:end].split())
    return " ".join(article.markdown[:280].split())


def _same_json(left: str, right: str) -> bool:
    try:
        return json.loads(left) == json.loads(right)
    except (TypeError, ValueError):
        return left == right


def _source_url(opaque: str) -> str:
    return f"https://bedesten.adalet.gov.tr/mevzuat/mevzuatDetay/{opaque}"


def _map_error(error: Exception, *, search_endpoint: bool = False) -> dict[str, object]:
    if isinstance(error, ValidationError):
        return error_envelope(
            _localized_validation_error(error),
            upstream="bedesten",
            not_found_as_unavailable=search_endpoint,
        )
    if isinstance(error, LegislationRequestError):
        return error_envelope(error, upstream="bedesten")
    if isinstance(error, ChunkOutOfRangeError):
        return error_envelope(error, upstream="bedesten")
    if isinstance(error, LegislationNotFound):
        return error_envelope(
            UpstreamNotFound("İstenen belge üst kaynakta bulunamadı."),
            upstream="bedesten",
            not_found_as_unavailable=search_endpoint,
        )
    if isinstance(error, LegislationProviderError):
        return error_envelope(
            UpstreamUnavailable("Üst kaynak şu anda kullanılamıyor."),
            upstream="bedesten",
        )
    return error_envelope(
        error,
        upstream="bedesten",
        not_found_as_unavailable=search_endpoint,
    )


def _localized_validation_error(error: ValidationError) -> _LocalizedValidationError:
    field_errors: dict[str, list[str]] = {}
    for item in error.errors(include_url=False):
        location = item.get("loc", ())
        field = ".".join(str(part) for part in location) or "request"
        field_errors.setdefault(field, []).append(_localized_validation_message(item))
    return _LocalizedValidationError(
        {field: tuple(messages) for field, messages in field_errors.items()}
    )


def _localized_validation_message(item: Mapping[str, object]) -> str:
    error_type = str(item.get("type", ""))
    if error_type.startswith(
        (
            "invalid_legislation_title",
            "duplicate_legislation_types",
            "search_selector_required",
            "exact_title_requires_title",
            "gazette_date_order",
            "invalid_legislation_id",
            "invalid_article_number",
        )
    ):
        return str(item.get("msg", "Geçersiz değer."))

    context = item.get("ctx")
    if not isinstance(context, Mapping):
        context = {}
    if error_type == "missing":
        return "Alan gereklidir."
    if error_type == "string_too_short":
        return f"En az {context.get('min_length', 1)} karakter olmalıdır."
    if error_type == "greater_than_equal":
        return f"{context.get('ge')} veya daha büyük olmalıdır."
    if error_type == "less_than_equal":
        return f"{context.get('le')} veya daha küçük olmalıdır."
    if error_type == "enum":
        return "Geçerli bir mevzuat türü olmalıdır."
    if error_type == "string_type":
        return "Geçerli bir metin olmalıdır."
    if error_type in {"bool_parsing", "bool_type"}:
        return "Geçerli bir mantıksal değer olmalıdır."
    if error_type.startswith("date_"):
        return "Geçerli bir tarih olmalıdır."
    if error_type == "extra_forbidden":
        return "Bu parametre kabul edilmez."
    return "Geçersiz değer."


def _stale_eligible(error: UpstreamUnavailable) -> bool:
    return error.stale_eligible and (
        error.status_code is None or error.status_code >= 500
    )
