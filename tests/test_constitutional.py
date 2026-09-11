"""Isolated behavioral coverage for Constitutional Court provider and service flows."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from mutalaamcp.cache.store import CacheStore, Freshness
from mutalaamcp.domain.ids import ANAYASA_SEQUENCE_MAX_DIGITS, AnayasaId
from mutalaamcp.domain.models import ConstitutionalKind
from mutalaamcp.net import SafeHttpClient, UpstreamUnavailable
from mutalaamcp.providers.anayasa.client import (
    AYM_ALLOWED_HOSTS,
    INDIVIDUAL_ORIGIN,
    NORM_ORIGIN,
    SEARCH_PATH,
    AnayasaClient,
    AnayasaNotFound,
    AnayasaProtocolError,
    AnayasaRecord,
    AnayasaSearchPage,
)
from mutalaamcp.services.constitutional import (
    _DOCUMENT_CACHE_NAMESPACE,
    ConstitutionalDocumentRequest,
    ConstitutionalService,
)
from mutalaamcp.tool_schemas import standalone_tool_schema

FIXTURE_YEAR = 2042
FIXTURE_SEQUENCE = 17


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _document_id(kind: str = "nd") -> AnayasaId:
    return AnayasaId(kind=kind, year=FIXTURE_YEAR, sequence=FIXTURE_SEQUENCE)


def _provider_item(
    kind: ConstitutionalKind,
    *,
    provider_id: str = "fixture-token",
    html_content: str | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "id": provider_id,
        "basvuruAdi": "Synthetic fixture",
        "kararKonusu": "Synthetic provider response.",
        "kararTarihi": "2042-01-02",
    }
    if kind is ConstitutionalKind.NORM_REVIEW:
        item.update(
            {
                "path": f"/ND/{FIXTURE_YEAR}/{FIXTURE_SEQUENCE}",
                "esasNo": f"{FIXTURE_YEAR}/{FIXTURE_SEQUENCE}",
                "kararNo": f"{FIXTURE_YEAR}/18",
            }
        )
    else:
        item.update(
            {
                "path": f"/BB/{FIXTURE_YEAR}/{FIXTURE_SEQUENCE}",
                "basvuruNo": f"{FIXTURE_YEAR}/{FIXTURE_SEQUENCE}",
            }
        )
    if html_content is not None:
        item["icerik"] = html_content
    return item


def _record(document_id: AnayasaId, *, edition: int = 1) -> AnayasaRecord:
    kind = (
        ConstitutionalKind.NORM_REVIEW
        if document_id.kind == "nd"
        else ConstitutionalKind.INDIVIDUAL_APPLICATION
    )
    return AnayasaRecord(
        document_id=document_id,
        kind=kind,
        provider_id=f"fixture-{edition}",
        source_url=f"https://example.test/constitutional/{document_id.kind}/{edition}",
        title="Synthetic fixture",
        summary="Synthetic provider response.",
        esas_no=f"{document_id.year}/{document_id.sequence}"
        if kind is ConstitutionalKind.NORM_REVIEW
        else None,
        karar_no=f"{document_id.year}/18"
        if kind is ConstitutionalKind.NORM_REVIEW
        else None,
        application_number=f"{document_id.year}/{document_id.sequence}"
        if kind is ConstitutionalKind.INDIVIDUAL_APPLICATION
        else None,
        decision_date=None,
        snippet=None,
        html_content=f"<h1>Edition {edition}</h1><p>Fixture body.</p>",
    )


class FakeAnayasa:
    """In-memory provider with controllable calls and upstream failures."""

    def __init__(self) -> None:
        self.search_calls: list[tuple[ConstitutionalKind, str | None, int, int]] = []
        self.document_calls: list[AnayasaId] = []
        self.search_error: Exception | None = None
        self.document_error: Exception | None = None
        self.search_entered: asyncio.Event | None = None
        self.search_release: asyncio.Event | None = None
        self.search_records: tuple[AnayasaRecord, ...] = ()

    async def search(
        self,
        *,
        kind: ConstitutionalKind,
        query: str | None,
        page: int,
        size: int,
    ) -> AnayasaSearchPage:
        self.search_calls.append((kind, query, page, size))
        if self.search_entered is not None:
            self.search_entered.set()
        if self.search_release is not None:
            await self.search_release.wait()
        if self.search_error is not None:
            raise self.search_error
        return AnayasaSearchPage(
            records=self.search_records,
            total_records=len(self.search_records),
            page=page,
            page_size=size,
        )

    async def get_document(self, document_id: AnayasaId) -> AnayasaRecord:
        self.document_calls.append(document_id)
        if self.document_error is not None:
            raise self.document_error
        return _record(document_id, edition=len(self.document_calls))


class SearchTransport:
    """MockTransport handler recording exact AYM SPA search requests."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, object]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert isinstance(body, dict)
        self.requests.append((request.method, str(request.url), body))
        kind = (
            ConstitutionalKind.NORM_REVIEW
            if body["kararTipi"] == "NormDenetimi"
            else ConstitutionalKind.INDIVIDUAL_APPLICATION
        )
        return httpx.Response(
            200,
            json={
                "data": [_provider_item(kind)],
                "total": 1,
                "page": body["page"],
                "page_size": body["size"],
            },
        )


