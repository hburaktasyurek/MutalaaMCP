"""Isolated Bedesten decision-provider and decision-service behavior tests."""

from __future__ import annotations

import asyncio
import base64
import json
from collections import deque
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from mutalaamcp.cache.store import CacheStore, Freshness
from mutalaamcp.domain.articles import chunk_markdown
from mutalaamcp.domain.models import Court, DecisionHit, PageInfo
from mutalaamcp.net import (
    SafeHttpClient,
    UpstreamNotFound,
    UpstreamRateLimited,
    UpstreamUnavailable,
)
from mutalaamcp.providers.bedesten import decisions as bedesten_module
from mutalaamcp.providers.bedesten.decisions import (
    DOCUMENT_URL,
    SEARCH_URL,
    BedestenDecisionProvider,
    BedestenDocumentPayload,
    BedestenSearchResult,
    DecisionSearchRequest,
    ProviderProtocolError,
    RollingWindowRateLimiter,
)
from mutalaamcp.services.common import canonical_parameter_hash
from mutalaamcp.services.decisions import DecisionService, _markdown_pages
from mutalaamcp.tool_schemas import standalone_tool_schema

_DOCUMENT_ID = "fixture-decision-001"
_DOCUMENT_SOURCE_URL = f"https://mevzuat.adalet.gov.tr/ictihat/{_DOCUMENT_ID}"
_SEARCH_NAMESPACE = "search_decisions:v1"
_DOCUMENT_NAMESPACE = "document:bedesten:v1"


_SANITIZED_DOCUMENT_HTML = (
    "<p>MADDE 1 - Birinci arındırılmış karar paragrafı.</p>"
    "<p>MADDE 2 - İkinci arındırılmış karar paragrafı.</p>"
)
_SANITIZED_DOCUMENT_BASE64 = base64.b64encode(
    _SANITIZED_DOCUMENT_HTML.encode("utf-8")
).decode("ascii")


def _live_shaped_search_payload(*, item_type: str) -> dict[str, Any]:
    return {
        "metadata": {"FMTY": "SUCCESS"},
        "data": {
            "emsalKararList": [
                {
                    "documentId": _DOCUMENT_ID,
                    "itemType": {
                        "name": item_type,
                        "description": "Arındırılmış karar türü",
                    },
                    "birimAdi": "Yargıtay 3. Hukuk Dairesi",
                    "esasNoYil": 2030,
                    "esasNoSira": 42,
                    "kararNoYil": 2031,
                    "kararNoSira": 7,
                    "kararTarihi": "2031-04-05T00:00:00",
                    "title": "Arındırılmış karar başlığı",
                    "highlight": "ön ek <em>fixture</em> son ek",
                }
            ],
            "total": 21,
            "start": 0,
        },
    }


def _live_shaped_document_payload() -> dict[str, Any]:
    return {
        "metadata": {"FMTY": "SUCCESS"},
        "data": {
            "content": _SANITIZED_DOCUMENT_BASE64,
            "mimeType": "text/html",
        },
    }


def _request(**overrides: Any) -> DecisionSearchRequest:
    defaults: dict[str, Any] = {
        "query": "fixture phrase",
        "courts": (Court.YARGITAY,),
    }
    defaults.update(overrides)
    return DecisionSearchRequest(**defaults)


def _hit(*, title: str = "Generated result") -> DecisionHit:
    return DecisionHit(
        id=f"bedesten:{_DOCUMENT_ID}",
        court=Court.YARGITAY,
        chamber="H3",
        esas_no="2030/42",
        karar_no="2031/7",
        date=date(2031, 4, 5),
        title=title,
        snippet="Generated snippet",
        source_url=_DOCUMENT_SOURCE_URL,
    )


def _search_result(*, title: str = "Generated result") -> BedestenSearchResult:
    return BedestenSearchResult(
        hits=(_hit(title=title),),
        page=PageInfo(
            page=1,
            page_size=10,
            total_records=1,
            total_pages=1,
            has_more=False,
        ),
        etag='"search-v1"',
        last_modified="Thu, 01 Jan 2032 00:00:00 GMT",
    )


def _document_payload(
    *,
    markdown_text: str = "Generated document version one",
    etag: str = '"document-v1"',
) -> BedestenDocumentPayload:
    return BedestenDocumentPayload(
        data=f"<p>{markdown_text}</p>".encode(),
        mime_type="text/html",
        source_url=_DOCUMENT_SOURCE_URL,
        etag=etag,
        last_modified="Thu, 01 Jan 2032 00:00:00 GMT",
    )


def _tamper_cache_hash(
    cache: CacheStore, namespace: str, key: str, *, content_hash: str
) -> None:
    assert cache._conn is not None
    cache._conn.execute(
        "UPDATE cache_entry SET content_hash = ? WHERE namespace = ? AND key = ?",
        (content_hash, namespace, key),
    )
    cache._conn.commit()


class RecordedBedestenTransport:
    """Synthetic live-shaped Bedesten response that records exact requests."""

    def __init__(self, *, item_type: str = "YARGITAYKARARI") -> None:
        self.requests: list[httpx.Request] = []
        self._item_type = item_type

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == SEARCH_URL
        return httpx.Response(
            200,
            json=_live_shaped_search_payload(item_type=self._item_type),
            headers={"ETag": '"result-v1"'},
            request=request,
        )


class RecordedBedestenDocumentTransport:
    """Synthetic live-shaped document response that records exact requests."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._status_code = status_code
        self._payload = payload
        self._content = content
        self._headers = headers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == DOCUMENT_URL
        response_kwargs: dict[str, Any] = {
            "headers": self._headers,
            "request": request,
        }
        if self._payload is not None:
            response_kwargs["json"] = self._payload
        elif self._content is not None:
            response_kwargs["content"] = self._content
        return httpx.Response(self._status_code, **response_kwargs)


class BlockingBedestenDocumentTransport(httpx.AsyncBaseTransport):
    """Pause the single upstream document request until both callers are waiting."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == DOCUMENT_URL
        self.started.set()
        await self.release.wait()
        return httpx.Response(
            200,
            json=_live_shaped_document_payload(),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "ETag": '"document-v1"',
                "Last-Modified": "Thu, 01 Jan 2032 00:00:00 GMT",
            },
            request=request,
        )


