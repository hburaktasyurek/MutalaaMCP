"""Isolated Bedesten legislation provider and cache-service contract tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator

import mutalaamcp.services.legislation as legislation_service
from mutalaamcp.cache.store import CacheStore
from mutalaamcp.conversion.documents import ConvertedDocument
from mutalaamcp.domain.models import (
    ConversionMethod,
    LegislationHit,
    LegislationType,
    PageInfo,
)
from mutalaamcp.net import (
    SafeHttpClient,
    UpstreamNotFound,
    UpstreamUnavailable,
)
from mutalaamcp.providers.bedesten.legislation import (
    BASE_URL,
    BedestenLegislationProvider,
    InvalidLegislationType,
    LegislationNotFound,
    LegislationProviderError,
    ProviderDocument,
    ProviderOutline,
    SearchPage,
)
from mutalaamcp.tool_schemas import standalone_tool_schema

SOURCE_URL = "https://bedesten.adalet.gov.tr/mevzuat/mevzuatDetay/law"
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@dataclass
class _Response:
    status_code: int
    payload: object
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> object:
        return self.payload


class _FakeHttp:
    def __init__(self, *responses: _Response) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls.append((method, url, dict(kwargs)))
        if not self._responses:
            raise AssertionError("unexpected Bedesten request")
        return self._responses.pop(0)


class _ScriptedProvider:
    """Provider fake with only queued local responses and recorded calls."""

    def __init__(
        self,
        *,
        searches: tuple[SearchPage | None | Exception, ...] = (),
        documents: tuple[ProviderDocument | Exception, ...] = (),
        outlines: tuple[object | Exception, ...] = (),
        articles: tuple[ProviderDocument | Exception, ...] = (),
    ) -> None:
        self._searches = list(searches)
        self._documents = list(documents)
        self._outlines = list(outlines)
        self._articles = list(articles)
        self.search_calls: list[tuple[dict[str, object], dict[str, str]]] = []
        self.document_headers: list[dict[str, str]] = []
        self.outline_headers: list[dict[str, str]] = []
        self.article_calls: list[tuple[str, str, dict[str, str]]] = []
        self.search_started = asyncio.Event()
        self.release_search = asyncio.Event()
        self.block_search = False
        self.document_entered: asyncio.Queue[None] = asyncio.Queue()
        self.release_document = asyncio.Event()
        self.block_document = False

    @staticmethod
    def _take(items: list[object]) -> object:
        if not items:
            raise AssertionError("unexpected provider call")
        result = items.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def search(self, **params: object) -> SearchPage | None:
        headers = params.pop("conditional_headers", None)
        assert headers is None or isinstance(headers, Mapping)
        self.search_calls.append((dict(params), dict(headers or {})))
        self.search_started.set()
        if self.block_search:
            await self.release_search.wait()
        return self._take(self._searches)  # type: ignore[return-value]

    async def get_document(
        self, opaque: str, *, conditional_headers: Mapping[str, str] | None = None
    ) -> ProviderDocument:
        assert opaque == "law"
        self.document_headers.append(dict(conditional_headers or {}))
        self.document_entered.put_nowait(None)
        if self.block_document:
            await self.release_document.wait()
        return self._take(self._documents)  # type: ignore[return-value]

    async def get_outline(
        self, opaque: str, *, conditional_headers: Mapping[str, str] | None = None
    ) -> object:
        assert opaque == "law"
        self.outline_headers.append(dict(conditional_headers or {}))
        return self._take(self._outlines)

    async def get_article(
        self,
        article_id: str,
        *,
        legislation_id: str,
        conditional_headers: Mapping[str, str] | None = None,
    ) -> ProviderDocument:
        self.article_calls.append(
            (article_id, legislation_id, dict(conditional_headers or {}))
        )
        return self._take(self._articles)  # type: ignore[return-value]


@dataclass
class _Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def _document(
    markdown: str,
    *,
    etag: str = "etag-1",
    last_modified: str = "Mon, 01 Sep 2026 12:00:00 GMT",
) -> ProviderDocument:
    return ProviderDocument(
        content=markdown.encode("utf-8"),
        mime_type="text/html",
        source_url=SOURCE_URL,
        etag=etag,
        last_modified=last_modified,
    )


def _not_modified(*, etag: str, last_modified: str) -> ProviderDocument:
    return ProviderDocument(
        content=None,
        mime_type=None,
        source_url=SOURCE_URL,
        etag=etag,
        last_modified=last_modified,
        not_modified=True,
    )


def _search_page(
    *,
    title: str = "Fixture Regulation",
    etag: str = "search-etag-1",
    last_modified: str = "Mon, 01 Sep 2026 12:00:00 GMT",
    not_modified: bool = False,
) -> SearchPage:
    return SearchPage(
        hits=(
            LegislationHit(
                id="mevzuat:law",
                source_url=SOURCE_URL,
                legislation_type=LegislationType.KANUN,
                number="42",
                title=title,
            ),
        ),
        page=PageInfo(
            page=1,
            page_size=20,
            total_records=1,
            total_pages=1,
            has_more=False,
        ),
        etag=etag,
        last_modified=last_modified,
        not_modified=not_modified,
    )


def _install_text_converter(monkeypatch: pytest.MonkeyPatch) -> None:
    async def convert(
        data: bytes, mime_type: str, source_url: str
    ) -> ConvertedDocument:
        markdown = data.decode("utf-8")
        return ConvertedDocument(
            markdown=markdown,
            mime_type=mime_type,
            conversion=ConversionMethod.HTML_MARKDOWN,
            content_hash="sha256:"
            + hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        )

    monkeypatch.setattr(legislation_service, "convert_document", convert)


def _tamper_cache_hash(
    cache: CacheStore, namespace: str, key: str, *, content_hash: str
) -> None:
    assert cache._conn is not None
    cache._conn.execute(
        "UPDATE cache_entry SET content_hash = ? WHERE namespace = ? AND key = ?",
        (content_hash, namespace, key),
    )
    cache._conn.commit()


@pytest.mark.asyncio
async def test_search_defaults_to_the_exact_twelve_types_and_normalizes_hit_payloads_async() -> (
    None
):
    response = _Response(
        200,
        {
            "metadata": {"FMTY": "SUCCESS"},
            "data": {
                "mevzuatList": [
                    {
                        "mevzuatId": 42,
                        "mevzuatTur": {"code": "KANUN"},
                        "mevzuatAdi": "Kişisel Verilerin Korunması Kanunu",
                        "mevzuatNo": 6698,
                        "resmiGazeteTarihi": "07/04/2016",
                        "resmiGazeteSayisi": 29677,
                        "highlight": "kişisel veri",
                        "url": "https://mevzuat.gov.tr/MevzuatMetin/1.5.6698.pdf",
                    }
                ],
                "total": "21",
            },
        },
        {"etag": "search-v1", "last-modified": "Mon, 01 Sep 2026 12:00:00 GMT"},
    )
    http = _FakeHttp(response)
    provider = BedestenLegislationProvider(http)  # type: ignore[arg-type]

    page = await provider.search(query="  kişisel veri  ", include_snippet=True)

    assert page is not None
    assert [member.value for member in LegislationType] == [
        "KANUN",
        "KHK",
        "TUZUK",
        "CB_KARARNAME",
        "YONETMELIK",
        "CB_YONETMELIK",
        "CB_KARAR",
        "CB_GENELGE",
        "KKY",
        "UY",
        "TEBLIGLER",
        "MULGA",
    ]
    assert page.page.total_records == 21
    assert page.page.total_pages == 2
    assert page.page.has_more
    assert page.etag == "search-v1"
    hit = page.hits[0]
    assert hit.id == "mevzuat:42"
    assert hit.legislation_type is LegislationType.KANUN
    assert hit.number == "6698"
    assert hit.official_gazette_date.isoformat() == "2016-04-07"
    assert hit.official_gazette_issue == "29677"
    assert hit.snippet == "kişisel veri"
    assert hit.source_url == "https://mevzuat.gov.tr/MevzuatMetin/1.5.6698.pdf"

    method, url, kwargs = http.calls[0]
    assert method == "POST"
    assert url == f"{BASE_URL}/searchDocuments"
    payload = kwargs["json"]
    assert isinstance(payload, dict)
    assert payload["data"] == {
        "pageSize": 20,
        "pageNumber": 1,
        "sortFields": ["RESMI_GAZETE_TARIHI"],
        "sortDirection": "desc",
        "mevzuatTurList": [member.value for member in LegislationType],
        "phrase": "kişisel veri",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title",
    (
        "Türkiye'de Kamu-Özel (T.C.), Kanun*",
        "Kanun?",
    ),
)
async def test_bedesten_title_search_accepts_documented_wildcards_and_turkish_punctuation(
    title: str,
) -> None:
    response = _Response(
        200,
        {
            "metadata": {"FMTY": "SUCCESS"},
            "data": {"mevzuatList": [], "total": 0},
        },
    )
    http = _FakeHttp(response)

    await BedestenLegislationProvider(http).search(title=title)  # type: ignore[arg-type]

    payload = http.calls[0][2]["json"]
    assert isinstance(payload, dict)
    assert payload["data"]["mevzuatAdi"] == title


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "message"),
    (
        (
            "borçlar^2",
            "title alanı desteklenmeyen arama söz dizimi veya noktalama içeriyor.",
        ),
        (
            "kanun~",
            "title alanı desteklenmeyen arama söz dizimi veya noktalama içeriyor.",
        ),
        (
            '"kanun"',
            "title alanı desteklenmeyen arama söz dizimi veya noktalama içeriyor.",
        ),
        (
            "kanun:123",
            "title alanı desteklenmeyen arama söz dizimi veya noktalama içeriyor.",
        ),
        ("*kanun", "title alanında * yalnızca sözcük sonunda kullanılabilir."),
        ("-kanun", "title alanında tire yalnızca sözcük içinde kullanılabilir."),
    ),
)
async def test_legislation_title_rejects_unsupported_solr_syntax_locally(
    tmp_path: Path, title: str, message: str
) -> None:
    http = _FakeHttp()
    provider = BedestenLegislationProvider(http)  # type: ignore[arg-type]

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        result = await legislation_service.LegislationService(provider, cache).search(
            title=title
        )

    assert result["ok"] is False
    assert result["error"] == {
        "code": "invalid_params",
        "message": "İstek parametreleri geçersiz.",
        "retryable": False,
        "details": {"field_errors": {"title": [message]}},
    }
    assert http.calls == []


@pytest.mark.asyncio
async def test_invalid_title_syntax_cannot_return_a_poisoned_cache(
    tmp_path: Path,
) -> None:
    title = "kanun:123"
    provider = _ScriptedProvider()
    poisoned_request = legislation_service.SearchLegislationRequest.model_construct(
        title=title
    )
    key = legislation_service.canonical_parameter_hash(
        poisoned_request.model_dump(mode="json"), unordered_fields=("types",)
    )
    now = legislation_service.utc_now()

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        cache.put(
            namespace=legislation_service._SEARCH_NAMESPACE,
            key=key,
            source_url=legislation_service._SEARCH_SOURCE_URL,
            content=legislation_service._serialize_search(
                _search_page(title="Poisoned cached result")
            ),
            fetched_at=now,
            validated_at=now,
            expires_at=now + legislation_service.SEARCH_CACHE_TTL,
        )
        result = await legislation_service.LegislationService(provider, cache).search(
            title=title
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_params"
    assert provider.search_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    (
        {"metadata": {"FMTY": "SUCCESS"}, "data": {"total": 0}},
        {"metadata": {"FMTY": "SUCCESS"}, "data": {"mevzuatList": []}},
    ),
)
async def test_legislation_search_rejects_malformed_success_envelopes_without_caching(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    http = _FakeHttp(_Response(200, payload), _Response(200, payload))
    provider = BedestenLegislationProvider(http)  # type: ignore[arg-type]

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)
        first = await service.search(query="fixture")
        second = await service.search(query="fixture")

    assert first["error"]["code"] == "upstream_unavailable"
    assert second["error"]["code"] == "upstream_unavailable"
    assert len(http.calls) == 2


def _metadata_operation_payload(operation: str, metadata: object) -> dict[str, object]:
    if operation == "search":
        data: object = {
            "mevzuatList": [],
            "total": 0,
            "start": 0,
        }
    elif operation == "document":
        data = {
            "content": base64.b64encode(
                b"<html><body>Sentetik mevzuat</body></html>"
            ).decode("ascii"),
            "mimeType": "text/html",
        }
    elif operation == "outline":
        data = {
            "maddeId": "synthetic-root",
            "title": "SENTETİK MEVZUAT",
            "children": [],
        }
    else:
        raise AssertionError(f"unsupported operation: {operation}")
    return {"data": data, "metadata": metadata}


async def _invoke_metadata_operation(
    provider: BedestenLegislationProvider, operation: str
) -> object:
    if operation == "search":
        return await provider.search(query="sentetik")
    if operation == "document":
        return await provider.get_document("synthetic-law")
    if operation == "outline":
        return await provider.get_outline("synthetic-law")
    raise AssertionError(f"unsupported operation: {operation}")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("search", "document", "outline"))
async def test_bedesten_operations_reject_missing_metadata(operation: str) -> None:
    payload = _metadata_operation_payload(operation, {"FMTY": "SUCCESS"})
    del payload["metadata"]
    http = _FakeHttp(_Response(200, payload))

    with pytest.raises(LegislationProviderError, match="metadata"):
        await _invoke_metadata_operation(BedestenLegislationProvider(http), operation)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", (None, [], "SUCCESS"))
@pytest.mark.parametrize("operation", ("search", "document", "outline"))
async def test_bedesten_operations_reject_non_object_metadata(
    operation: str, metadata: object
) -> None:
    http = _FakeHttp(_Response(200, _metadata_operation_payload(operation, metadata)))

    with pytest.raises(LegislationProviderError, match="metadata"):
        await _invoke_metadata_operation(BedestenLegislationProvider(http), operation)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("search", "document", "outline"))
async def test_bedesten_operations_reject_error_metadata_without_upstream_detail(
    operation: str,
) -> None:
    secret = "Sentetik Bedesten yanıt hatası."
    http = _FakeHttp(
        _Response(
            200,
            _metadata_operation_payload(
                operation,
                {
                    "FMTY": "ERROR",
                    "FMC": "MEVZUAT_ERROR_0005",
                    "FMU": secret,
                },
            ),
        )
    )

    with pytest.raises(LegislationProviderError) as error:
        await _invoke_metadata_operation(BedestenLegislationProvider(http), operation)  # type: ignore[arg-type]

    assert str(error.value) == "Bedesten yanıtında metadata.FMTY SUCCESS değil."
    assert secret not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("search", "document", "outline"))
async def test_bedesten_operations_accept_explicit_success_metadata(
    operation: str,
) -> None:
    http = _FakeHttp(
        _Response(
            200,
            _metadata_operation_payload(operation, {"FMTY": "SUCCESS"}),
        )
    )

    result = await _invoke_metadata_operation(
        BedestenLegislationProvider(http), operation
    )  # type: ignore[arg-type]

    if operation == "search":
        assert isinstance(result, SearchPage)
    elif operation == "document":
        assert isinstance(result, ProviderDocument)
        assert result.content == b"<html><body>Sentetik mevzuat</body></html>"
    else:
        assert isinstance(result, ProviderOutline)
        assert result.tree == {
            "maddeId": "synthetic-root",
            "title": "SENTETİK MEVZUAT",
            "children": [],
        }


@pytest.mark.asyncio
async def test_bedesten_outline_success_null_data_is_an_empty_tree() -> None:
    http = _FakeHttp(
        _Response(
            200,
            {"metadata": {"FMTY": "SUCCESS"}, "data": None},
            {"etag": "outline-empty", "last-modified": "empty-time"},
        )
    )

    outline = await BedestenLegislationProvider(http).get_outline("law")  # type: ignore[arg-type]

    assert outline == ProviderOutline(
        tree=[],
        etag="outline-empty",
        last_modified="empty-time",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tree", (0, True, "not-an-outline"))
async def test_bedesten_outline_rejects_scalar_success_data(tree: object) -> None:
    http = _FakeHttp(_Response(200, {"metadata": {"FMTY": "SUCCESS"}, "data": tree}))

    with pytest.raises(LegislationProviderError, match="data"):
        await BedestenLegislationProvider(http).get_outline("law")  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("search", "document", "outline"))
async def test_bedesten_operations_preserve_not_found_metadata_mapping(
    operation: str,
) -> None:
    http = _FakeHttp(
        _Response(
            200,
            _metadata_operation_payload(
                operation,
                {
                    "FMTY": "ERROR",
                    "FMC": "MEVZUAT_NOT_FOUND",
                    "FMU": "Mevzuat bulunamadı.",
                },
            ),
        )
    )

    with pytest.raises(LegislationNotFound) as error:
        await _invoke_metadata_operation(BedestenLegislationProvider(http), operation)  # type: ignore[arg-type]

    assert str(error.value) == "İstenen belge Bedesten'de bulunamadı."


@pytest.mark.asyncio
async def test_bedesten_search_clamps_minimum_gazette_date_start() -> None:
    response = _Response(
        200,
        {
            "metadata": {"FMTY": "SUCCESS"},
            "data": {"mevzuatList": [], "total": 0},
        },
    )
    http = _FakeHttp(response)

    await BedestenLegislationProvider(http).search(  # type: ignore[arg-type]
        number="1", gazette_date_from=date.min
    )

    payload = http.calls[0][2]["json"]
    assert isinstance(payload, dict)
    assert payload["data"]["resmiGazeteTarihiStart"] == "0001-01-01T00:00:00.000Z"


@pytest.mark.asyncio
async def test_bedesten_provider_translates_conditional_304_responses_with_mock_transport() -> (
    None
):
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            304,
            headers={
                "ETag": '"revalidated-v2"',
                "Last-Modified": "Tue, 02 Sep 2026 12:00:00 GMT",
            },
            request=request,
        )

    conditional_headers = {
        "If-None-Match": '"cached-v1"',
        "If-Modified-Since": "Mon, 01 Sep 2026 12:00:00 GMT",
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as raw:
        provider = BedestenLegislationProvider(
            SafeHttpClient(raw, {"bedesten.adalet.gov.tr"})
        )
        search = await provider.search(
            query="fixture", conditional_headers=conditional_headers
        )
        document = await provider.get_document(
            "law", conditional_headers=conditional_headers
        )
        article = await provider.get_article(
            "article-1",
            legislation_id="law",
            conditional_headers=conditional_headers,
        )
        outline = await provider.get_outline(
            "law", conditional_headers=conditional_headers
        )

    assert isinstance(search, SearchPage)
    assert search.not_modified
    assert search.etag == '"revalidated-v2"'
    assert search.last_modified == "Tue, 02 Sep 2026 12:00:00 GMT"
    assert search.page.page == 1
    assert search.page.page_size == 20
    assert document == ProviderDocument(
        content=None,
        mime_type=None,
        source_url=f"{BASE_URL}/mevzuatDetay/law",
        etag='"revalidated-v2"',
        last_modified="Tue, 02 Sep 2026 12:00:00 GMT",
        not_modified=True,
    )
    assert article == ProviderDocument(
        content=None,
        mime_type=None,
        source_url=f"{BASE_URL}/mevzuatDetay/law",
        etag='"revalidated-v2"',
        last_modified="Tue, 02 Sep 2026 12:00:00 GMT",
        not_modified=True,
    )
    assert outline == ProviderOutline(
        tree=None,
        etag='"revalidated-v2"',
        last_modified="Tue, 02 Sep 2026 12:00:00 GMT",
        not_modified=True,
    )
    assert [request.url.path for request in requests] == [
        "/mevzuat/searchDocuments",
        "/mevzuat/getDocumentContent",
        "/mevzuat/getDocumentContent",
        "/mevzuat/mevzuatMaddeTree",
    ]
    assert [
        (
            request.headers["if-none-match"],
            request.headers["if-modified-since"],
        )
        for request in requests
    ] == [tuple(conditional_headers.values())] * 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "types",
    (
        (),
        ("KANUN", "KANUN"),
        ("KANUN", "NOT_A_TYPE"),
        "KANUN",
        (object(),),
    ),
)
async def test_search_rejects_any_type_outside_the_strict_twelve_type_contract(
    types: object,
) -> None:
    http = _FakeHttp()
    provider = BedestenLegislationProvider(http)  # type: ignore[arg-type]

    with pytest.raises(InvalidLegislationType):
        await provider.search(query="veri", types=types)  # type: ignore[arg-type]

    assert http.calls == []


@pytest.mark.asyncio
async def test_bedesten_full_tree_and_madde_routes_decode_local_payloads() -> None:
    encoded_document = base64.b64encode(b"<main>MADDE 1</main>").decode("ascii")
    http = _FakeHttp(
        _Response(
            200,
            {
                "metadata": {"FMTY": "SUCCESS"},
                "data": {"content": encoded_document, "mimeType": "text/html"},
            },
            {"etag": "full-v1", "last-modified": "full-time"},
        ),
        _Response(
            200,
            {
                "metadata": {"FMTY": "SUCCESS"},
                "data": {"content": "<article>MADDE 2</article>"},
            },
            {"etag": "article-v1", "last-modified": "article-time"},
        ),
        _Response(
            200,
            {
                "metadata": {"FMTY": "SUCCESS"},
                "data": [
                    {
                        "baslik": "BİRİNCİ BÖLÜM",
                        "altMaddeler": [{"maddeNo": "2", "maddeId": "m-2"}],
                    }
                ],
            },
            {"etag": "tree-v1", "last-modified": "tree-time"},
        ),
    )
    provider = BedestenLegislationProvider(http)  # type: ignore[arg-type]

    full = await provider.get_document("law")
    article = await provider.get_article("m-2", legislation_id="law")
    tree = await provider.get_outline("law")

    assert full.content == b"<main>MADDE 1</main>"
    assert full.mime_type == "text/html"
    assert full.source_url == f"{BASE_URL}/mevzuatDetay/law"
    assert full.etag == "full-v1"
    assert article.content == b"<article>MADDE 2</article>"
    assert article.mime_type == "text/html"
    assert article.source_url == f"{BASE_URL}/mevzuatDetay/law"
    assert tree == ProviderOutline(
        tree=[
            {
                "baslik": "BİRİNCİ BÖLÜM",
                "altMaddeler": [{"maddeNo": "2", "maddeId": "m-2"}],
            }
        ],
        etag="tree-v1",
        last_modified="tree-time",
    )

    assert [call[1] for call in http.calls] == [
        f"{BASE_URL}/getDocumentContent",
        f"{BASE_URL}/getDocumentContent",
        f"{BASE_URL}/mevzuatMaddeTree",
    ]
    assert http.calls[0][2]["json"] == {
        "applicationName": "UyapMevzuat",
        "data": {"documentType": "MEVZUAT", "id": "law"},
    }
    assert http.calls[1][2]["json"] == {
        "applicationName": "UyapMevzuat",
        "data": {"documentType": "MADDE", "id": "m-2"},
    }
    assert http.calls[2][2]["json"] == {
        "applicationName": "UyapMevzuat",
        "data": {"mevzuatId": "law"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "expected_selectors"),
    (
        (
            {"query": "fixture phrase"},
            {
                "query": "fixture phrase",
                "title": None,
                "number": None,
                "exact_title": False,
            },
        ),
        (
            {"title": "Fixture Regulation"},
            {
                "query": None,
                "title": "Fixture Regulation",
                "number": None,
                "exact_title": False,
            },
        ),
        (
            {"title": "Fixture Regulation", "exact_title": True},
            {
                "query": None,
                "title": "Fixture Regulation",
                "number": None,
                "exact_title": True,
            },
        ),
        (
            {"number": "42"},
            {
                "query": None,
                "title": None,
                "number": "42",
                "exact_title": False,
            },
        ),
    ),
)
async def test_search_service_forwards_each_selector_and_returns_public_envelope(
    tmp_path: Path,
    params: dict[str, object],
    expected_selectors: dict[str, object],
) -> None:
    provider = _ScriptedProvider(searches=(_search_page(),))

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        result = await service.search(**params)

    assert set(result) == {"ok", "hits", "page", "expires_at", "warnings"}
    assert result["ok"] is True
    assert result["hits"] == [
        {
            "id": "mevzuat:law",
            "source_url": SOURCE_URL,
            "title": "Fixture Regulation",
            "source": "bedesten",
            "legislation_type": "KANUN",
            "number": "42",
            "official_gazette_date": None,
            "official_gazette_issue": None,
            "snippet": None,
        }
    ]
    assert result["page"] == {
        "page": 1,
        "page_size": 20,
        "total_records": 1,
        "total_pages": 1,
        "has_more": False,
    }
    assert isinstance(result["expires_at"], str)
    assert result["warnings"] == []
    provider_params, conditional_headers = provider.search_calls[0]
    assert {
        name: provider_params[name]
        for name in ("query", "title", "number", "exact_title")
    } == expected_selectors
    assert conditional_headers == {}


@pytest.mark.asyncio
async def test_search_service_cache_keys_isolate_all_search_selectors(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider(
        searches=(
            _search_page(title="Phrase result"),
            _search_page(title="Title result"),
            _search_page(title="Exact title result"),
            _search_page(title="Number result"),
        )
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        by_query = await service.search(query="same value")
        by_title = await service.search(title="same value")
        by_exact_title = await service.search(title="same value", exact_title=True)
        by_number = await service.search(number="same value")
        repeated_query = await service.search(query="same value")

    assert [
        result["hits"][0]["title"]
        for result in (by_query, by_title, by_exact_title, by_number)
    ] == [
        "Phrase result",
        "Title result",
        "Exact title result",
        "Number result",
    ]
    assert repeated_query["hits"][0]["title"] == "Phrase result"
    assert len(provider.search_calls) == 4


@pytest.mark.asyncio
async def test_search_service_uses_fresh_cache_then_conditionally_refetches_expired_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    provider = _ScriptedProvider(
        searches=(
            _search_page(etag="search-v1", last_modified="search-time-v1"),
            _search_page(
                etag="search-v2",
                last_modified="search-time-v2",
                not_modified=True,
            ),
        )
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        first = await service.search(query="fixture")

        clock.now = T0 + timedelta(hours=23, minutes=59)
        fresh = await service.search(query="fixture")

        clock.now = T0 + timedelta(hours=24)
        revalidated = await service.search(query="fixture")
        key = legislation_service.canonical_parameter_hash(
            legislation_service.SearchLegislationRequest(query="fixture").model_dump(
                mode="json"
            ),
            unordered_fields=("types",),
        )
        cached = cache.get(legislation_service._SEARCH_NAMESPACE, key, now=clock.now)

    assert fresh == first
    assert revalidated["hits"] == first["hits"]
    assert revalidated["expires_at"] != first["expires_at"]
    assert len(provider.search_calls) == 2
    assert cached is not None
    assert cached.etag == "search-v2"
    assert cached.last_modified == "search-time-v2"
    assert provider.search_calls[1][1] == {
        "If-None-Match": "search-v1",
        "If-Modified-Since": "search-time-v1",
    }


@pytest.mark.asyncio
async def test_outline_service_rotates_validators_after_304(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    tree = [{"baslik": "BİRİNCİ BÖLÜM", "altMaddeler": []}]
    provider = _ScriptedProvider(
        outlines=(
            (tree, "outline-v1", "outline-time-v1"),
            ProviderOutline(
                tree=None,
                etag="outline-v2",
                last_modified="outline-time-v2",
                not_modified=True,
            ),
        )
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        first = await service.get_outline("mevzuat:law")
        clock.now = T0 + timedelta(hours=24)
        revalidated = await service.get_outline("mevzuat:law")
        cached = cache.get(legislation_service._OUTLINE_NAMESPACE, "law", now=clock.now)

    assert revalidated["nodes"] == first["nodes"]
    assert revalidated["expires_at"] != first["expires_at"]
    assert cached is not None
    assert cached.etag == "outline-v2"
    assert cached.last_modified == "outline-time-v2"
    assert provider.outline_headers[1] == {
        "If-None-Match": "outline-v1",
        "If-Modified-Since": "outline-time-v1",
    }


@pytest.mark.asyncio
async def test_outline_service_normalizes_null_tree_to_empty_nodes(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider(outlines=((None, "outline-empty", "empty-time"),))

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        result = await service.get_outline("mevzuat:law")
        cached = cache.get(legislation_service._OUTLINE_NAMESPACE, "law")

    assert result["ok"] is True
    assert result["nodes"] == []
    assert cached is not None
    assert cached.content == "[]"


@pytest.mark.asyncio
async def test_outline_service_does_not_cache_or_reuse_scalar_tree_data(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider(
        outlines=(
            ("invalid scalar", "outline-v1", "outline-time-v1"),
            ("invalid scalar", "outline-v2", "outline-time-v2"),
        )
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        first = await service.get_outline("mevzuat:law")
        second = await service.get_outline("mevzuat:law")
        cached = cache.get(legislation_service._OUTLINE_NAMESPACE, "law")

    assert first["ok"] is False
    assert second["ok"] is False
    assert first["error"]["code"] == "upstream_unavailable"
    assert second["error"]["code"] == "upstream_unavailable"
    assert len(provider.outline_headers) == 2
    assert cached is None


@pytest.mark.asyncio
async def test_search_304_does_not_validate_invalid_stale_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    provider = _ScriptedProvider(
        searches=(
            _search_page(
                etag="search-v2",
                last_modified="search-time-v2",
                not_modified=True,
            ),
        )
    )
    request = legislation_service.SearchLegislationRequest(query="fixture")
    key = legislation_service.canonical_parameter_hash(
        request.model_dump(mode="json"), unordered_fields=("types",)
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        cache.put(
            namespace=legislation_service._SEARCH_NAMESPACE,
            key=key,
            source_url=legislation_service._SEARCH_SOURCE_URL,
            content="{}",
            fetched_at=T0,
            validated_at=T0,
            expires_at=T0,
            etag="search-v1",
            last_modified="search-time-v1",
        )
        result = await legislation_service.LegislationService(provider, cache).search(
            query="fixture"
        )
        cached = cache.get(legislation_service._SEARCH_NAMESPACE, key, now=clock.now)

    assert result["ok"] is False
    assert result["error"]["code"] == "upstream_unavailable"
    assert cached is not None
    assert cached.freshness is legislation_service.Freshness.EXPIRED
    assert cached.validated_at == T0
    assert cached.etag == "search-v1"
    assert cached.last_modified == "search-time-v1"


@pytest.mark.asyncio
async def test_document_304_does_not_validate_invalid_stale_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    provider = _ScriptedProvider(
        documents=(_not_modified(etag="document-v2", last_modified="document-time-v2"),)
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        cache.put(
            namespace=legislation_service._DOCUMENT_NAMESPACE,
            key="law",
            source_url=SOURCE_URL,
            content="MADDE 1\nEski içerik.",
            fetched_at=T0,
            validated_at=T0,
            expires_at=T0,
            etag="document-v1",
            last_modified="document-time-v1",
        )
        result = await legislation_service.LegislationService(
            provider, cache
        ).get_document("mevzuat:law")
        cached = cache.get(
            legislation_service._DOCUMENT_NAMESPACE, "law", now=clock.now
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "upstream_unavailable"
    assert cached is not None
    assert cached.freshness is legislation_service.Freshness.EXPIRED
    assert cached.validated_at == T0
    assert cached.etag == "document-v1"
    assert cached.last_modified == "document-time-v1"


@pytest.mark.asyncio
async def test_article_304_does_not_validate_invalid_stale_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    tree = [{"maddeNo": "1", "maddeId": "article-1", "baslik": "Birinci"}]
    provider = _ScriptedProvider(
        articles=(_not_modified(etag="article-v2", last_modified="article-time-v2"),)
    )
    article_key = legislation_service.canonical_parameter_hash(
        {"legislation_id": "law", "article_number": "1"}
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        cache.put(
            namespace=legislation_service._OUTLINE_NAMESPACE,
            key="law",
            source_url=SOURCE_URL,
            content=json.dumps(tree, ensure_ascii=False),
            fetched_at=T0,
            validated_at=T0,
            expires_at=T0 + timedelta(days=1),
        )
        cache.put(
            namespace=legislation_service._ARTICLE_NAMESPACE,
            key=article_key,
            source_url=SOURCE_URL,
            content="MADDE 1\nEski madde.",
            fetched_at=T0,
            validated_at=T0,
            expires_at=T0,
            etag="article-v1",
            last_modified="article-time-v1",
        )
        result = await legislation_service.LegislationService(
            provider, cache
        ).get_article("mevzuat:law", "1")
        cached = cache.get(
            legislation_service._ARTICLE_NAMESPACE, article_key, now=clock.now
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "upstream_unavailable"
    assert cached is not None
    assert cached.freshness is legislation_service.Freshness.EXPIRED
    assert cached.validated_at == T0
    assert cached.etag == "article-v1"
    assert cached.last_modified == "article-time-v1"


@pytest.mark.asyncio
async def test_outline_304_does_not_validate_invalid_stale_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    provider = _ScriptedProvider(
        outlines=(
            ProviderOutline(
                tree=None,
                etag="outline-v2",
                last_modified="outline-time-v2",
                not_modified=True,
            ),
        )
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        cache.put(
            namespace=legislation_service._OUTLINE_NAMESPACE,
            key="law",
            source_url=SOURCE_URL,
            content="null",
            fetched_at=T0,
            validated_at=T0,
            expires_at=T0,
            etag="outline-v1",
            last_modified="outline-time-v1",
        )
        result = await legislation_service.LegislationService(
            provider, cache
        ).get_outline("mevzuat:law")
        cached = cache.get(legislation_service._OUTLINE_NAMESPACE, "law", now=clock.now)

    assert result["ok"] is False
    assert result["error"]["code"] == "upstream_unavailable"
    assert cached is not None
    assert cached.freshness is legislation_service.Freshness.EXPIRED
    assert cached.validated_at == T0
    assert cached.etag == "outline-v1"
    assert cached.last_modified == "outline-time-v1"


@pytest.mark.asyncio
async def test_search_fresh_cache_hash_mismatch_is_not_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    provider = _ScriptedProvider()
    request = legislation_service.SearchLegislationRequest(query="fixture")
    key = legislation_service.canonical_parameter_hash(
        request.model_dump(mode="json"), unordered_fields=("types",)
    )
    content = legislation_service._serialize_search(_search_page())
    bad_hash = "sha256:" + "0" * 64

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        cache.put(
            namespace=legislation_service._SEARCH_NAMESPACE,
            key=key,
            source_url=legislation_service._SEARCH_SOURCE_URL,
            content=content,
            fetched_at=T0,
            validated_at=T0,
            expires_at=T0 + timedelta(days=1),
            now=T0,
        )
        _tamper_cache_hash(
            cache, legislation_service._SEARCH_NAMESPACE, key, content_hash=bad_hash
        )
        result = await legislation_service.LegislationService(provider, cache).search(
            query="fixture"
        )
        cached = cache.get(legislation_service._SEARCH_NAMESPACE, key, now=clock.now)

    assert result["ok"] is False
    assert result["error"]["code"] == "upstream_unavailable"
    assert provider.search_calls == []
    assert cached is not None
    assert cached.content_hash == bad_hash
    assert cached.validated_at == T0
    assert cached.expires_at == T0 + timedelta(days=1)


@pytest.mark.asyncio
async def test_outline_stale_cache_hash_mismatch_is_not_used_as_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    provider = _ScriptedProvider(
        outlines=(UpstreamUnavailable("Bedesten erişilemedi", status_code=503),)
    )
    tree = [{"baslik": "BİRİNCİ BÖLÜM", "altMaddeler": []}]
    content = json.dumps(
        tree, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    bad_hash = "sha256:" + "0" * 64

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        cache.put(
            namespace=legislation_service._OUTLINE_NAMESPACE,
            key="law",
            source_url=legislation_service._source_url("law"),
            content=content,
            fetched_at=T0,
            validated_at=T0,
            expires_at=T0,
            etag="outline-v1",
            last_modified="outline-time-v1",
            now=T0,
        )
        _tamper_cache_hash(
            cache,
            legislation_service._OUTLINE_NAMESPACE,
            "law",
            content_hash=bad_hash,
        )
        result = await legislation_service.LegislationService(
            provider, cache
        ).get_outline("mevzuat:law")
        cached = cache.get(legislation_service._OUTLINE_NAMESPACE, "law", now=clock.now)

    assert result["ok"] is False
    assert result["error"]["code"] == "upstream_unavailable"
    assert cached is not None
    assert cached.content_hash == bad_hash
    assert cached.validated_at == T0
    assert cached.expires_at == T0
    assert cached.etag == "outline-v1"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ("fresh", "stale", "304"))
async def test_converted_cache_hash_mismatch_is_never_served_or_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    if path == "fresh":
        documents: tuple[ProviderDocument | Exception, ...] = ()
        expires_at = T0 + timedelta(days=1)
    elif path == "stale":
        documents = (UpstreamUnavailable("Bedesten erişilemedi", status_code=503),)
        expires_at = T0
    else:
        documents = (_not_modified(etag="document-v2", last_modified="l2"),)
        expires_at = T0
    provider = _ScriptedProvider(documents=documents)
    content = "MADDE 1\nÖnbelleğe alınan metin."
    bad_hash = "sha256:" + "0" * 64

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        cache.put(
            namespace=legislation_service._DOCUMENT_NAMESPACE,
            key="law",
            source_url=SOURCE_URL,
            content=content,
            fetched_at=T0,
            validated_at=T0,
            expires_at=expires_at,
            etag="document-v1",
            last_modified="l1",
            mime_type="text/markdown",
            conversion=ConversionMethod.HTML_MARKDOWN,
            now=T0,
        )
        _tamper_cache_hash(
            cache,
            legislation_service._DOCUMENT_NAMESPACE,
            "law",
            content_hash=bad_hash,
        )
        result = await legislation_service.LegislationService(
            provider, cache
        ).get_document("mevzuat:law")
        cached = cache.get(
            legislation_service._DOCUMENT_NAMESPACE, "law", now=clock.now
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "upstream_unavailable"
    assert cached is not None
    assert cached.content_hash == bad_hash
    assert cached.validated_at == T0
    assert cached.expires_at == expires_at
    assert cached.etag == "document-v1"
    if path == "fresh":
        assert provider.document_headers == []


@pytest.mark.asyncio
async def test_search_service_returns_eligible_stale_result_for_network_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    provider = _ScriptedProvider(
        searches=(
            _search_page(etag="search-v1", last_modified="search-time-v1"),
            UpstreamUnavailable("Bedesten erişilemedi"),
        )
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        await service.search(query="fixture")
        clock.now = T0 + timedelta(hours=24)
        stale = await service.search(query="fixture")

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
    assert stale["warnings"] == [
        {
            "code": "stale_content",
            "message": "Önbelleğe alınan içerik, üst kaynak yeniden doğrulaması kullanılamadığı için güncel değildir.",
        }
    ]
    assert stale["revalidation_error"] == {
        "code": "upstream_unavailable",
        "message": "Üst kaynak şu anda kullanılamıyor. Ayrıntı: Bedesten erişilemedi",
    }
    assert provider.search_calls[1][1] == {
        "If-None-Match": "search-v1",
        "If-Modified-Since": "search-time-v1",
    }


@pytest.mark.asyncio
async def test_search_service_does_not_serve_stale_result_after_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    provider = _ScriptedProvider(
        searches=(
            _search_page(),
            UpstreamNotFound("mevzuat bulunamadı", status_code=404),
        )
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        await service.search(query="fixture")
        clock.now = T0 + timedelta(hours=24)
        result = await service.search(query="fixture")

    assert result == {
        "ok": False,
        "error": {
            "code": "upstream_unavailable",
            "message": "Üst kaynak istenen belgeyi bulamadı. Ayrıntı: mevzuat bulunamadı",
            "retryable": True,
            "details": {"upstream": "bedesten"},
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "field_errors"),
    (
        (
            {},
            {"request": ["query, title veya number alanlarından biri gereklidir."]},
        ),
        (
            {"exact_title": True},
            {"request": ["query, title veya number alanlarından biri gereklidir."]},
        ),
        (
            {"query": "fixture", "exact_title": True},
            {"request": ["exact_title için title gereklidir."]},
        ),
        (
            {
                "query": "fixture",
                "gazette_date_from": date(2026, 9, 2),
                "gazette_date_to": date(2026, 9, 1),
            },
            {
                "request": [
                    "gazette_date_from, gazette_date_to değerinden sonra olamaz."
                ]
            },
        ),
        (
            {"query": "fixture", "types": ["KANUN", "KANUN"]},
            {"types": ["types alanı yinelenen değerler içermemelidir."]},
        ),
        (
            {"query": "fixture", "page": 0},
            {"page": ["1 veya daha büyük olmalıdır."]},
        ),
        (
            {"query": "fixture", "types": ["NOT_A_TYPE"]},
            {"types.0": ["Geçerli bir mevzuat türü olmalıdır."]},
        ),
    ),
)
async def test_search_service_maps_invalid_params_to_the_public_error_envelope(
    tmp_path: Path,
    params: dict[str, object],
    field_errors: dict[str, list[str]],
) -> None:
    provider = _ScriptedProvider()

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        result = await service.search(**params)

    assert result["ok"] is False
    assert result["error"] == {
        "code": "invalid_params",
        "message": "İstek parametreleri geçersiz.",
        "retryable": False,
        "details": {"field_errors": field_errors},
    }
    assert provider.search_calls == []


@pytest.mark.asyncio
async def test_search_service_singleflight_coalesces_identical_calls(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider(searches=(_search_page(),))
    provider.block_search = True

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        first_task = asyncio.create_task(service.search(query="fixture"))
        await provider.search_started.wait()
        second_task = asyncio.create_task(service.search(query="fixture"))
        await asyncio.sleep(0)
        provider.release_search.set()
        first, second = await asyncio.gather(first_task, second_task)

    assert first == second
    assert first["ok"] is True
    assert len(provider.search_calls) == 1


@pytest.mark.asyncio
async def test_document_cache_is_24_hours_and_revalidates_conditionally_or_by_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_text_converter(monkeypatch)
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    provider = _ScriptedProvider(
        documents=(
            _document("MADDE 1\nİlk içerik.", etag="e1", last_modified="l1"),
            _not_modified(etag="e2", last_modified="l2"),
            _document("MADDE 1\nİlk içerik.", etag="e3", last_modified="l3"),
        )
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        first = await service.get_document("mevzuat:law")
        assert first["markdown"] == "MADDE 1\nİlk içerik."
        assert len(provider.document_headers) == 1

        clock.now = T0 + timedelta(hours=23, minutes=59)
        fresh = await service.get_document("mevzuat:law")
        assert fresh["markdown"] == first["markdown"]
        assert len(provider.document_headers) == 1

        clock.now = T0 + timedelta(hours=24)
        await service.get_document("mevzuat:law")
        after_304 = cache.get("bedesten_legislation_document", "law", now=clock.now)
        assert after_304 is not None
        assert after_304.fetched_at == T0
        assert after_304.validated_at == clock.now
        assert after_304.expires_at == T0 + timedelta(hours=48)
        assert after_304.etag == "e2"
        assert provider.document_headers[-1] == {
            "If-None-Match": "e1",
            "If-Modified-Since": "l1",
        }

        clock.now = T0 + timedelta(hours=48)
        await service.get_document("mevzuat:law")
        after_hash_match = cache.get(
            "bedesten_legislation_document", "law", now=clock.now
        )
        assert after_hash_match is not None
        assert after_hash_match.content == "MADDE 1\nİlk içerik."
        assert after_hash_match.fetched_at == T0
        assert after_hash_match.validated_at == clock.now
        assert after_hash_match.etag == "e3"
        assert provider.document_headers[-1] == {
            "If-None-Match": "e2",
            "If-Modified-Since": "l2",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", (None, 503))
async def test_expired_document_uses_stale_cache_only_for_network_or_5xx_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status_code: int | None
) -> None:
    _install_text_converter(monkeypatch)
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    provider = _ScriptedProvider(
        documents=(
            _document("MADDE 1\nÖnbellekte kalan metin."),
            UpstreamUnavailable("Bedesten erişilemedi", status_code=status_code),
        )
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        await service.get_document("mevzuat:law")
        clock.now = T0 + timedelta(hours=24)
        stale = await service.get_document("mevzuat:law")

    assert stale["ok"] is True
    assert stale["markdown"] == "MADDE 1\nÖnbellekte kalan metin."
    assert stale["warnings"] == [
        {
            "code": "stale_content",
            "message": "Önbelleğe alınan içerik, üst kaynak yeniden doğrulaması kullanılamadığı için güncel değildir.",
        }
    ]
    assert stale["revalidation_error"] == {
        "code": "upstream_unavailable",
        "message": "Üst kaynak şu anda kullanılamıyor. Ayrıntı: Bedesten erişilemedi",
    }


@pytest.mark.asyncio
async def test_404_never_returns_stale_document_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_text_converter(monkeypatch)
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    provider = _ScriptedProvider(
        documents=(
            _document("MADDE 1\nEski metin."),
            UpstreamNotFound("mevzuat bulunamadı", status_code=404),
        )
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        await service.get_document("mevzuat:law")
        clock.now = T0 + timedelta(hours=24)
        result = await service.get_document("mevzuat:law")

    assert result == {
        "ok": False,
        "error": {
            "code": "not_found",
            "message": "İstenen belge üst kaynakta bulunamadı. Ayrıntı: mevzuat bulunamadı",
            "retryable": False,
            "details": {"upstream": "bedesten"},
        },
    }


@pytest.mark.asyncio
async def test_forced_document_refresh_bypasses_a_fresh_cache_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_text_converter(monkeypatch)
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    provider = _ScriptedProvider(
        documents=(
            _document("MADDE 1\nEski metin.", etag="old", last_modified="old-lm"),
            _document("MADDE 1\nYeni metin.", etag="new", last_modified="new-lm"),
        )
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        await service.get_document("mevzuat:law")
        refreshed = await service.get_document("mevzuat:law", refresh=True)

    assert refreshed["markdown"] == "MADDE 1\nYeni metin."
    assert provider.document_headers == [
        {},
        {"If-None-Match": "old", "If-Modified-Since": "old-lm"},
    ]


@pytest.mark.asyncio
async def test_outline_and_madde_retrieval_use_local_tree_ids_and_article_paging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_text_converter(monkeypatch)
    article_markdown = "MADDE 2 - İkinci Madde\n" + "a" * 12_100 + "\n\n" + "b" * 12_100
    provider = _ScriptedProvider(
        outlines=(
            (
                [
                    {
                        "baslik": "BİRİNCİ BÖLÜM",
                        "altMaddeler": [
                            {
                                "maddeNo": "2",
                                "maddeId": "madde-2",
                                "maddeBaslik": "İkinci Madde",
                            }
                        ],
                    }
                ],
                "outline-v1",
                "outline-time",
            ),
        ),
        articles=(_document(article_markdown, etag="article-v1"),),
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        outline = await service.get_outline("mevzuat:law")
        article = await service.get_article("mevzuat:law", "madde 2", page=2)

    assert outline["nodes"] == [
        {
            "article_number": None,
            "title": "BİRİNCİ BÖLÜM",
            "children": [
                {
                    "article_number": "2",
                    "title": "İkinci Madde",
                    "children": [],
                }
            ],
        }
    ]
    assert provider.article_calls == [("madde-2", "law", {})]
    assert article["article_number"] == "2"
    assert article["page"] == {
        "page": 2,
        "page_size": 12_100,
        "total_records": 1,
        "total_pages": 2,
        "has_more": False,
    }
    assert article["markdown"] == "b" * 12_100


@pytest.mark.asyncio
async def test_missing_article_uses_the_turkish_public_not_found_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_text_converter(monkeypatch)
    provider = _ScriptedProvider(
        outlines=(([], "outline-v1", "outline-time"),),
        documents=(_document("MADDE 1\nBirinci madde."),),
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        result = await legislation_service.LegislationService(
            provider, cache
        ).get_article("mevzuat:law", "2")

    assert result == {
        "ok": False,
        "error": {
            "code": "not_found",
            "message": (
                "İstenen belge üst kaynakta bulunamadı. Ayrıntı: İstenen madde "
                "bulunamadı."
            ),
            "retryable": False,
            "details": {"upstream": "bedesten"},
        },
    }
    assert provider.article_calls == []


@pytest.mark.asyncio
async def test_within_legislation_paginates_matching_article_sections_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_text_converter(monkeypatch)
    markdown = """MADDE 1 - Birinci