class DocumentTransport:
    """MockTransport handler for ID-based AYM document resolution and detail."""

    def __init__(self, kind: ConstitutionalKind) -> None:
        self.kind = kind
        self.requests: list[tuple[str, str, dict[str, object]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert isinstance(body, dict)
        self.requests.append((request.method, str(request.url), body))
        if "id" not in body:
            return httpx.Response(
                200,
                json={
                    "data": [_provider_item(self.kind)],
                    "total": 1,
                    "page": body["page"],
                    "page_size": body["size"],
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    _provider_item(
                        self.kind,
                        html_content="<h1>Fixture</h1><p>Only synthetic content.</p>",
                    )
                ]
            },
        )


def _safe_client(client: httpx.AsyncClient) -> AnayasaClient:
    return AnayasaClient(SafeHttpClient(client, AYM_ALLOWED_HOSTS, timeout=1.0))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "origin", "karar_tipi", "canonical_id"),
    [
        (
            ConstitutionalKind.NORM_REVIEW,
            NORM_ORIGIN,
            "NormDenetimi",
            "anayasa:nd:2042:17",
        ),
        (
            ConstitutionalKind.INDIVIDUAL_APPLICATION,
            INDIVIDUAL_ORIGIN,
            "BireyselBasvuru",
            "anayasa:bb:2042:17",
        ),
    ],
)
async def test_search_uses_exact_aym_host_path_body_and_kind_mapping(
    kind: ConstitutionalKind,
    origin: str,
    karar_tipi: str,
    canonical_id: str,
) -> None:
    transport = SearchTransport()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as http:
        page = await _safe_client(http).search(
            kind=kind, query="synthetic query", page=2, size=7
        )

    assert transport.requests == [
        (
            "POST",
            origin + SEARCH_PATH,
            {
                "kararTipi": karar_tipi,
                "page": 2,
                "size": 7,
                "query": "synthetic query",
            },
        )
    ]
    assert page.records[0].kind is kind
    assert page.records[0].document_id.format() == canonical_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sequence", "rejects"),
    [
        ("9" * ANAYASA_SEQUENCE_MAX_DIGITS, False),
        ("9" * (ANAYASA_SEQUENCE_MAX_DIGITS + 1), True),
    ],
)
async def test_constitutional_search_enforces_sequence_digit_limit(
    sequence: str, rejects: bool
) -> None:
    item = _provider_item(ConstitutionalKind.NORM_REVIEW)
    item["path"] = f"/ND/{FIXTURE_YEAR}/{sequence}"
    item["esasNo"] = f"{FIXTURE_YEAR}/{sequence}"

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [item],
                "total": 1,
                "page": 1,
                "page_size": 10,
            },
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as http:
        client = _safe_client(http)
        if rejects:
            with pytest.raises(AnayasaProtocolError):
                await client.search(
                    kind=ConstitutionalKind.NORM_REVIEW,
                    query="fixture",
                    page=1,
                    size=10,
                )
        else:
            page = await client.search(
                kind=ConstitutionalKind.NORM_REVIEW,
                query="fixture",
                page=1,
                size=10,
            )

    if not rejects:
        assert page.records[0].document_id.sequence == int(sequence)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "rejects"),
    [
        ({"data": []}, True),
        ({"data": [], "total": False}, True),
        ({"data": [], "total": 0}, False),
    ],
)
async def test_constitutional_search_requires_valid_total_for_empty_results(
    payload: dict[str, object], rejects: bool
) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as http:
        client = _safe_client(http)
        if rejects:
            with pytest.raises(
                AnayasaProtocolError,
                match="^arama yanıtı total negatif olmayan bir tam sayı olmalıdır$",
            ):
                await client.search(
                    kind=ConstitutionalKind.NORM_REVIEW,
                    query="fixture",
                    page=1,
                    size=10,
                )
        else:
            page = await client.search(
                kind=ConstitutionalKind.NORM_REVIEW,
                query="fixture",
                page=1,
                size=10,
            )

    if not rejects:
        assert page.records == ()
        assert page.total_records == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_kind", "expected_query"),
    [
        (
            {"kind": "norm_denetimi", "esas_no": "2042/11"},
            ConstitutionalKind.NORM_REVIEW,
            "2042/11",
        ),
        (
            {"kind": "norm_denetimi", "karar_no": "2042/12"},
            ConstitutionalKind.NORM_REVIEW,
            "2042/12",
        ),
        (
            {
                "kind": "norm_denetimi",
                "query": "synthetic query",
                "esas_no": "2042/11",
                "karar_no": "2042/12",
            },
            ConstitutionalKind.NORM_REVIEW,
            "synthetic query 2042/11 2042/12",
        ),
        (
            {
                "kind": "bireysel_basvuru",
                "query": "synthetic query",
                "application_number": "2042/13",
            },
            ConstitutionalKind.INDIVIDUAL_APPLICATION,
            "synthetic query 2042/13",
        ),
    ],
)
async def test_identifier_inputs_compile_deterministically_and_warn(
    tmp_path: Path,
    arguments: dict[str, str],
    expected_kind: ConstitutionalKind,
    expected_query: str,
) -> None:
    provider = FakeAnayasa()
    with CacheStore(tmp_path / "cache.sqlite") as cache:
        result = await ConstitutionalService(provider, cache).search(**arguments)

    assert provider.search_calls == [(expected_kind, expected_query, 1, 10)]
    assert result["ok"] is True
    assert result["warnings"] == [
        {
            "code": "query_backed_identifier_filter",
            "message": (
                "Kimlik filtrelemesi geçerli AYM SPA sorgusuna derlenir ve tam "
                "eşleşmeli bir üst kaynak kimlik filtresi değildir."
            ),
        }
    ]