class StubDecisionProvider:
    """In-memory provider with controllable latency and immutable responses."""

    def __init__(self) -> None:
        self.search_result = _search_result()
        self.search_error: Exception | None = None
        self.document_payloads: deque[BedestenDocumentPayload] = deque(
            (_document_payload(),)
        )
        self.search_calls: list[tuple[DecisionSearchRequest, dict[str, str]]] = []
        self.document_calls: list[tuple[str, dict[str, str]]] = []
        self.search_started = asyncio.Event()
        self.release_search = asyncio.Event()
        self.block_search = False

    async def search(
        self,
        request: DecisionSearchRequest,
        *,
        conditional_headers: dict[str, str] | None = None,
    ) -> BedestenSearchResult:
        self.search_calls.append((request, dict(conditional_headers or {})))
        self.search_started.set()
        if self.block_search:
            await self.release_search.wait()
        if self.search_error is not None:
            raise self.search_error
        return self.search_result

    async def get_document(
        self,
        document_id: str,
        *,
        conditional_headers: dict[str, str] | None = None,
    ) -> BedestenDocumentPayload:
        self.document_calls.append((document_id, dict(conditional_headers or {})))
        if not self.document_payloads:
            raise AssertionError("unexpected additional document retrieval")
        return self.document_payloads.popleft()


class FakeMonotonicClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class _RecordingDecisionLimiter:
    def __init__(self) -> None:
        self.acquire_calls = 0
        self.pause_calls: list[float | None] = []

    async def acquire(self) -> None:
        self.acquire_calls += 1

    async def pause(self, retry_after: float | None) -> None:
        self.pause_calls.append(retry_after)


@pytest.mark.parametrize(
    "query",
    (
        "E. 2020/1 K. 2021/2",
        "E. 2020/1, K. 2021/2",
    ),
)
def test_decision_request_accepts_legal_citations_with_multiple_slashes(
    query: str,
) -> None:
    assert _request(query=query).query == query


@pytest.mark.parametrize(
    "query",
    (
        "/temerrüt/",
        "temerrüt /temerrüt/ faizi",
        "/temerrüt/i",
    ),
)
def test_decision_request_rejects_slash_delimited_regex_tokens(query: str) -> None:
    with pytest.raises(
        ValueError,
        match="joker karakter, düzenli ifade, bulanık ve yakınlık söz dizimleri desteklenmez",
    ):
        _request(query=query)


def test_decision_request_accepts_inclusive_equal_date_bounds() -> None:
    day = date(2030, 1, 2)

    request = _request(date_from=day, date_to=day)

    assert (request.date_from, request.date_to) == (day, day)


@pytest.mark.asyncio
async def test_decision_search_accepts_and_caches_an_explicit_zero_result_envelope(
    tmp_path: Path,
) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "metadata": {"FMTY": "SUCCESS"},
                "data": {"emsalKararList": [], "total": 0, "start": 0},
            },
            request=request,
        )

    request = _request()
    key = canonical_parameter_hash(
        request.model_dump(mode="json"), unordered_fields=("courts",)
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as raw:
        provider = BedestenDecisionProvider(
            SafeHttpClient(raw, {"bedesten.adalet.gov.tr"})
        )
        with CacheStore(tmp_path / "decisions.sqlite") as cache:
            result = await DecisionService(provider, cache).search(request)
            cached = cache.get(_SEARCH_NAMESPACE, key)

    assert result["ok"] is True
    assert result["hits"] == []
    assert result["page"] == {
        "page": 1,
        "page_size": 10,
        "total_records": 0,
        "total_pages": 0,
        "has_more": False,
    }
    assert cached is not None
    assert json.loads(cached.content) == {
        "hits": [],
        "page": {
            "page": 1,
            "page_size": 10,
            "total_records": 0,
            "total_pages": 0,
            "has_more": False,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        {
            "metadata": {"FMTY": "SUCCESS"},
            "data": {"total": 0, "start": 0},
        },
    ),
)
async def test_decision_search_rejects_malformed_success_envelopes_without_caching(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    request = _request()
    key = canonical_parameter_hash(
        request.model_dump(mode="json"), unordered_fields=("courts",)
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as raw:
        provider = BedestenDecisionProvider(
            SafeHttpClient(raw, {"bedesten.adalet.gov.tr"})
        )
        with CacheStore(tmp_path / "decisions.sqlite") as cache:
            result = await DecisionService(provider, cache).search(request)
            assert cache.get(_SEARCH_NAMESPACE, key) is None

    assert result["ok"] is False
    assert result["error"]["code"] == "upstream_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        {
            "metadata": {"FMTY": "FAILURE", "FMTE": "upstream secret detail"},
            "data": {"emsalKararList": [], "total": 0, "start": 0},
        },
        {
            "data": {"emsalKararList": [], "total": 0, "start": 0},
        },
    ),
)
async def test_bedesten_search_requires_success_metadata(
    payload: dict[str, Any],
) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as raw:
        provider = BedestenDecisionProvider(
            SafeHttpClient(raw, {"bedesten.adalet.gov.tr"})
        )
        with pytest.raises(ProviderProtocolError) as error:
            await provider.search(_request())

    assert "upstream secret detail" not in str(error.value)


@pytest.mark.asyncio
async def test_bedesten_search_returns_not_modified_with_response_headers() -> None:
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            304,
            headers={
                "ETag": '"search-v2"',
                "Last-Modified": "Fri, 02 Jan 2032 00:00:00 GMT",
            },
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as raw:
        provider = BedestenDecisionProvider(
            SafeHttpClient(raw, {"bedesten.adalet.gov.tr"})
        )
        result = await provider.search(
            _request(),
            conditional_headers={
                "If-None-Match": '"search-v1"',
                "If-Modified-Since": "Thu, 01 Jan 2032 00:00:00 GMT",
            },
        )

    assert len(requests) == 1
    assert requests[0].headers["if-none-match"] == '"search-v1"'
    assert result == BedestenSearchResult(
        hits=(),
        page=PageInfo(
            page=1,
            page_size=10,
            total_records=0,
            total_pages=0,
            has_more=False,
        ),
        etag='"search-v2"',
        last_modified="Fri, 02 Jan 2032 00:00:00 GMT",
        not_modified=True,
    )


@pytest.mark.asyncio
async def test_bedesten_search_pauses_injected_limiter_on_429() -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "12"},
            request=request,
        )

    limiter = _RecordingDecisionLimiter()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as raw:
        provider = BedestenDecisionProvider(
            SafeHttpClient(raw, {"bedesten.adalet.gov.tr"}),
            limiter=limiter,
        )
        with pytest.raises(UpstreamRateLimited) as error:
            await provider.search(_request())

    assert limiter.acquire_calls == 1
    assert limiter.pause_calls == [12.0]
    assert error.value.status_code == 429
    assert error.value.retry_after == 12.0