İdari işlem hakkında karar verilir.

MADDE 2 - İkinci
İdari işlem hakkında karar verilir.

MADDE 3 - Üçüncü
İdari işlem hakkında karar verilir.
"""
    provider = _ScriptedProvider(documents=(_document(markdown),))

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        result = await service.search_in_legislation(
            "mevzuat:law", '"idari işlem" karar', page=2, page_size=2
        )

    assert result["legislation_id"] == "mevzuat:law"
    assert result["page"] == {
        "page": 2,
        "page_size": 2,
        "total_records": 3,
        "total_pages": 2,
        "has_more": False,
    }
    assert result["hits"] == [
        {
            "article_number": "3",
            "title": "Üçüncü",
            "snippet": "MADDE 3 - Üçüncü İdari işlem hakkında karar verilir.",
            "match_count": 2,
        }
    ]
    assert len(provider.document_headers) == 1


@pytest.mark.asyncio
async def test_within_legislation_maps_maximum_length_invalid_boolean_to_invalid_params(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider()

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        result = await service.search_in_legislation("mevzuat:law", "a OR " * 100)

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_params"
    assert "query" in result["error"]["details"]["field_errors"]
    assert provider.document_headers == []


@pytest.mark.asyncio
async def test_opposite_document_refresh_policies_do_not_share_stale_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_text_converter(monkeypatch)
    clock = _Clock(T0)
    monkeypatch.setattr(legislation_service, "utc_now", clock)
    provider = _ScriptedProvider(
        documents=(
            _document("MADDE 1\nÖnbellekte kalan metin."),
            UpstreamUnavailable("normal yeniden doğrulama başarısız"),
            UpstreamUnavailable("zorunlu yenileme başarısız"),
        )
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        await service.get_document("mevzuat:law")
        clock.now = T0 + timedelta(hours=24)
        await provider.document_entered.get()
        provider.block_document = True
        stale_eligible = asyncio.create_task(service.get_document("mevzuat:law"))
        await provider.document_entered.get()
        forced_refresh = asyncio.create_task(
            service.get_document("mevzuat:law", refresh=True)
        )
        await provider.document_entered.get()
        assert len(provider.document_headers) == 3
        provider.release_document.set()
        stale, refreshed = await asyncio.gather(stale_eligible, forced_refresh)

    assert stale["ok"] is True
    assert stale["warnings"][0]["code"] == "stale_content"
    assert refreshed["ok"] is False
    assert refreshed["error"]["code"] == "upstream_unavailable"
    assert "warnings" not in refreshed


@pytest.mark.asyncio
async def test_legislation_service_ordinary_outputs_match_tool_schemas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_text_converter(monkeypatch)
    markdown = "MADDE 1\nİdari işlem hakkında karar verilir."
    provider = _ScriptedProvider(
        searches=(_search_page(),),
        documents=(_document(markdown),),
        outlines=(
            (
                [{"baslik": "BİRİNCİ BÖLÜM", "altMaddeler": []}],
                "outline-v1",
                "outline-time",
            ),
        ),
    )

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)  # type: ignore[arg-type]
        search = await service.search(query="fixture")
        document = await service.get_document("mevzuat:law")
        article = await service.get_article("mevzuat:law", "1")
        within = await service.search_in_legislation(
            "mevzuat:law", "idari", include_snippet=False
        )
        outline = await service.get_outline("mevzuat:law")

    assert search["hits"][0]["official_gazette_date"] is None
    assert search["hits"][0]["official_gazette_issue"] is None
    assert search["hits"][0]["snippet"] is None
    assert article["title"] is None
    assert within["hits"][0]["title"] is None
    assert within["hits"][0]["snippet"] is None
    assert outline["nodes"][0]["article_number"] is None
    for tool_name, response in (
        ("mevzuat_ara", search),
        ("belge_getir", document),
        ("mevzuat_madde_getir", article),
        ("mevzuat_icinde_ara", within),
        ("mevzuat_madde_agaci_getir", outline),
    ):
        Draft202012Validator(standalone_tool_schema(tool_name, "output")).validate(
            response
        )


@pytest.mark.asyncio
async def test_placeholder_outline_is_rebuilt_from_document_with_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_text_converter(monkeypatch)
    markdown = "# BİRİNCİ KISIM\n# Genel Hükümler\n# BİRİNCİ BÖLÜM\n# Kaynaklar\nA. Tanımı\nMADDE 1- Ana metin.\n\n6098 SAYILI KANUNA İŞLENEMEYEN HÜKÜMLER\nMADDE 1- Ek metin."
    provider = _ScriptedProvider(
        outlines=(([{"maddeNo": "1", "maddeBaslik": "Madde No: 1"}], None, None),),
        documents=(_document(markdown),),
    )
    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = legislation_service.LegislationService(provider, cache)
        outline = await service.get_outline("mevzuat:law")
        within = await service.search_in_legislation("mevzuat:law", "Ana")
    assert outline["ok"] is True
    assert outline["warnings"][0]["code"] == "outline_from_document"
    assert outline["nodes"][0]["title"] == "BİRİNCİ KISIM — Genel Hükümler"
    assert within["page"]["total_records"] == 1
    assert within["hits"][0]["article_number"] == "1"
    Draft202012Validator(
        standalone_tool_schema("mevzuat_madde_agaci_getir", "output")
    ).validate(outline)


@pytest.mark.asyncio
async def test_article_fallback_rejects_ambiguous_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_text_converter(monkeypatch)
    provider = _ScriptedProvider(
        outlines=(([], None, None),),
        documents=(_document("MADDE 1- Bir metin.\n\nMADDE 1- Farklı metin."),),
    )
    with CacheStore(tmp_path / "cache.sqlite") as cache:
        result = await legislation_service.LegislationService(
            provider, cache
        ).get_article("mevzuat:law", "1")
    assert result["ok"] is False
    assert "tek madde güvenle" in result["error"]["message"]