@pytest.mark.asyncio
async def test_constitutional_service_returns_turkish_blank_query_validation_error(
    tmp_path: Path,
) -> None:
    provider = FakeAnayasa()

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        result = await ConstitutionalService(provider, cache).search(
            kind="norm_denetimi",
            query="   ",
        )

    assert result == {
        "ok": False,
        "error": {
            "code": "invalid_params",
            "message": "İstek parametreleri geçersiz.",
            "retryable": False,
            "details": {"field_errors": {"query": ["boş olamaz"]}},
        },
    }
    assert provider.search_calls == []


@pytest.mark.parametrize(
    "value",
    ["anayasa:nd:2042:17", "anayasa:bb:2042:17"],
)
def test_document_request_normalizes_canonical_nd_and_bb_ids(value: str) -> None:
    request = ConstitutionalDocumentRequest.model_validate({"id": value})

    assert request.id == value


@pytest.mark.parametrize(
    "value",
    [
        "https://normkararlarbilgibankasi.anayasa.gov.tr/ND/2042/17",
        "/ND/2042/17",
        "anayasa:ND:2042:17",
        "anayasa:nd:2042:017",
        "anayasa:bb:2042:0",
        "anayasa:nd:2042:17?raw=true",
        "anayasa:nd:2042:" + "9" * 5_000,
    ],
)
def test_document_request_rejects_raw_or_noncanonical_ids(value: str) -> None:
    with pytest.raises(ValidationError):
        ConstitutionalDocumentRequest.model_validate({"id": value})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "origin", "karar_tipi", "identifier_field", "document_id", "source_url"),
    [
        (
            ConstitutionalKind.NORM_REVIEW,
            NORM_ORIGIN,
            "NormDenetimi",
            "esasNo",
            "anayasa:nd:2042:17",
            (
                "https://normkararlarbilgibankasi.anayasa.gov.tr/kbb/pages/search/"
                "NormDenetimi?id=a2JiOmZpeHR1cmUtdG9rZW4&type=NormDenetimi"
            ),
        ),
        (
            ConstitutionalKind.INDIVIDUAL_APPLICATION,
            INDIVIDUAL_ORIGIN,
            "BireyselBasvuru",
            "basvuruNo",
            "anayasa:bb:2042:17",
            (
                "https://kararlarbilgibankasi.anayasa.gov.tr/kbb/pages/search/"
                "BireyselBasvuru?id=a2JiOmZpeHR1cmUtdG9rZW4&type=BireyselBasvuru"
            ),
        ),
    ],
)
async def test_document_retrieval_resolves_nd_and_bb_through_real_spa_detail_flow(
    tmp_path: Path,
    kind: ConstitutionalKind,
    origin: str,
    karar_tipi: str,
    identifier_field: str,
    document_id: str,
    source_url: str,
) -> None:
    transport = DocumentTransport(kind)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as http:
        with CacheStore(tmp_path / "cache.sqlite") as cache:
            service = ConstitutionalService(_safe_client(http), cache)
            rejected = await service.get_document(
                id=(
                    f"{origin}/{document_id.split(':')[1].upper()}/"
                    f"{FIXTURE_YEAR}/{FIXTURE_SEQUENCE}"
                )
            )
            assert rejected["error"]["code"] == "invalid_params"
            assert transport.requests == []

            result = await service.get_document(id=document_id)

    assert result["ok"] is True
    assert result["id"] == document_id
    assert result["source_url"] == source_url
    assert "Only synthetic content." in result["markdown"]
    assert transport.requests == [
        (
            "POST",
            origin + SEARCH_PATH,
            {
                "kararTipi": karar_tipi,
                "page": 1,
                "size": 10,
                identifier_field: "2042/17",
            },
        ),
        (
            "POST",
            origin + SEARCH_PATH,
            {
                "kararTipi": karar_tipi,
                "id": "fixture-token",
                "page": 1,
                "size": 1,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_document_resolution_has_a_fixed_page_budget_despite_upstream_total() -> (
    None
):
    requests: list[dict[str, object]] = []
    nonmatching = _provider_item(ConstitutionalKind.NORM_REVIEW)
    nonmatching["path"] = f"/ND/{FIXTURE_YEAR}/{FIXTURE_SEQUENCE + 1}"
    nonmatching["esasNo"] = f"{FIXTURE_YEAR}/{FIXTURE_SEQUENCE + 1}"

    def transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert isinstance(body, dict)
        assert "id" not in body
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "data": [nonmatching] * 10,
                "total": 1_000_000,
                "page": body["page"],
                "page_size": body["size"],
            },
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), timeout=1.0
    ) as http:
        with pytest.raises(AnayasaNotFound):
            await _safe_client(http).get_document(_document_id())

    assert [body["page"] for body in requests] == [1, 2, 3]
    assert all(
        body["esasNo"] == f"{FIXTURE_YEAR}/{FIXTURE_SEQUENCE}" for body in requests
    )


