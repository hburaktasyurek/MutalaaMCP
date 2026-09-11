"""In-memory FastMCP V1 tool tests using injectable runtime fakes."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pytest
from fastmcp import Client

from mutalaamcp import tools
from mutalaamcp.auth import ActivationError, AuthenticationRequired, SessionExpired
from mutalaamcp.domain.errors import ErrorCode
from mutalaamcp.server import V1_TOOL_NAMES, create_server
from mutalaamcp.settings import Settings

_CONTRACT_PATH = Path(__file__).parents[1] / "contracts" / "tool-surface-v1.json"

_CONTRACT = json.loads(_CONTRACT_PATH.read_text())
_TOOL_CONTRACTS = {tool["name"]: tool for tool in _CONTRACT["tools"]}
_SCHEMA_DEFINITIONS = _CONTRACT["$defs"]
_SOURCE_URL = "https://source.invalid/record"
_TERMS_URL = "https://terms.invalid/accept"
_CONTENT_HASH = "sha256:" + "0" * 64

_EXPECTED_PUBLIC_TOOL_NAMES = frozenset(
    {
        "turk_hukuku_sorularinda_once_bu_araci_cagir",
        "karar_ara",
        "mevzuat_ara",
        "anayasa_karari_ara",
        "belge_getir",
        "mevzuat_madde_getir",
        "mevzuat_icinde_ara",
        "mevzuat_madde_agaci_getir",
    }
)
_LEGACY_PUBLIC_TOOL_NAMES = frozenset(
    {
        "search_decisions",
        "search_legislation",
        "search_constitutional",
        "get_document",
        "get_legislation_article",
        "search_in_legislation",
        "get_legislation_outline",
    }
)
_SUCCESS_TYPES = {
    "turk_hukuku_sorularinda_once_bu_araci_cagir": "ResearchStartSuccess",
    "karar_ara": "DecisionSearchSuccess",
    "mevzuat_ara": "LegislationSearchSuccess",
    "anayasa_karari_ara": "ConstitutionalSearchSuccess",
    "belge_getir": "DocumentSuccess",
    "mevzuat_madde_getir": "ArticleSuccess",
    "mevzuat_icinde_ara": "WithinSearchSuccess",
    "mevzuat_madde_agaci_getir": "OutlineSuccess",
}

_TOOL_CALLS = {
    "turk_hukuku_sorularinda_once_bu_araci_cagir": {"soru": "Aidatı kim öder?"},
    "karar_ara": {"courts": ["yargitay"], "query": "temerrüt"},
    "mevzuat_ara": {"query": "borç"},
    "anayasa_karari_ara": {"kind": "norm_denetimi", "query": "iptal"},
    "belge_getir": {"id": "bedesten:decision-42"},
    "mevzuat_madde_getir": {
        "legislation_id": "mevzuat:law-42",
        "article_number": "25/A",
    },
    "mevzuat_icinde_ara": {
        "legislation_id": "mevzuat:law-42",
        "query": "borç AND temerrüt",
    },
    "mevzuat_madde_agaci_getir": {"legislation_id": "mevzuat:law-42"},
}


_UNAUTHENTICATED_INVALID_RAW_CALLS: tuple[tuple[str, dict[str, object]], ...] = (
    ("belge_getir", {"id": "not-a-document-id"}),
    ("karar_ara", {}),
    ("anayasa_karari_ara", {"kind": "not-a-kind"}),
    (
        "karar_ara",
        {"courts": ["yargitay"], "date_from": "2026-99-99"},
    ),
    ("belge_getir", {"id": 42}),
)

_AUTHORIZED_INVALID_RAW_CALLS = (
    *_UNAUTHENTICATED_INVALID_RAW_CALLS,
    ("mevzuat_ara", {"query": "borç", "types": []}),
)


def _page(page: int, page_size: int) -> dict[str, object]:
    return {
        "page": page,
        "page_size": page_size,
        "total_records": 0,
        "total_pages": 0,
        "has_more": False,
    }


def _search_success(*, page: int, page_size: int) -> dict[str, object]:
    return {"ok": True, "hits": [], "page": _page(page, page_size), "warnings": []}


def _document_success(
    document_id: str, *, page: int, refresh: bool
) -> dict[str, object]:
    del refresh
    source = "anayasa" if document_id.startswith("anayasa:") else "bedesten"
    return {
        "ok": True,
        "id": document_id,
        "source": source,
        "source_url": _SOURCE_URL,
        "markdown": "# Fixture document",
        "mime_type": "text/html",
        "conversion": "html_markdown",
        "content_hash": _CONTENT_HASH,
        "page": _page(page, 1),
        "warnings": [],
        "fetched_at": "2026-09-03T00:00:00+00:00",
        "validated_at": "2026-09-03T00:00:00+00:00",
        "expires_at": (
            "2026-09-04T00:00:00+00:00" if document_id.startswith("mevzuat:") else None
        ),
    }


class FakeAuth:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def ensure_authorized(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


class FakeDecisions:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def search(self, **arguments: object) -> dict[str, object]:
        self.calls.append(("search", dict(arguments)))
        return _search_success(
            page=int(arguments["page"]), page_size=int(arguments["page_size"])
        )

    async def get_document(
        self, document_id: str, *, page: int, refresh: bool
    ) -> dict[str, object]:
        self.calls.append(
            ("get_document", {"id": document_id, "page": page, "refresh": refresh})
        )
        return _document_success(document_id, page=page, refresh=refresh)


class FakeLegislation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def search(self, **arguments: object) -> dict[str, object]:
        self.calls.append(("search", dict(arguments)))
        return _search_success(
            page=int(arguments["page"]), page_size=int(arguments["page_size"])
        )

    async def get_document(
        self, legislation_id: str, *, page: int, refresh: bool
    ) -> dict[str, object]:
        self.calls.append(
            (
                "get_document",
                {"legislation_id": legislation_id, "page": page, "refresh": refresh},
            )
        )
        return _document_success(legislation_id, page=page, refresh=refresh)

    async def get_article(
        self,
        legislation_id: str,
        article_number: str,
        *,
        page: int,
        refresh: bool,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "get_article",
                {
                    "legislation_id": legislation_id,
                    "article_number": article_number,
                    "page": page,
                    "refresh": refresh,
                },
            )
        )
        return {
            "ok": True,
            "legislation_id": legislation_id,
            "article_number": article_number,
            "title": None,
            "source": "bedesten",
            "source_url": _SOURCE_URL,
            "markdown": "# Fixture article",
            "mime_type": "text/html",
            "conversion": "html_markdown",
            "content_hash": _CONTENT_HASH,
            "page": _page(page, 1),
            "warnings": [],
            "fetched_at": "2026-09-03T00:00:00+00:00",
            "validated_at": "2026-09-03T00:00:00+00:00",
            "expires_at": "2026-09-04T00:00:00+00:00",
        }

    async def search_in_legislation(
        self,
        legislation_id: str,
        query: str,
        *,
        page: int,
        page_size: int,
        include_snippet: bool,
        refresh: bool,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "search_in_legislation",
                {
                    "legislation_id": legislation_id,
                    "query": query,
                    "page": page,
                    "page_size": page_size,
                    "include_snippet": include_snippet,
                    "refresh": refresh,
                },
            )
        )
        return {
            "ok": True,
            "legislation_id": legislation_id,
            "hits": [],
            "page": _page(page, page_size),
            "warnings": [],
        }

    async def get_outline(
        self, legislation_id: str, *, refresh: bool
    ) -> dict[str, object]:
        self.calls.append(
            ("get_outline", {"legislation_id": legislation_id, "refresh": refresh})
        )
        return {
            "ok": True,
            "legislation_id": legislation_id,
            "source": "bedesten",
            "nodes": [],
            "warnings": [],
        }


class FakeConstitutional:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def search(self, **arguments: object) -> dict[str, object]:
        self.calls.append(("search", dict(arguments)))
        return _search_success(
            page=int(arguments["page"]), page_size=int(arguments["page_size"])
        )

    async def get_document(
        self, *, id: str, page: int, refresh: bool
    ) -> dict[str, object]:
        self.calls.append(
            ("get_document", {"id": id, "page": page, "refresh": refresh})
        )
        return _document_success(id, page=page, refresh=refresh)


@dataclass
class FakeRuntime:
    auth: FakeAuth = field(default_factory=FakeAuth)
    decisions: FakeDecisions = field(default_factory=FakeDecisions)
    legislation: FakeLegislation = field(default_factory=FakeLegislation)
    constitutional: FakeConstitutional = field(default_factory=FakeConstitutional)
    cache_reads: int = 0
    closes: int = 0

    @property
    def cache(self) -> object:
        self.cache_reads += 1
        return object()

    async def aclose(self) -> None:
        self.closes += 1

    @property
    def service_calls(self) -> int:
        return sum(
            len(service.calls)
            for service in (self.decisions, self.legislation, self.constitutional)
        )


def _server_with_runtime(runtime: FakeRuntime) -> object:
    @asynccontextmanager
    async def fake_lifespan(_server: object) -> AsyncIterator[dict[str, FakeRuntime]]:
        try:
            yield {"runtime": runtime}
        finally:
            await runtime.aclose()

    return create_server(lifespan_factory=fake_lifespan)


class _ToolCallResult(Protocol):
    @property
    def data(self) -> object: ...


@runtime_checkable
class _ModelDumpable(Protocol):
    def model_dump(self, *, mode: str) -> object: ...


def _result_data(result: _ToolCallResult) -> dict[str, Any]:
    data = result.data
    if isinstance(data, _ModelDumpable):
        data = data.model_dump(mode="json")
    assert isinstance(data, Mapping)
    return dict(data)


def _tool_schema(tool: object, camel_name: str) -> dict[str, object]:
    snake_name = camel_name[0].lower() + "".join(
        f"_{character.lower()}" if character.isupper() else character
        for character in camel_name[1:]
    )
    schema = getattr(tool, camel_name, None)
    if schema is None:
        schema = getattr(tool, snake_name, None)
    assert isinstance(schema, dict), (
        f"{getattr(tool, 'name', '<unknown>')} lacks {camel_name}"
    )
    return schema


def _schema_values(schema: object, key: str) -> list[object]:
    if isinstance(schema, Mapping):
        found = [schema[key]] if key in schema else []
        for value in schema.values():
            found.extend(_schema_values(value, key))
        return found
    if isinstance(schema, list):
        found: list[object] = []
        for value in schema:
            found.extend(_schema_values(value, key))
        return found
    return []


def _resolve_root_local_ref(schema: object, root: Mapping[str, object]) -> object:
    if not isinstance(schema, Mapping):
        return schema
    definitions = root.get("$defs")
    if not isinstance(definitions, Mapping):
        return schema

    resolved = schema
    seen_references: set[str] = set()
    while isinstance(resolved, Mapping):
        reference = resolved.get("$ref")
        if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
            break
        if reference in seen_references:
            break
        candidate = definitions.get(reference.removeprefix("#/$defs/"))
        if not isinstance(candidate, Mapping):
            break
        seen_references.add(reference)
        resolved = candidate
    return resolved


def _has_object_shape(
    actual: object, expected: Mapping[str, object], *, ok: bool
) -> bool:
    if not isinstance(actual, Mapping):
        return False
    actual_properties = actual.get("properties")
    expected_properties = expected.get("properties")
    if not isinstance(actual_properties, Mapping) or not isinstance(
        expected_properties, Mapping
    ):
        return False
    ok_schema = actual_properties.get("ok")
    return (
        actual.get("type") == expected.get("type") == "object"
        and actual.get("additionalProperties") == expected.get("additionalProperties")
        and set(actual_properties) == set(expected_properties)
        and set(actual.get("required", ())) == set(expected.get("required", ()))
        and isinstance(ok_schema, Mapping)
        and ok_schema.get("const") is ok
    )


def _has_error_body(schema: object, root: Mapping[str, object]) -> bool:
    schema = _resolve_root_local_ref(schema, root)
    if isinstance(schema, Mapping):
        properties = schema.get("properties")
        required = schema.get("required", ())
        if (
            isinstance(properties, Mapping)
            and {"code", "message", "retryable"}.issubset(properties)
            and isinstance(required, list)
            and {"code", "message", "retryable"}.issubset(required)
        ):
            return True
        return any(_has_error_body(value, root) for value in schema.values())
    if isinstance(schema, list):
        return any(_has_error_body(value, root) for value in schema)
    return False


def _is_success_alternative(
    schema: object, root: Mapping[str, object], name: str
) -> bool:
    return _has_object_shape(
        _resolve_root_local_ref(schema, root),
        _SCHEMA_DEFINITIONS[_SUCCESS_TYPES[name]],
        ok=True,
    )


def _is_error_alternative(schema: object, root: Mapping[str, object]) -> bool:
    resolved = _resolve_root_local_ref(schema, root)
    if not _has_object_shape(resolved, _SCHEMA_DEFINITIONS["ErrorEnvelope"], ok=False):
        return False
    assert isinstance(resolved, Mapping)
    properties = resolved["properties"]
    assert isinstance(properties, Mapping)
    return _has_error_body(properties["error"], root)


def _assert_output_schema_matches_contract(
    actual: dict[str, object], name: str
) -> None:
    alternatives = actual.get("oneOf", actual.get("anyOf"))
    assert isinstance(alternatives, list)
    assert len(alternatives) == 2
    assert any(_is_success_alternative(branch, actual, name) for branch in alternatives)
    assert any(_is_error_alternative(branch, actual) for branch in alternatives)


def _assert_input_schema_matches_contract(
    actual: dict[str, object], expected: Mapping[str, object]
) -> None:
    assert actual.get("type") == "object"
    assert actual.get("additionalProperties") is False
    actual_properties = actual.get("properties")
    expected_properties = expected["properties"]
    assert isinstance(actual_properties, Mapping)
    assert isinstance(expected_properties, Mapping)
    assert set(actual_properties) == set(expected_properties)
    assert set(actual.get("required", ())) == set(expected.get("required", ()))

    for name, expected_property in expected_properties.items():
        assert isinstance(expected_property, Mapping)
        actual_property = actual_properties[name]
        for keyword in (
            "default",
            "enum",
            "minimum",
            "maximum",
            "minLength",
            "minItems",
            "uniqueItems",
            "pattern",
        ):
            if keyword in expected_property:
                assert expected_property[keyword] in _schema_values(
                    actual_property, keyword
                ), f"{name} does not preserve {keyword}"

    for keyword in ("dependentRequired", "anyOf", "allOf"):
        if keyword in expected:
            assert actual.get(keyword) == expected[keyword]

    assert not {
        "url",
        "source_url",
        "document_url",
    }.intersection(actual_properties)


@pytest.mark.asyncio
async def test_v1_tool_catalog_and_schemas_match_contract() -> None:
    runtime = FakeRuntime()
    server = _server_with_runtime(runtime)

    async with Client(server) as client:
        registered = {tool.name: tool for tool in await client.list_tools()}

    registered_names = set(registered)
    assert registered_names == _EXPECTED_PUBLIC_TOOL_NAMES
    assert V1_TOOL_NAMES == _EXPECTED_PUBLIC_TOOL_NAMES
    assert set(_TOOL_CONTRACTS) == _EXPECTED_PUBLIC_TOOL_NAMES
    assert (
        not (registered_names | set(V1_TOOL_NAMES) | set(_TOOL_CONTRACTS))
        & _LEGACY_PUBLIC_TOOL_NAMES
    )
    assert len(registered) == 8
    for name, contract in _TOOL_CONTRACTS.items():
        _assert_input_schema_matches_contract(
            _tool_schema(registered[name], "inputSchema"), contract["input_schema"]
        )
        _assert_output_schema_matches_contract(
            _tool_schema(registered[name], "outputSchema"), name
        )

    assert runtime.closes == 1


@pytest.mark.asyncio
async def test_all_tools_forward_arguments_and_contract_defaults() -> None:
    runtime = FakeRuntime()
    server = _server_with_runtime(runtime)

    async with Client(server) as client:
        for name, arguments in _TOOL_CALLS.items():
            assert (
                _result_data(
                    await client.call_tool(name, arguments, raise_on_error=False)
                )["ok"]
                is True
            )

    assert runtime.auth.calls == 8
    assert runtime.decisions.calls == [
        (
            "search",
            {
                "courts": ["yargitay"],
                "query": "temerrüt",
                "chamber": None,
                "date_from": None,
                "date_to": None,
                "esas_year": None,
                "esas_sequence": None,
                "karar_year": None,
                "karar_sequence": None,
                "sort": "tarih_yeniden_eskiye",
                "page": 1,
                "page_size": 10,
                "include_snippet": False,
            },
        ),
        (
            "get_document",
            {"id": "bedesten:decision-42", "page": 1, "refresh": False},
        ),
    ]
    assert runtime.legislation.calls == [
        (
            "search",
            {
                "query": "borç",
                "title": None,
                "number": None,
                "types": None,
                "exact_title": False,
                "gazette_date_from": None,
                "gazette_date_to": None,
                "gazette_issue": None,
                "page": 1,
                "page_size": 20,
                "include_snippet": False,
            },
        ),
        (
            "get_article",
            {
                "legislation_id": "mevzuat:law-42",
                "article_number": "25/A",
                "page": 1,
                "refresh": False,
            },
        ),
        (
            "search_in_legislation",
            {
                "legislation_id": "mevzuat:law-42",
                "query": "borç AND temerrüt",
                "page": 1,
                "page_size": 25,
                "include_snippet": True,
                "refresh": False,
            },
        ),
        (
            "get_outline",
            {"legislation_id": "mevzuat:law-42", "refresh": False},
        ),
    ]
    assert runtime.constitutional.calls == [
        (
            "search",
            {
                "kind": "norm_denetimi",
                "query": "iptal",
                "esas_no": None,
                "karar_no": None,
                "application_number": None,
                "page": 1,
                "page_size": 10,
                "include_snippet": False,
            },
        )
    ]
    assert runtime.closes == 1


@pytest.mark.asyncio
async def test_belge_getir_routes_namespaced_ids_and_rejects_raw_urls() -> None:
    runtime = FakeRuntime()
    server = _server_with_runtime(runtime)

    async with Client(server) as client:
        for document_id in (
            "bedesten:decision-77",
            "mevzuat:law-77",
            "anayasa:nd:2024:7",
            "anayasa:bb:2025:8",
        ):
            result = await client.call_tool(
                "belge_getir", {"id": document_id}, raise_on_error=False
            )
            assert _result_data(result)["id"] == document_id

        invalid = await client.call_tool(
            "belge_getir",
            {"id": "https://source.invalid/not-an-id"},
            raise_on_error=False,
        )

    invalid_payload = _result_data(invalid)
    assert invalid_payload["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert invalid_payload["error"]["details"]["field_errors"]["id"]
    assert runtime.decisions.calls == [
        (
            "get_document",
            {"id": "bedesten:decision-77", "page": 1, "refresh": False},
        )
    ]
    assert runtime.legislation.calls == [
        (
            "get_document",
            {"legislation_id": "mevzuat:law-77", "page": 1, "refresh": False},
        )
    ]
    assert runtime.constitutional.calls == [
        (
            "get_document",
            {"id": "anayasa:nd:2024:7", "page": 1, "refresh": False},
        ),
        (
            "get_document",
            {"id": "anayasa:bb:2025:8", "page": 1, "refresh": False},
        ),
    ]


def _activation_error(code: ErrorCode) -> ActivationError:
    return ActivationError(
        code,
        f"{code.value} fixture",
        http_status=409 if code is ErrorCode.ACCOUNT_LINK_REQUIRED else 403,
        terms_url=_TERMS_URL if code is ErrorCode.TERMS_REQUIRED else None,
    )


_AUTH_FAILURES: tuple[tuple[Callable[[], Exception], ErrorCode, str | None], ...] = (
    (AuthenticationRequired, ErrorCode.AUTHENTICATION_REQUIRED, None),
    (lambda: SessionExpired("refresh token expired"), ErrorCode.SESSION_EXPIRED, None),
    (
        lambda: _activation_error(ErrorCode.TERMS_REQUIRED),
        ErrorCode.TERMS_REQUIRED,
        _TERMS_URL,
    ),
    (
        lambda: _activation_error(ErrorCode.ACCOUNT_DISABLED),
        ErrorCode.ACCOUNT_DISABLED,
        None,
    ),
    (
        lambda: _activation_error(ErrorCode.ACCOUNT_LINK_REQUIRED),
        ErrorCode.ACCOUNT_LINK_REQUIRED,
        None,
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", _TOOL_CALLS.items())
@pytest.mark.parametrize("error_factory,code,terms_url", _AUTH_FAILURES)
async def test_every_raw_tool_call_authenticates_before_any_service_or_cache_access(
    name: str,
    arguments: dict[str, object],
    error_factory: Callable[[], Exception],
    code: ErrorCode,
    terms_url: str | None,
) -> None:
    runtime = FakeRuntime(auth=FakeAuth(error_factory()))
    server = _server_with_runtime(runtime)

    async with Client(server) as client:
        result = await client.call_tool(name, arguments, raise_on_error=False)

    payload = _result_data(result)
    error = payload["error"]
    assert payload["ok"] is False
    assert error["code"] == code
    assert error["retryable"] is False
    if terms_url is None:
        assert "details" not in error or "terms_url" not in error["details"]
    else:
        assert error["details"]["terms_url"] == terms_url
    assert runtime.auth.calls == 1
    assert runtime.cache_reads == 0
    assert runtime.service_calls == 0
    assert runtime.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", _UNAUTHENTICATED_INVALID_RAW_CALLS)
async def test_unauthenticated_invalid_raw_calls_return_auth_before_validation(
    name: str, arguments: dict[str, object]
) -> None:
    runtime = FakeRuntime(auth=FakeAuth(AuthenticationRequired()))
    server = _server_with_runtime(runtime)

    async with Client(server) as client:
        result = await client.call_tool_mcp(name, arguments)
    assert isinstance(result.structuredContent, Mapping)
    payload = dict(result.structuredContent)
    assert payload["ok"] is False
    assert payload["error"]["code"] == ErrorCode.AUTHENTICATION_REQUIRED
    assert runtime.auth.calls == 1
    assert runtime.cache_reads == 0
    assert runtime.service_calls == 0
    assert runtime.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", _AUTHORIZED_INVALID_RAW_CALLS)
async def test_authorized_invalid_raw_calls_return_contract_invalid_params(
    name: str, arguments: dict[str, object]
) -> None:
    runtime = FakeRuntime()
    server = _server_with_runtime(runtime)

    async with Client(server) as client:
        result = await client.call_tool_mcp(name, arguments)

    assert isinstance(result.structuredContent, Mapping)
    payload = dict(result.structuredContent)
    assert payload["ok"] is False
    assert payload["error"]["code"] == ErrorCode.INVALID_PARAMS
    field_errors = payload["error"]["details"]["field_errors"]
    assert isinstance(field_errors, Mapping)
    assert field_errors
    assert runtime.auth.calls == 1
    assert runtime.service_calls == 0
    assert runtime.closes == 1


@pytest.mark.asyncio
async def test_lifespan_closes_fake_runtime_once_after_client_disconnects() -> None:
    runtime = FakeRuntime()
    server = _server_with_runtime(runtime)

    async with Client(server) as client:
        assert {tool.name for tool in await client.list_tools()} == V1_TOOL_NAMES
        assert runtime.closes == 0

    assert runtime.closes == 1


@pytest.mark.asyncio
async def test_tool_lifespan_signals_after_creating_the_real_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "runtime-ready.marker"
    marker.write_bytes(b"")
    if os.name == "posix":
        marker.chmod(0o600)
    settings = Settings(
        auth_base_url="https://auth.test",
        terms_base_url="https://terms.test",
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
        model_dir=tmp_path / "models",
    )
    monkeypatch.setenv(tools._RUNTIME_READY_FILE_ENV, str(marker))
    monkeypatch.setattr(tools, "load_settings", lambda: settings)

    async with tools.tool_lifespan(object()) as context:
        assert isinstance(context["runtime"], tools.ToolRuntime)
        assert settings.cache_db_path.is_file()
        assert marker.read_bytes() == b"ready\n"
        if os.name == "posix":
            assert stat.S_IMODE(marker.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_unavailable_tool_lifespan_never_signals_runtime_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "runtime-ready.marker"
    marker.write_bytes(b"")
    if os.name == "posix":
        marker.chmod(0o600)
    monkeypatch.setenv(tools._RUNTIME_READY_FILE_ENV, str(marker))

    def unavailable_settings() -> Settings:
        raise ValueError("deployment configuration is incomplete")

    monkeypatch.setattr(tools, "load_settings", unavailable_settings)

    async with tools.tool_lifespan(object()) as context:
        assert isinstance(context["runtime"], tools._UnavailableRuntime)
        assert marker.read_bytes() == b""


@pytest.mark.asyncio
async def test_missing_lifespan_runtime_returns_not_configured() -> None:
    @asynccontextmanager
    async def unavailable_lifespan(_server: object) -> AsyncIterator[dict[str, object]]:
        yield {}

    server = create_server(lifespan_factory=unavailable_lifespan)

    async with Client(server) as client:
        result = await client.call_tool_mcp("karar_ara", {})
    assert isinstance(result.structuredContent, Mapping)
    payload = dict(result.structuredContent)
    assert payload["ok"] is False
    assert payload["error"]["code"] == ErrorCode.NOT_CONFIGURED
    assert payload["error"]["retryable"] is False
    assert payload["error"]["message"]


@pytest.mark.asyncio
async def test_court_validation_lists_exact_values_for_recovery() -> None:
    runtime = FakeRuntime()
    async with Client(_server_with_runtime(runtime)) as client:
        result = await client.call_tool_mcp(
            "karar_ara", {"courts": ["YARGITAY"], "query": "kira"}
        )
    assert result.structuredContent["error"]["details"]["field_errors"]["courts.0"] == [
        "İzin verilen değerler: yargitay, danistay, istinaf, yerel, kyb."
    ]
    assert runtime.service_calls == 0


@pytest.mark.asyncio
async def test_research_start_is_honest_without_search_and_can_resolve_title() -> None:
    runtime = FakeRuntime()
    async with Client(_server_with_runtime(runtime)) as client:
        guide = _result_data(
            await client.call_tool(
                "turk_hukuku_sorularinda_once_bu_araci_cagir",
                {"soru": "Aidatı kim öder?"},
            )
        )
        assert guide["research_performed"] is False
        assert "initial_search" not in guide
        assert len(guide["tools"]) == 7
        searched = _result_data(
            await client.call_tool(
                "turk_hukuku_sorularinda_once_bu_araci_cagir",
                {"soru": "Aidatı kim öder?", "mevzuat_adi": "Kat Mülkiyeti Kanunu"},
            )
        )
        assert searched["research_performed"] is True
        assert searched["initial_search"]["ok"] is True


@pytest.mark.asyncio
async def test_announcements_are_added_to_success_without_changing_errors(
    tmp_path,
) -> None:
    import httpx

    from mutalaamcp.announcements import Announcements

    item = {"id": "news-1", "title": "Duyuru", "body": "Ürün haberi", "url": None}
    runtime = FakeRuntime()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"announcement": item})
        )
    ) as http:
        runtime.announcements = Announcements(http, "https://mutalaa.tr", tmp_path)
        async with Client(_server_with_runtime(runtime)) as client:
            invalid = _result_data(
                await client.call_tool(
                    "mevzuat_ara", {"page_size": 999}, raise_on_error=False
                )
            )
            assert invalid["ok"] is False
            assert "announcement" not in invalid
            result = _result_data(
                await client.call_tool("mevzuat_ara", {"number": "634"})
            )
            assert result["ok"] is True
            assert result["announcement"] == item
            repeated = _result_data(
                await client.call_tool("mevzuat_ara", {"number": "634"})
            )
            assert "announcement" not in repeated