@pytest.mark.asyncio
async def test_bedesten_search_translates_complete_request_and_normalizes_hit() -> None:
    transport = RecordedBedestenTransport()
    request = _request(
        courts=(Court.YARGITAY, Court.DANISTAY),
        chamber="H3",
        date_from=date(2030, 1, 2),
        date_to=date(2031, 3, 4),
        esas_year=2030,
        esas_sequence=42,
        karar_year=2031,
        karar_sequence=7,
        sort="tarih_eskiden_yeniye",
        page=2,
        page_size=10,
        include_snippet=True,
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as raw:
        http = SafeHttpClient(raw, {"bedesten.adalet.gov.tr"})
        provider = BedestenDecisionProvider(http)
        result = await provider.search(request)

    assert len(transport.requests) == 1
    assert json.loads(transport.requests[0].content) == {
        "applicationName": "UyapMevzuat",
        "paging": True,
        "data": {
            "pageSize": 10,
            "pageNumber": 2,
            "itemTypeList": ["YARGITAYKARARI", "DANISTAYKARAR"],
            "sortFields": ["KARAR_TARIHI"],
            "sortDirection": "ASC",
            "phrase": "fixture phrase",
            "birimAdi": "3. Hukuk Dairesi",
            "kararTarihiStart": "2030-01-02T00:00:00.000Z",
            "kararTarihiEnd": "2031-03-04T23:59:59.000Z",
            "esasNoYil": "2030",
            "esasNoSira": "42",
            "kararNoYil": "2031",
            "kararNoSira": "7",
        },
    }
    assert transport.requests[0].headers["origin"] == "https://mevzuat.adalet.gov.tr"
    assert result.page.model_dump() == {
        "page": 2,
        "page_size": 10,
        "total_records": 21,
        "total_pages": 3,
        "has_more": True,
    }
    assert result.hits[0].model_dump(mode="json") == {
        "id": f"bedesten:{_DOCUMENT_ID}",
        "source_url": _DOCUMENT_SOURCE_URL,
        "title": "Arındırılmış karar başlığı",
        "snippet": "ön ek fixture son ek",
        "source": "bedesten",
        "court": "yargitay",
        "chamber": "H3",
        "esas_no": "2030/42",
        "karar_no": "2031/7",
        "date": "2031-04-05",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("court", "item_type"),
    (
        (Court.YARGITAY, "YARGITAYKARARI"),
        (Court.DANISTAY, "DANISTAYKARAR"),
        (Court.ISTINAF, "ISTINAFHUKUK"),
        (Court.YEREL, "YERELHUKUK"),
        (Court.KYB, "KYB"),
    ),
)
async def test_bedesten_search_emits_each_canonical_court_mapping(
    court: Court, item_type: str
) -> None:
    transport = RecordedBedestenTransport(item_type=item_type)
    request = _request(courts=(court,))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as raw:
        provider = BedestenDecisionProvider(
            SafeHttpClient(raw, {"bedesten.adalet.gov.tr"})
        )
        result = await provider.search(request)

    assert len(transport.requests) == 1
    assert json.loads(transport.requests[0].content) == {
        "applicationName": "UyapMevzuat",
        "paging": True,
        "data": {
            "pageSize": 10,
            "pageNumber": 1,
            "itemTypeList": [item_type],
            "sortFields": ["KARAR_TARIHI"],
            "sortDirection": "desc",
            "phrase": "fixture phrase",
        },
    }
    assert result.hits[0].court is court


@pytest.mark.asyncio
async def test_bedesten_document_decodes_live_shaped_json_and_headers() -> None:
    transport = RecordedBedestenDocumentTransport(
        payload=_live_shaped_document_payload(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "ETag": '"document-v1"',
            "Last-Modified": "Thu, 01 Jan 2032 00:00:00 GMT",
        },
    )
    conditional_headers = {
        "If-None-Match": '"document-v0"',
        "If-Modified-Since": "Wed, 31 Dec 2031 00:00:00 GMT",
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as raw:
        provider = BedestenDecisionProvider(
            SafeHttpClient(raw, {"bedesten.adalet.gov.tr"})
        )
        payload = await provider.get_document(
            _DOCUMENT_ID, conditional_headers=conditional_headers
        )

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert json.loads(request.content) == {
        "applicationName": "UyapMevzuat",
        "data": {"documentId": _DOCUMENT_ID},
    }
    assert {
        name: request.headers[name]
        for name in (
            "accept",
            "origin",
            "referer",
            "if-none-match",
            "if-modified-since",
        )
    } == {
        "accept": "application/json, text/plain, */*",
        "origin": "https://mevzuat.adalet.gov.tr",
        "referer": "https://mevzuat.adalet.gov.tr/",
        "if-none-match": '"document-v0"',
        "if-modified-since": "Wed, 31 Dec 2031 00:00:00 GMT",
    }
    assert payload == BedestenDocumentPayload(
        data=_SANITIZED_DOCUMENT_HTML.encode("utf-8"),
        mime_type="text/html",
        source_url=_DOCUMENT_SOURCE_URL,
        etag='"document-v1"',
        last_modified="Thu, 01 Jan 2032 00:00:00 GMT",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        {
            "metadata": {"FMTY": "FAILURE", "FMTE": "upstream secret detail"},
            "data": {
                "content": _SANITIZED_DOCUMENT_BASE64,
                "mimeType": "text/html",
            },
        },
        {
            "data": {
                "content": _SANITIZED_DOCUMENT_BASE64,
                "mimeType": "text/html",
            },
        },
    ),
)
async def test_bedesten_document_requires_success_metadata(
    payload: dict[str, Any],
) -> None:
    transport = RecordedBedestenDocumentTransport(
        payload=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as raw:
        provider = BedestenDecisionProvider(
            SafeHttpClient(raw, {"bedesten.adalet.gov.tr"})
        )
        with pytest.raises(ProviderProtocolError) as error:
            await provider.get_document(_DOCUMENT_ID)

    assert "upstream secret detail" not in str(error.value)


@pytest.mark.asyncio
async def test_bedesten_document_rejects_malformed_base64_content() -> None:
    malformed_content = "not-base64-fixture"
    transport = RecordedBedestenDocumentTransport(
        payload={
            "metadata": {"FMTY": "SUCCESS"},
            "data": {"content": malformed_content, "mimeType": "text/html"},
        },
        headers={"Content-Type": "application/json; charset=utf-8"},
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as raw:
        provider = BedestenDecisionProvider(
            SafeHttpClient(raw, {"bedesten.adalet.gov.tr"})
        )
        with pytest.raises(ProviderProtocolError) as error:
            await provider.get_document(_DOCUMENT_ID)

    assert malformed_content not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direct_content", "content_type", "expected_mime"),
    (
        (b"<p>direct fixture</p>", "text/html; charset=utf-8", "text/html"),
        (b"plain fixture \x00\xff", "text/plain; charset=utf-8", "text/plain"),
    ),
)
async def test_bedesten_document_preserves_non_json_direct_response(
    direct_content: bytes, content_type: str, expected_mime: str
) -> None:
    transport = RecordedBedestenDocumentTransport(
        content=direct_content,
        headers={"Content-Type": content_type},
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as raw:
        provider = BedestenDecisionProvider(
            SafeHttpClient(raw, {"bedesten.adalet.gov.tr"})
        )
        payload = await provider.get_document(_DOCUMENT_ID)

    assert payload == BedestenDocumentPayload(
        data=direct_content,
        mime_type=expected_mime,
        source_url=_DOCUMENT_SOURCE_URL,
        etag=None,
        last_modified=None,
    )


@pytest.mark.asyncio
async def test_bedesten_document_returns_not_modified_for_304() -> None:
    transport = RecordedBedestenDocumentTransport(
        status_code=304,
        headers={
            "ETag": '"document-v2"',
            "Last-Modified": "Fri, 02 Jan 2032 00:00:00 GMT",
        },
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as raw:
        provider = BedestenDecisionProvider(
            SafeHttpClient(raw, {"bedesten.adalet.gov.tr"})
        )
        payload = await provider.get_document(
            _DOCUMENT_ID, conditional_headers={"If-None-Match": '"document-v1"'}
        )

    assert json.loads(transport.requests[0].content) == {
        "applicationName": "UyapMevzuat",
        "data": {"documentId": _DOCUMENT_ID},
    }
    assert transport.requests[0].headers["if-none-match"] == '"document-v1"'
    assert payload == BedestenDocumentPayload(
        data=b"",
        mime_type="",
        source_url=_DOCUMENT_SOURCE_URL,
        etag='"document-v2"',
        last_modified="Fri, 02 Jan 2032 00:00:00 GMT",
        not_modified=True,
    )


@pytest.mark.asyncio
async def test_bedesten_document_surfaces_429_retry_after() -> None:
    transport = RecordedBedestenDocumentTransport(
        status_code=429, headers={"Retry-After": "12"}
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as raw:
        provider = BedestenDecisionProvider(
            SafeHttpClient(raw, {"bedesten.adalet.gov.tr"})
        )
        with pytest.raises(UpstreamRateLimited) as error:
            await provider.get_document(_DOCUMENT_ID)

    assert json.loads(transport.requests[0].content) == {
        "applicationName": "UyapMevzuat",
        "data": {"documentId": _DOCUMENT_ID},
    }
    assert error.value.status_code == 429
    assert error.value.retry_after == 12.0


@pytest.mark.asyncio
async def test_rolling_window_rate_limiter_waits_for_capacity_and_retry_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeMonotonicClock()
    monkeypatch.setattr(bedesten_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(bedesten_module.asyncio, "sleep", clock.sleep)
    limiter = RollingWindowRateLimiter(capacity=1, window_seconds=3.0)

    await limiter.acquire()
    await limiter.acquire()
    await limiter.pause(2.0)
    await limiter.acquire()

    assert clock.sleeps == [3.0, 2.5]


@pytest.mark.asyncio
async def test_decision_service_uses_fresh_search_cache(tmp_path: Path) -> None:
    provider = StubDecisionProvider()
    request = _request()

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        service = DecisionService(provider, cache)
        first = await service.search(request)
        second = await service.search(request)

    assert first == second
    assert first["ok"] is True
    assert "expires_at" not in first
    assert len(provider.search_calls) == 1
    assert provider.search_calls[0][1] == {}


@pytest.mark.asyncio
async def test_decision_service_document_cache_never_expires(tmp_path: Path) -> None:
    provider = StubDecisionProvider()

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        service = DecisionService(provider, cache)
        first = await service.get_document(f"bedesten:{_DOCUMENT_ID}")
        second = await service.get_document(f"bedesten:{_DOCUMENT_ID}")
        cached = cache.get(
            _DOCUMENT_NAMESPACE,
            f"bedesten:{_DOCUMENT_ID}",
            now=datetime(2099, 1, 1, tzinfo=UTC),
        )

    assert first == second
    assert first["ok"] is True
    assert first["expires_at"] is None
    assert len(provider.document_calls) == 1
    assert cached is not None
    assert cached.freshness is Freshness.FRESH
    assert cached.expires_at is None


@pytest.mark.asyncio
async def test_decision_service_converts_plain_text_document(tmp_path: Path) -> None:
    provider = StubDecisionProvider()
    provider.document_payloads = deque(
        (
            BedestenDocumentPayload(
                data="Danıştay karar metni\r\n\r\nİkinci satır".encode(),
                mime_type="text/plain; charset=utf-8",
                source_url=_DOCUMENT_SOURCE_URL,
                etag=None,
                last_modified=None,
            ),
        )
    )

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        document = await DecisionService(provider, cache).get_document(
            f"bedesten:{_DOCUMENT_ID}"
        )

    assert document["ok"] is True
    assert document["markdown"] == "Danıştay karar metni\n\nİkinci satır\n"
    assert document["mime_type"] == "text/plain"
    assert document["conversion"] == "text_markdown"
    Draft202012Validator(standalone_tool_schema("belge_getir", "output")).validate(
        document
    )


@pytest.mark.asyncio
async def test_decision_service_fresh_outputs_match_mcp_schemas(
    tmp_path: Path,
) -> None:
    provider = StubDecisionProvider()

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        service = DecisionService(provider, cache)
        search = await service.search(_request())
        document = await service.get_document(f"bedesten:{_DOCUMENT_ID}")

    assert "revalidation_error" not in search
    assert "expires_at" not in search
    assert "revalidation_error" not in document
    assert document["expires_at"] is None
    assert document["mime_type"] == "text/html"
    Draft202012Validator(standalone_tool_schema("karar_ara", "output")).validate(search)
    Draft202012Validator(standalone_tool_schema("belge_getir", "output")).validate(
        document
    )


@pytest.mark.asyncio
async def test_decision_service_preserves_nullable_hit_fields_and_matches_schema(
    tmp_path: Path,
) -> None:
    provider = StubDecisionProvider()
    provider.search_result = BedestenSearchResult(
        hits=(
            DecisionHit(
                id=f"bedesten:{_DOCUMENT_ID}",
                court=Court.YARGITAY,
                source_url=_DOCUMENT_SOURCE_URL,
            ),
        ),
        page=PageInfo(
            page=1,
            page_size=10,
            total_records=1,
            total_pages=1,
            has_more=False,
        ),
        etag='"search-v1"',
        last_modified="Thu, 01 Jan 2032 00:00:00 GMT",
    )

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        response = await DecisionService(provider, cache).search(_request())

    assert response["hits"] == [
        {
            "id": f"bedesten:{_DOCUMENT_ID}",
            "source_url": _DOCUMENT_SOURCE_URL,
            "title": None,
            "snippet": None,
            "source": "bedesten",
            "court": "yargitay",
            "chamber": None,
            "esas_no": None,
            "karar_no": None,
            "date": None,
        }
    ]
    Draft202012Validator(standalone_tool_schema("karar_ara", "output")).validate(
        response
    )


def test_decision_pages_split_long_articles_at_paragraph_boundaries() -> None:
    target_chars = 120
    first_page = "MADDE 1 - Uygulama\n" + "Birinci paragrafın metni. " * 3
    second_page = "İkinci paragrafın metni. " * 3
    first_article = f"{first_page}\n\n{second_page}"
    second_article = "MADDE 2 - Sonuç\nKısa ikinci madde."
    markdown = f"{first_article}\n\n{second_article}"

    pages = _markdown_pages(markdown, target_chars=target_chars)

    assert pages == (first_page, second_page, second_article)
    assert pages == chunk_markdown(markdown, max_chars=target_chars)
    assert "\n\n".join(pages) == markdown
    assert all(len(page) <= target_chars for page in pages)


@pytest.mark.asyncio
async def test_decision_service_singleflight_caches_once_and_paginates_per_caller(
    tmp_path: Path,
) -> None:
    transport = BlockingBedestenDocumentTransport()

    async with httpx.AsyncClient(transport=transport, timeout=1.0) as raw:
        provider = BedestenDecisionProvider(
            SafeHttpClient(raw, {"bedesten.adalet.gov.tr"})
        )
        with CacheStore(tmp_path / "decisions.sqlite") as cache:
            service = DecisionService(provider, cache, document_page_target_chars=1)
            first_task = asyncio.create_task(
                service.get_document(f"bedesten:{_DOCUMENT_ID}", page=1)
            )
            await transport.started.wait()
            second_task = asyncio.create_task(
                service.get_document(f"bedesten:{_DOCUMENT_ID}", page=2)
            )
            await asyncio.sleep(0)
            assert len(transport.requests) == 1
            transport.release.set()
            first, second = await asyncio.gather(first_task, second_task)

    assert json.loads(transport.requests[0].content) == {
        "applicationName": "UyapMevzuat",
        "data": {"documentId": _DOCUMENT_ID},
    }
    assert first["ok"] is True
    assert second["ok"] is True
    assert first["page"]["page"] == 1
    assert first["page"]["total_pages"] == 2
    assert first["page"]["has_more"] is True
    assert second["page"]["page"] == 2
    assert second["page"]["total_pages"] == 2
    assert second["page"]["has_more"] is False
    assert first["markdown"] != second["markdown"]
    assert first["content_hash"] == second["content_hash"]


@pytest.mark.asyncio
async def test_decision_service_singleflight_coalesces_matching_searches(
    tmp_path: Path,
) -> None:
    provider = StubDecisionProvider()
    provider.block_search = True
    request = _request()

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        service = DecisionService(provider, cache)
        first_task = asyncio.create_task(service.search(request))
        await provider.search_started.wait()
        second_task = asyncio.create_task(service.search(request))
        await asyncio.sleep(0)
        provider.release_search.set()
        first, second = await asyncio.gather(first_task, second_task)

    assert first == second
    assert first["ok"] is True
    assert len(provider.search_calls) == 1


@pytest.mark.asyncio
async def test_decision_service_refreshes_document_and_revalidates_with_etag(
    tmp_path: Path,
) -> None:
    provider = StubDecisionProvider()
    provider.document_payloads = deque(
        (
            _document_payload(markdown_text="Generated document version one"),
            _document_payload(
                markdown_text="Generated document version two", etag='"document-v2"'
            ),
        )
    )

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        service = DecisionService(provider, cache)
        initial = await service.get_document(f"bedesten:{_DOCUMENT_ID}")
        refreshed = await service.get_document(f"bedesten:{_DOCUMENT_ID}", refresh=True)

    assert initial["markdown"] == "Generated document version one\n"
    assert refreshed["markdown"] == "Generated document version two\n"
    assert len(provider.document_calls) == 2
    assert provider.document_calls[1] == (
        _DOCUMENT_ID,
        {
            "If-None-Match": '"document-v1"',
            "If-Modified-Since": "Thu, 01 Jan 2032 00:00:00 GMT",
        },
    )


@pytest.mark.asyncio
async def test_decision_service_revalidates_cached_document_on_304(
    tmp_path: Path,
) -> None:
    provider = StubDecisionProvider()
    provider.document_payloads = deque(
        (
            _document_payload(markdown_text="Generated document version one"),
            BedestenDocumentPayload(
                data=b"",
                mime_type="",
                source_url=_DOCUMENT_SOURCE_URL,
                etag='"document-v2"',
                last_modified="Fri, 02 Jan 2032 00:00:00 GMT",
                not_modified=True,
            ),
        )
    )

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        service = DecisionService(provider, cache)
        initial = await service.get_document(f"bedesten:{_DOCUMENT_ID}")
        cache.mark_validated(
            _DOCUMENT_NAMESPACE,
            f"bedesten:{_DOCUMENT_ID}",
            validated_at=datetime(2000, 1, 1, tzinfo=UTC),
            expires_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        revalidated = await service.get_document(
            f"bedesten:{_DOCUMENT_ID}", refresh=True
        )
        cached = cache.get(_DOCUMENT_NAMESPACE, f"bedesten:{_DOCUMENT_ID}")

    assert revalidated["markdown"] == initial["markdown"]
    assert len(provider.document_calls) == 2
    assert provider.document_calls[1] == (
        _DOCUMENT_ID,
        {
            "If-None-Match": '"document-v1"',
            "If-Modified-Since": "Thu, 01 Jan 2032 00:00:00 GMT",
        },
    )
    assert cached is not None
    assert cached.freshness is Freshness.FRESH
    assert cached.content == initial["markdown"]
    assert cached.etag == '"document-v2"'
    assert cached.last_modified == "Fri, 02 Jan 2032 00:00:00 GMT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_code",
    (
        pytest.param(None, id="network"),
        pytest.param(503, id="server-503"),
    ),
)
async def test_decision_service_serves_eligible_stale_search_for_network_or_5xx(
    tmp_path: Path, status_code: int | None
) -> None:
    provider = StubDecisionProvider()
    request = _request()

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        service = DecisionService(provider, cache)
        seeded = await service.search(request)
        key = canonical_parameter_hash(
            request.model_dump(mode="json"), unordered_fields=("courts",)
        )
        cache.mark_validated(
            _SEARCH_NAMESPACE,
            key,
            validated_at=datetime(2000, 1, 1, tzinfo=UTC),
            expires_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        provider.search_error = UpstreamUnavailable(
            "synthetic outage", status_code=status_code
        )
        stale = await service.search(request)

    assert seeded["ok"] is True
    assert set(stale) == {
        "ok",
        "hits",
        "page",
        "expires_at",
        "warnings",
        "fetched_at",
        "validated_at",
        "revalidation_error",
    }
    assert stale["ok"] is True
    assert stale["hits"] == seeded["hits"]
    assert stale["page"] == seeded["page"]
    assert stale["warnings"] == [
        {
            "code": "stale_content",
            "message": (
                "Önbelleğe alınan içerik, üst kaynak yeniden doğrulaması "
                "kullanılamadığı için güncel değildir."
            ),
        }
    ]
    assert stale["revalidation_error"] == {
        "code": "upstream_unavailable",
        "message": "Üst kaynak şu anda kullanılamıyor. Ayrıntı: synthetic outage",
    }
    assert len(provider.search_calls) == 2
    assert provider.search_calls[1][1] == {
        "If-None-Match": '"search-v1"',
        "If-Modified-Since": "Thu, 01 Jan 2032 00:00:00 GMT",
    }


@pytest.mark.asyncio
async def test_decision_service_revalidates_cached_search_on_304(
    tmp_path: Path,
) -> None:
    provider = StubDecisionProvider()
    request = _request()

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        service = DecisionService(provider, cache)
        initial = await service.search(request)
        key = canonical_parameter_hash(
            request.model_dump(mode="json"), unordered_fields=("courts",)
        )
        cache.mark_validated(
            _SEARCH_NAMESPACE,
            key,
            validated_at=datetime(2000, 1, 1, tzinfo=UTC),
            expires_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        provider.search_result = replace(
            provider.search_result,
            hits=(),
            etag='"search-v2"',
            last_modified="Fri, 02 Jan 2032 00:00:00 GMT",
            not_modified=True,
        )
        revalidated = await service.search(request)
        cached = cache.get(_SEARCH_NAMESPACE, key)

    assert revalidated == initial
    assert revalidated["hits"] == initial["hits"]
    assert len(provider.search_calls) == 2
    assert provider.search_calls[1][1] == {
        "If-None-Match": '"search-v1"',
        "If-Modified-Since": "Thu, 01 Jan 2032 00:00:00 GMT",
    }
    assert cached is not None
    assert cached.freshness is Freshness.FRESH
    assert cached.content == json.dumps(
        {"hits": initial["hits"], "page": initial["page"]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert cached.etag == '"search-v2"'
    assert cached.last_modified == "Fri, 02 Jan 2032 00:00:00 GMT"


@pytest.mark.asyncio
async def test_decision_service_does_not_validate_corrupt_stale_search_on_304(
    tmp_path: Path,
) -> None:
    provider = StubDecisionProvider()
    request = _request()
    stale_at = datetime(2000, 1, 1, tzinfo=UTC)
    key = canonical_parameter_hash(
        request.model_dump(mode="json"), unordered_fields=("courts",)
    )
    provider.search_result = replace(
        provider.search_result,
        hits=(),
        etag='"search-v2"',
        last_modified="Fri, 02 Jan 2032 00:00:00 GMT",
        not_modified=True,
    )

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        cache.put(
            namespace=_SEARCH_NAMESPACE,
            key=key,
            source_url=SEARCH_URL,
            content="{",
            fetched_at=stale_at,
            validated_at=stale_at,
            expires_at=stale_at,
            etag='"search-v1"',
            last_modified="Thu, 01 Jan 2032 00:00:00 GMT",
            mime_type="application/json",
        )
        service = DecisionService(provider, cache)
        first = await service.search(request)
        after_failed_revalidation = cache.get(_SEARCH_NAMESPACE, key)
        provider.search_result = _search_result(title="Recovered result")
        second = await service.search(request)
        after_revalidation = cache.get(_SEARCH_NAMESPACE, key)

    assert first["ok"] is False
    assert first["error"]["code"] == "upstream_unavailable"
    assert after_failed_revalidation is not None
    assert after_failed_revalidation.freshness is Freshness.EXPIRED
    assert after_failed_revalidation.validated_at == stale_at
    assert after_failed_revalidation.etag == '"search-v1"'
    assert second["ok"] is True
    assert second["hits"][0]["title"] == "Recovered result"
    assert len(provider.search_calls) == 2
    assert provider.search_calls[1][1] == {
        "If-None-Match": '"search-v1"',
        "If-Modified-Since": "Thu, 01 Jan 2032 00:00:00 GMT",
    }
    assert after_revalidation is not None
    assert after_revalidation.freshness is Freshness.FRESH


@pytest.mark.asyncio
async def test_decision_service_does_not_validate_corrupt_stale_document_on_304(
    tmp_path: Path,
) -> None:
    provider = StubDecisionProvider()
    provider.document_payloads = deque(
        (
            BedestenDocumentPayload(
                data=b"",
                mime_type="",
                source_url=_DOCUMENT_SOURCE_URL,
                etag='"document-v2"',
                last_modified="Fri, 02 Jan 2032 00:00:00 GMT",
                not_modified=True,
            ),
            _document_payload(markdown_text="Recovered document", etag='"document-v3"'),
        )
    )
    stale_at = datetime(2000, 1, 1, tzinfo=UTC)
    key = f"bedesten:{_DOCUMENT_ID}"

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        cache.put(
            namespace=_DOCUMENT_NAMESPACE,
            key=key,
            source_url=_DOCUMENT_SOURCE_URL,
            content="Corrupt cached document",
            fetched_at=stale_at,
            validated_at=stale_at,
            expires_at=stale_at,
            etag='"document-v1"',
            last_modified="Thu, 01 Jan 2032 00:00:00 GMT",
            mime_type="text/markdown",
        )
        service = DecisionService(provider, cache)
        first = await service.get_document(key, refresh=True)
        after_failed_revalidation = cache.get(_DOCUMENT_NAMESPACE, key)
        second = await service.get_document(key, refresh=True)
        after_revalidation = cache.get(_DOCUMENT_NAMESPACE, key)

    assert first["ok"] is False
    assert first["error"]["code"] == "upstream_unavailable"
    assert after_failed_revalidation is not None
    assert after_failed_revalidation.freshness is Freshness.EXPIRED
    assert after_failed_revalidation.validated_at == stale_at
    assert after_failed_revalidation.etag == '"document-v1"'
    assert second["ok"] is True
    assert second["markdown"] == "Recovered document\n"
    assert len(provider.document_calls) == 2
    assert provider.document_calls[1][1] == {
        "If-None-Match": '"document-v1"',
        "If-Modified-Since": "Thu, 01 Jan 2032 00:00:00 GMT",
    }
    assert after_revalidation is not None
    assert after_revalidation.freshness is Freshness.FRESH
    assert after_revalidation.etag == '"document-v3"'


@pytest.mark.asyncio
async def test_decision_service_does_not_validate_hash_mismatched_stale_search_on_304(
    tmp_path: Path,
) -> None:
    provider = StubDecisionProvider()
    request = _request()
    stale_at = datetime(2000, 1, 1, tzinfo=UTC)
    key = canonical_parameter_hash(
        request.model_dump(mode="json"), unordered_fields=("courts",)
    )

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        service = DecisionService(provider, cache)
        initial = await service.search(request)
        cache.mark_validated(
            _SEARCH_NAMESPACE,
            key,
            validated_at=stale_at,
            expires_at=stale_at,
        )
        _tamper_cache_hash(
            cache,
            _SEARCH_NAMESPACE,
            key,
            content_hash="sha256:" + "0" * 64,
        )
        provider.search_result = replace(
            provider.search_result,
            hits=(),
            etag='"search-v2"',
            last_modified="Fri, 02 Jan 2032 00:00:00 GMT",
            not_modified=True,
        )
        first = await service.search(request)
        after_failed_revalidation = cache.get(_SEARCH_NAMESPACE, key)
        provider.search_result = _search_result(title="Recovered result")
        second = await service.search(request)
        after_revalidation = cache.get(_SEARCH_NAMESPACE, key)

    assert initial["ok"] is True
    assert first["ok"] is False
    assert first["error"]["code"] == "upstream_unavailable"
    assert after_failed_revalidation is not None
    assert after_failed_revalidation.freshness is Freshness.EXPIRED
    assert after_failed_revalidation.validated_at == stale_at
    assert after_failed_revalidation.content_hash == "sha256:" + "0" * 64
    assert second["ok"] is True
    assert second["hits"][0]["title"] == "Recovered result"
    assert len(provider.search_calls) == 3
    assert provider.search_calls[1][1] == {
        "If-None-Match": '"search-v1"',
        "If-Modified-Since": "Thu, 01 Jan 2032 00:00:00 GMT",
    }
    assert provider.search_calls[2][1] == {
        "If-None-Match": '"search-v1"',
        "If-Modified-Since": "Thu, 01 Jan 2032 00:00:00 GMT",
    }
    assert after_revalidation is not None
    assert after_revalidation.freshness is Freshness.FRESH


@pytest.mark.asyncio
async def test_decision_service_does_not_validate_hash_mismatched_stale_document_on_304(
    tmp_path: Path,
) -> None:
    provider = StubDecisionProvider()
    stale_at = datetime(2000, 1, 1, tzinfo=UTC)
    key = f"bedesten:{_DOCUMENT_ID}"
    provider.document_payloads = deque(
        (
            _document_payload(),
            BedestenDocumentPayload(
                data=b"",
                mime_type="",
                source_url=_DOCUMENT_SOURCE_URL,
                etag='"document-v2"',
                last_modified="Fri, 02 Jan 2032 00:00:00 GMT",
                not_modified=True,
            ),
            _document_payload(markdown_text="Recovered document", etag='"document-v3"'),
        )
    )

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        service = DecisionService(provider, cache)
        initial = await service.get_document(key)
        cache.mark_validated(
            _DOCUMENT_NAMESPACE,
            key,
            validated_at=stale_at,
            expires_at=stale_at,
        )
        _tamper_cache_hash(
            cache,
            _DOCUMENT_NAMESPACE,
            key,
            content_hash="sha256:" + "0" * 64,
        )
        first = await service.get_document(key, refresh=True)
        after_failed_revalidation = cache.get(_DOCUMENT_NAMESPACE, key)
        second = await service.get_document(key, refresh=True)
        after_revalidation = cache.get(_DOCUMENT_NAMESPACE, key)

    assert initial["ok"] is True
    assert first["ok"] is False
    assert first["error"]["code"] == "upstream_unavailable"
    assert after_failed_revalidation is not None
    assert after_failed_revalidation.freshness is Freshness.EXPIRED
    assert after_failed_revalidation.validated_at == stale_at
    assert after_failed_revalidation.content_hash == "sha256:" + "0" * 64
    assert second["ok"] is True
    assert second["markdown"] == "Recovered document\n"
    assert len(provider.document_calls) == 3
    assert provider.document_calls[1][1] == {
        "If-None-Match": '"document-v1"',
        "If-Modified-Since": "Thu, 01 Jan 2032 00:00:00 GMT",
    }
    assert provider.document_calls[2][1] == {
        "If-None-Match": '"document-v1"',
        "If-Modified-Since": "Thu, 01 Jan 2032 00:00:00 GMT",
    }
    assert after_revalidation is not None
    assert after_revalidation.freshness is Freshness.FRESH


@pytest.mark.asyncio
async def test_decision_service_never_serves_stale_search_for_upstream_404(
    tmp_path: Path,
) -> None:
    provider = StubDecisionProvider()
    request = _request()

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        service = DecisionService(provider, cache)
        seeded = await service.search(request)
        key = canonical_parameter_hash(
            request.model_dump(mode="json"), unordered_fields=("courts",)
        )
        cache.mark_validated(
            _SEARCH_NAMESPACE,
            key,
            validated_at=datetime(2000, 1, 1, tzinfo=UTC),
            expires_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        provider.search_error = UpstreamNotFound("synthetic record is absent", 404)
        response = await service.search(request)

    assert seeded["ok"] is True
    assert response == {
        "ok": False,
        "error": {
            "code": "upstream_unavailable",
            "message": (
                "Üst kaynak istenen belgeyi bulamadı. "
                "Ayrıntı: synthetic record is absent"
            ),
            "retryable": True,
            "details": {"upstream": "bedesten"},
        },
    }
    assert len(provider.search_calls) == 2
    assert provider.search_calls[1][1] == {
        "If-None-Match": '"search-v1"',
        "If-Modified-Since": "Thu, 01 Jan 2032 00:00:00 GMT",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "field_errors"),
    (
        ("page", {"page": ["1 veya daha büyük bir tam sayı olmalıdır."]}),
        ("refresh", {"refresh": ["Geçerli bir mantıksal değer olmalıdır."]}),
        (
            "request_and_keywords",
            {
                "request": [
                    "request, anahtar sözcük parametreleriyle birlikte kullanılamaz."
                ]
            },
        ),
        ("request_type", {"request": ["bir nesne olmalıdır."]}),
        ("document_id_type", {"id": ["bir Bedesten belge kimliği olmalıdır."]}),
        (
            "document_id_provider",
            {"id": ["bir Bedesten belge kimliği olmalıdır."]},
        ),
    ),
)
async def test_decision_service_local_validation_messages_are_turkish(
    tmp_path: Path, case: str, field_errors: dict[str, list[str]]
) -> None:
    provider = StubDecisionProvider()

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        service = DecisionService(provider, cache)
        if case == "page":
            result = await service.get_document(f"bedesten:{_DOCUMENT_ID}", page=0)
        elif case == "refresh":
            result = await service.get_document(
                f"bedesten:{_DOCUMENT_ID}",
                refresh=1,  # type: ignore[arg-type]
            )
        elif case == "request_and_keywords":
            result = await service.search(_request(), query="başka sorgu")
        elif case == "request_type":
            result = await service.search(1)  # type: ignore[arg-type]
        elif case == "document_id_type":
            result = await service.get_document(1)  # type: ignore[arg-type]
        else:
            result = await service.get_document("anayasa:nd:2042:17")

    assert result == {
        "ok": False,
        "error": {
            "code": "invalid_params",
            "message": "İstek parametreleri geçersiz.",
            "retryable": False,
            "details": {"field_errors": field_errors},
        },
    }
    assert provider.search_calls == []
    assert provider.document_calls == []


@pytest.mark.asyncio
async def test_decision_service_returns_turkish_reversed_date_validation_error(
    tmp_path: Path,
) -> None:
    provider = StubDecisionProvider()

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        result = await DecisionService(provider, cache).search(
            {
                "query": "fixture phrase",
                "courts": ["yargitay"],
                "date_from": "2031-01-02",
                "date_to": "2030-01-02",
            }
        )

    assert result == {
        "ok": False,
        "error": {
            "code": "invalid_params",
            "message": "İstek parametreleri geçersiz.",
            "retryable": False,
            "details": {
                "field_errors": {
                    "request": ["date_from, date_to değerinden sonra olamaz"]
                }
            },
        },
    }
    assert provider.search_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "detail"),
    (
        (
            "search",
            "Bedesten, önbelleğe alınmış karar araması olmadan 304 yanıtı döndürdü.",
        ),
        (
            "document",
            "Bedesten, önbelleğe alınmış belge olmadan 304 yanıtı döndürdü.",
        ),
    ),
)
async def test_decision_service_translates_uncached_304_protocol_errors(
    tmp_path: Path, operation: str, detail: str
) -> None:
    provider = StubDecisionProvider()
    if operation == "search":
        provider.search_result = BedestenSearchResult(
            hits=(),
            page=PageInfo(
                page=1,
                page_size=10,
                total_records=0,
                total_pages=0,
                has_more=False,
            ),
            etag=None,
            last_modified=None,
            not_modified=True,
        )
    else:
        provider.document_payloads = deque(
            (
                BedestenDocumentPayload(
                    data=b"",
                    mime_type="text/html",
                    source_url=_DOCUMENT_SOURCE_URL,
                    etag=None,
                    last_modified=None,
                    not_modified=True,
                ),
            )
        )

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        service = DecisionService(provider, cache)
        if operation == "search":
            result = await service.search(_request())
        else:
            result = await service.get_document(f"bedesten:{_DOCUMENT_ID}")

    assert result == {
        "ok": False,
        "error": {
            "code": "upstream_unavailable",
            "message": f"Üst kaynak şu anda kullanılamıyor. Ayrıntı: {detail}",
            "retryable": True,
            "details": {"upstream": "bedesten"},
        },
    }


@pytest.mark.asyncio
async def test_decision_service_translates_invalid_cached_search_entry(
    tmp_path: Path,
) -> None:
    provider = StubDecisionProvider()
    request = _request()
    key = canonical_parameter_hash(
        request.model_dump(mode="json"), unordered_fields=("courts",)
    )
    now = datetime.now(UTC)

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        cache.put(
            namespace=_SEARCH_NAMESPACE,
            key=key,
            source_url=SEARCH_URL,
            content="{}",
            fetched_at=now,
            validated_at=now,
            expires_at=now + timedelta(days=1),
        )
        result = await DecisionService(provider, cache).search(request)

    assert result == {
        "ok": False,
        "error": {
            "code": "upstream_unavailable",
            "message": (
                "Üst kaynak şu anda kullanılamıyor. Ayrıntı: Önbellekteki Bedesten "
                "karar arama kaydı geçersiz."
            ),
            "retryable": True,
            "details": {"upstream": "bedesten"},
        },
    }
    assert provider.search_calls == []


@pytest.mark.asyncio
async def test_decision_service_translates_missing_cached_document_conversion_metadata(
    tmp_path: Path,
) -> None:
    provider = StubDecisionProvider()
    now = datetime.now(UTC)

    with CacheStore(tmp_path / "decisions.sqlite") as cache:
        cache.put(
            namespace=_DOCUMENT_NAMESPACE,
            key=f"bedesten:{_DOCUMENT_ID}",
            source_url=_DOCUMENT_SOURCE_URL,
            content="Önbelleğe alınmış karar metni.",
            fetched_at=now,
            validated_at=now,
            expires_at=None,
            mime_type="text/markdown",
        )
        result = await DecisionService(provider, cache).get_document(
            f"bedesten:{_DOCUMENT_ID}"
        )

    assert result == {
        "ok": False,
        "error": {
            "code": "upstream_unavailable",
            "message": (
                "Üst kaynak şu anda kullanılamıyor. Ayrıntı: Önbelleğe alınmış "
                "Bedesten belgesinde dönüşüm metaverisi yok."
            ),
            "retryable": True,
            "details": {"upstream": "bedesten"},
        },
    }
    assert provider.document_calls == []