@pytest.mark.asyncio
async def test_search_cache_expires_at_24_hours_and_isolates_request_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock(datetime(2042, 1, 2, tzinfo=UTC))
    monkeypatch.setattr("mutalaamcp.services.constitutional.utc_now", clock)
    provider = FakeAnayasa()
    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = ConstitutionalService(provider, cache)
        first = await service.search(kind="norm_denetimi", query="synthetic query")
        duplicate = await service.search(kind="norm_denetimi", query="synthetic query")

        clock.now += timedelta(hours=24) - timedelta(microseconds=1)
        before_expiry = await service.search(
            kind="norm_denetimi", query="synthetic query"
        )

        clock.now += timedelta(microseconds=1)
        at_expiry = await service.search(kind="norm_denetimi", query="synthetic query")

        isolated = [
            await service.search(kind="bireysel_basvuru", query="synthetic query"),
            await service.search(kind="norm_denetimi", query="another query"),
            await service.search(kind="norm_denetimi", query="synthetic query", page=2),
            await service.search(
                kind="norm_denetimi", query="synthetic query", page_size=9
            ),
        ]

    assert first["ok"] is True
    assert duplicate["ok"] is True
    assert before_expiry["ok"] is True
    assert at_expiry["ok"] is True
    assert all(result["ok"] is True for result in isolated)
    assert provider.search_calls == [
        (ConstitutionalKind.NORM_REVIEW, "synthetic query", 1, 10),
        (ConstitutionalKind.NORM_REVIEW, "synthetic query", 1, 10),
        (ConstitutionalKind.INDIVIDUAL_APPLICATION, "synthetic query", 1, 10),
        (ConstitutionalKind.NORM_REVIEW, "another query", 1, 10),
        (ConstitutionalKind.NORM_REVIEW, "synthetic query", 2, 10),
        (ConstitutionalKind.NORM_REVIEW, "synthetic query", 1, 9),
    ]


@pytest.mark.asyncio
async def test_document_cache_never_expires_and_refresh_replaces_cached_document(
    tmp_path: Path,
) -> None:
    provider = FakeAnayasa()
    document_id = "anayasa:nd:2042:17"
    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = ConstitutionalService(provider, cache)
        first = await service.get_document(id=document_id)
        cached = await service.get_document(id=document_id)
        refreshed = await service.get_document(id=document_id, refresh=True)
        reread = await service.get_document(id=document_id)
        record = cache.get(_DOCUMENT_CACHE_NAMESPACE, document_id)
        assert record is not None
        assert record.expires_at is None
        assert (
            cache.get(
                _DOCUMENT_CACHE_NAMESPACE,
                document_id,
                now=record.fetched_at + timedelta(days=3650),
            ).freshness
            is Freshness.FRESH
        )

    assert provider.document_calls == [_document_id(), _document_id()]
    assert "Edition 1" in first["markdown"]
    assert cached["markdown"] == first["markdown"]
    assert "Edition 2" in refreshed["markdown"]
    assert reread["markdown"] == refreshed["markdown"]


@pytest.mark.asyncio
async def test_concurrent_identical_searches_share_one_upstream_request(
    tmp_path: Path,
) -> None:
    provider = FakeAnayasa()
    provider.search_entered = asyncio.Event()
    provider.search_release = asyncio.Event()
    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = ConstitutionalService(provider, cache)
        first = asyncio.create_task(
            service.search(kind="norm_denetimi", query="synthetic query")
        )
        await provider.search_entered.wait()
        second = asyncio.create_task(
            service.search(kind="norm_denetimi", query="synthetic query")
        )
        await asyncio.sleep(0)
        provider.search_release.set()
        first_result, second_result = await asyncio.gather(first, second)

    assert provider.search_calls == [
        (ConstitutionalKind.NORM_REVIEW, "synthetic query", 1, 10)
    ]
    assert first_result["ok"] is True
    assert second_result["ok"] is True


@pytest.mark.asyncio
async def test_document_page_above_cached_chunk_count_is_not_clamped(
    tmp_path: Path,
) -> None:
    provider = FakeAnayasa()
    with CacheStore(tmp_path / "cache.sqlite") as cache:
        result = await ConstitutionalService(provider, cache).get_document(
            id="anayasa:nd:2042:17", page=2
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "chunk_out_of_range"
    assert provider.document_calls == [_document_id()]


@pytest.mark.asyncio
async def test_failed_document_refresh_never_serves_a_stale_cached_document(
    tmp_path: Path,
) -> None:
    provider = FakeAnayasa()
    document_id = "anayasa:nd:2042:17"
    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = ConstitutionalService(provider, cache)
        cached = await service.get_document(id=document_id)
        provider.document_error = UpstreamUnavailable("synthetic upstream outage")
        refreshed = await service.get_document(id=document_id, refresh=True)

    assert cached["ok"] is True
    assert refreshed["ok"] is False
    assert refreshed["error"]["code"] == "upstream_unavailable"
    assert "markdown" not in refreshed
    assert provider.document_calls == [_document_id(), _document_id()]


@pytest.mark.asyncio
async def test_constitutional_service_ordinary_outputs_match_tool_schemas(
    tmp_path: Path,
) -> None:
    provider = FakeAnayasa()
    provider.search_records = (_record(_document_id()),)

    with CacheStore(tmp_path / "cache.sqlite") as cache:
        service = ConstitutionalService(provider, cache)
        search = await service.search(kind="norm_denetimi", query="synthetic")
        document = await service.get_document(id="anayasa:nd:2042:17")

    assert search["hits"] == [
        {
            "id": "anayasa:nd:2042:17",
            "source_url": "https://example.test/constitutional/nd/1",
            "title": "Synthetic fixture",
            "snippet": None,
            "source": "anayasa",
            "kind": "norm_denetimi",
            "esas_no": "2042/17",
            "karar_no": "2042/18",
            "application_number": None,
            "date": None,
            "summary": "Synthetic provider response.",
        }
    ]
    Draft202012Validator(
        standalone_tool_schema("anayasa_karari_ara", "output")
    ).validate(search)
    Draft202012Validator(standalone_tool_schema("belge_getir", "output")).validate(
        document
    )
