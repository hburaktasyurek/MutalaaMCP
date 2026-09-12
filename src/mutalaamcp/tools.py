"""The contract-defined FastMCP research tools and their runtime lifecycle."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import mcp.types as mt
from fastmcp import Context, FastMCP
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from fastmcp.tools.function_tool import FunctionTool

from mutalaamcp.announcements import Announcements
from mutalaamcp.auth import (
    ActivationError,
    ActivationProtocolError,
    AuthenticationRequired,
    AuthSession,
    DeviceFlowError,
    SessionExpired,
    activation_error_message,
)
from mutalaamcp.cache.store import CacheStore
from mutalaamcp.domain.errors import ErrorBody, ErrorCode, ErrorDetails, ErrorEnvelope
from mutalaamcp.domain.ids import (
    AnayasaId,
    BedestenId,
    InvalidDocumentId,
    MevzuatId,
    parse_document_id,
)
from mutalaamcp.domain.models import ConstitutionalKind, Court, LegislationType
from mutalaamcp.net import Origin, OriginProtection, SafeHttpClient
from mutalaamcp.ocr import resolve_local_ocr
from mutalaamcp.providers.anayasa.client import AYM_ALLOWED_HOSTS, AnayasaClient
from mutalaamcp.providers.bedesten.decisions import BedestenDecisionProvider
from mutalaamcp.providers.bedesten.legislation import BedestenLegislationProvider
from mutalaamcp.services.common import error_envelope
from mutalaamcp.services.constitutional import ConstitutionalService
from mutalaamcp.services.decisions import DecisionService
from mutalaamcp.services.legislation import LegislationService
from mutalaamcp.settings import Settings
from mutalaamcp.tool_schemas import (
    fastmcp_output_schema,
    standalone_tool_schema,
    v1_tool_contract,
)

_BEDSETEN_HOST = "bedesten.adalet.gov.tr"
_BEDSETEN_ORIGIN: Origin = ("https", _BEDSETEN_HOST, None)
_RESEARCH_HOSTS = frozenset({_BEDSETEN_HOST, *AYM_ALLOWED_HOSTS})
_SOURCE_RATE_CAPACITY = 10
_SOURCE_RATE_WINDOW_SECONDS = 30.0
_SOURCE_MAX_CONCURRENCY = 2
_RUNTIME_READY_FILE_ENV = "MUTALAAMCP_RUNTIME_READY_FILE"
_READY_MARKER = b"ready\n"


def _signal_runtime_ready() -> None:
    """Atomically acknowledge a launcher-provided private readiness marker."""
    raw_marker = os.environ.get(_RUNTIME_READY_FILE_ENV)
    if raw_marker is None:
        return
    marker = Path(raw_marker)
    if not marker.is_absolute():
        raise RuntimeError(
            "Çalışma zamanı hazır olma işaretleyicisinin yolu mutlak olmalıdır."
        )
    try:
        status = marker.lstat()
    except OSError as exc:
        raise RuntimeError(
            "Çalışma zamanı hazır olma işaretleyicisine erişilemiyor."
        ) from exc
    if not stat.S_ISREG(status.st_mode) or status.st_size != 0:
        raise RuntimeError(
            "Çalışma zamanı hazır olma işaretleyicisi boş bir normal dosya olmalıdır."
        )
    if os.name == "posix" and (
        status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) != 0o600
    ):
        raise RuntimeError("Çalışma zamanı hazır olma işaretleyicisi özel değildir.")
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{marker.name}.", suffix=".tmp", dir=marker.parent
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as output:
            if os.name == "posix":
                os.fchmod(output.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            output.write(_READY_MARKER)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, marker)
        if os.name != "nt":
            directory = os.open(marker.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(slots=True)
class HttpClients:
    """The lifespan-owned clients for authenticated and official-source traffic."""

    auth: httpx.AsyncClient
    research: httpx.AsyncClient

    async def aclose(self) -> None:
        try:
            await self.auth.aclose()
        finally:
            await self.research.aclose()


@dataclass(slots=True)
class ResearchServices:
    """The provider-backed V1 service boundaries."""

    decisions: DecisionService
    legislation: LegislationService
    constitutional: ConstitutionalService


@dataclass(slots=True)
class ToolRuntime:
    """All resources created once for one FastMCP lifespan."""

    auth: AuthSession
    decisions: DecisionService
    legislation: LegislationService
    constitutional: ConstitutionalService
    cache: CacheStore
    clients: HttpClients
    announcements: Announcements | None = None

    async def aclose(self) -> None:
        try:
            self.cache.close()
        finally:
            await self.clients.aclose()


@dataclass(frozen=True, slots=True)
class _UnavailableRuntime:
    """A deliberately non-operational runtime yielded when configuration fails."""


def load_settings() -> Settings:
    """Load environment settings only when the server lifespan starts."""
    return Settings()


def create_http_clients(settings: Settings) -> HttpClients:
    """Create the two lifespan-owned HTTP clients without sending a request."""
    timeout = httpx.Timeout(
        timeout=settings.http_timeout_seconds,
        connect=settings.http_connect_timeout_seconds,
    )
    return HttpClients(
        auth=httpx.AsyncClient(timeout=timeout, follow_redirects=False),
        research=httpx.AsyncClient(timeout=timeout, follow_redirects=False),
    )


def create_cache_store(settings: Settings) -> CacheStore:
    """Open the local cache only after validated settings enter the lifespan."""
    return CacheStore(settings.cache_db_path, max_bytes=settings.cache_max_bytes)


def create_auth_session(settings: Settings, client: httpx.AsyncClient) -> AuthSession:
    """Build the Mütalaa-brokered account and activation gate."""
    return AuthSession(
        client,
        auth_base_url=settings.auth_base_url,
        allowed_terms_base_url=settings.terms_base_url,
        data_dir=settings.data_dir,
    )


def _research_origin_protections() -> dict[Origin, OriginProtection]:
    """Allocate one shared budget for every configured official origin."""
    origins: set[Origin] = {_BEDSETEN_ORIGIN}
    origins.update(("https", host, None) for host in AYM_ALLOWED_HOSTS)
    return {
        origin: OriginProtection(
            capacity=_SOURCE_RATE_CAPACITY,
            window_seconds=_SOURCE_RATE_WINDOW_SECONDS,
            max_concurrency=_SOURCE_MAX_CONCURRENCY,
        )
        for origin in origins
    }


class _SafeHttpManagedLimiter:
    """Avoid a second provider-local budget when SafeHttpClient owns the origin."""

    async def acquire(self) -> None:
        return None

    async def pause(self, _retry_after: float | None) -> None:
        return None


def create_services(
    cache: CacheStore, http: SafeHttpClient, *, ocr: Any | None = None
) -> ResearchServices:
    """Create the official-source providers and their cache-backed services."""
    return ResearchServices(
        decisions=DecisionService(
            BedestenDecisionProvider(http, limiter=_SafeHttpManagedLimiter()),
            cache,
            ocr=ocr,
        ),
        legislation=LegislationService(
            BedestenLegislationProvider(http),
            cache,
            ocr=ocr,
        ),
        constitutional=ConstitutionalService(AnayasaClient(http), cache),
    )


async def create_runtime(settings: Settings) -> ToolRuntime:
    """Allocate all operational resources, cleaning up a partially built runtime."""
    clients = create_http_clients(settings)
    cache: CacheStore | None = None
    try:
        cache = create_cache_store(settings)
        research_http = SafeHttpClient(
            clients.research,
            _RESEARCH_HOSTS,
            timeout=httpx.Timeout(
                timeout=settings.http_timeout_seconds,
                connect=settings.http_connect_timeout_seconds,
            ),
            max_bytes=settings.max_download_bytes,
            origin_protections=_research_origin_protections(),
        )
        services = create_services(
            cache, research_http, ocr=resolve_local_ocr(settings)
        )
        return ToolRuntime(
            auth=create_auth_session(settings, clients.auth),
            decisions=services.decisions,
            legislation=services.legislation,
            constitutional=services.constitutional,
            cache=cache,
            clients=clients,
            announcements=Announcements(
                clients.auth, settings.auth_base_url, settings.data_dir
            ),
        )
    except BaseException:
        try:
            if cache is not None:
                cache.close()
        finally:
            await clients.aclose()
        raise


@asynccontextmanager
async def _runtime(settings: Settings) -> AsyncIterator[ToolRuntime]:
    """Yield one operational runtime and close every owned resource on exit."""
    runtime = await create_runtime(settings)
    try:
        yield runtime
    finally:
        await runtime.aclose()


@lifespan
async def tool_lifespan(
    _server: FastMCP,
) -> AsyncIterator[dict[str, ToolRuntime | _UnavailableRuntime]]:
    """Expose a non-operational context instead of failing startup when unconfigured."""
    try:
        settings = load_settings()
        manager = _runtime(settings)
        runtime = await manager.__aenter__()
    except Exception:  # noqa: BLE001 -- lifespan startup yields an unavailable runtime
        yield {"runtime": _UnavailableRuntime()}
        return

    try:
        _signal_runtime_ready()
        yield {"runtime": runtime}
    finally:
        await manager.__aexit__(None, None, None)


def _tool_description(name: str) -> str:
    description = v1_tool_contract(name).get("description")
    if not isinstance(description, str):
        raise TypeError(f"V1 aracı {name} için bir açıklama eksik.")
    return description


def _runtime_for(ctx: Context) -> ToolRuntime | _UnavailableRuntime:
    context = ctx.lifespan_context
    if isinstance(context, Mapping):
        return cast(
            ToolRuntime | _UnavailableRuntime,
            context.get("runtime", _UnavailableRuntime()),
        )
    return cast(ToolRuntime | _UnavailableRuntime, context)


def _envelope(
    code: ErrorCode,
    message: str,
    *,
    field_errors: dict[str, tuple[str, ...]] | None = None,
    retry_after: float | None = None,
    terms_url: str | None = None,
) -> dict[str, Any]:
    details = ErrorDetails(
        field_errors=field_errors,
        retry_after=retry_after,
        terms_url=terms_url,
    )
    if details.model_dump() == {}:
        details = None  # type: ignore[assignment]
    return ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=message,
            retryable=code
            in {ErrorCode.UPSTREAM_RATE_LIMITED, ErrorCode.UPSTREAM_UNAVAILABLE},
            details=details,
        )
    ).model_dump(mode="json")


def _not_configured() -> dict[str, Any]:
    return _envelope(ErrorCode.NOT_CONFIGURED, "MutalaaMCP yapılandırılmamış.")


def _authorization_error(error: Exception) -> dict[str, Any]:
    reauth_hint = (
        " Yeniden giriş için istemcide MutalaaMCP'nin kimlik doğrulama "
        "akışını başlatın veya terminalde `mutalaamcp auth login` çalıştırın."
    )
    if isinstance(error, AuthenticationRequired):
        return _envelope(
            ErrorCode.AUTHENTICATION_REQUIRED,
            "Kimlik doğrulaması gerekli." + reauth_hint,
        )
    if isinstance(error, SessionExpired):
        return _envelope(
            ErrorCode.SESSION_EXPIRED, "Oturumun süresi doldu." + reauth_hint
        )
    if isinstance(error, ActivationError):
        return _envelope(
            error.code,
            activation_error_message(error.code, error.remote_detail),
            retry_after=(
                error.retry_after
                if error.code is ErrorCode.UPSTREAM_RATE_LIMITED
                else None
            ),
            terms_url=(
                error.terms_url if error.code is ErrorCode.TERMS_REQUIRED else None
            ),
        )
    if isinstance(error, (ActivationProtocolError, DeviceFlowError, httpx.HTTPError)):
        return _envelope(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "Kimlik doğrulama hizmetine şu anda erişilemiyor.",
        )
    return _envelope(ErrorCode.UPSTREAM_UNAVAILABLE, "Kimlik doğrulama başarısız oldu.")


async def _ensure_authorized(
    runtime: ToolRuntime | _UnavailableRuntime,
) -> dict[str, Any] | None:
    if isinstance(runtime, _UnavailableRuntime):
        return _not_configured()
    try:
        await runtime.auth.ensure_authorized()
    except Exception as error:  # noqa: BLE001 -- auth errors become safe envelopes
        return _authorization_error(error)
    return None


@cache
def _input_schema(name: str) -> dict[str, Any]:
    """Load one immutable V1 input contract for boundary validation."""
    return standalone_tool_schema(name, "input")


def _add_input_error(
    errors: dict[str, list[str]], path: tuple[str | int, ...], message: str
) -> None:
    field = ".".join(str(part) for part in path) or "request"
    errors.setdefault(field, []).append(message)


def _json_type_matches(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _schema_errors(
    value: object,
    schema: Mapping[str, Any],
    *,
    root: Mapping[str, Any],
    path: tuple[str | int, ...],
    errors: dict[str, list[str]],
) -> None:
    """Validate the V1 contract's JSON Schema subset without coercing raw input."""
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        definition_name = reference.removeprefix("#/$defs/")
        definitions = root.get("$defs")
        if isinstance(definitions, Mapping) and isinstance(
            definition := definitions.get(definition_name), Mapping
        ):
            _schema_errors(value, definition, root=root, path=path, errors=errors)
        else:
            _add_input_error(
                errors, path, "Bilinmeyen bir sözleşme tanımına başvuruyor."
            )
            return

    expected_type = schema.get("type")
    expected_types: tuple[str, ...]
    if isinstance(expected_type, str):
        expected_types = (expected_type,)
    elif isinstance(expected_type, list) and all(
        isinstance(item, str) for item in expected_type
    ):
        expected_types = tuple(expected_type)
    else:
        expected_types = ()
    if expected_types and not any(
        _json_type_matches(value, expected) for expected in expected_types
    ):
        _add_input_error(
            errors,
            path,
            f"Şu türlerden biri olmalıdır: {', '.join(expected_types)}.",
        )
        return

    if "const" in schema and value != schema["const"]:
        _add_input_error(errors, path, "Sözleşmedeki değerle aynı olmalıdır.")
    if isinstance(enum := schema.get("enum"), list) and value not in enum:
        allowed = ", ".join(str(item) for item in enum)
        _add_input_error(errors, path, f"İzin verilen değerler: {allowed}.")

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            if isinstance(branch, Mapping):
                _schema_errors(value, branch, root=root, path=path, errors=errors)

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        branch_errors: list[dict[str, list[str]]] = []
        for branch in any_of:
            if not isinstance(branch, Mapping):
                continue
            any_of_candidate_errors: dict[str, list[str]] = {}
            _schema_errors(
                value,
                branch,
                root=root,
                path=path,
                errors=any_of_candidate_errors,
            )
            if not any_of_candidate_errors:
                break
            branch_errors.append(any_of_candidate_errors)
        else:
            _add_input_error(
                errors, path, "İzin verilen biçimlerden en az biriyle eşleşmelidir."
            )

    not_schema = schema.get("not")
    if isinstance(not_schema, Mapping):
        not_candidate_errors: dict[str, list[str]] = {}
        _schema_errors(
            value,
            not_schema,
            root=root,
            path=path,
            errors=not_candidate_errors,
        )
        if not not_candidate_errors:
            _add_input_error(
                errors, path, "Yasak bir parametre birleşimiyle eşleşiyor."
            )

    if_schema = schema.get("if")
    if isinstance(if_schema, Mapping):
        condition_errors: dict[str, list[str]] = {}
        _schema_errors(
            value,
            if_schema,
            root=root,
            path=path,
            errors=condition_errors,
        )
        branch_name = "then" if not condition_errors else "else"
        branch = schema.get(branch_name)
        if isinstance(branch, Mapping):
            _schema_errors(value, branch, root=root, path=path, errors=errors)

    if isinstance(value, Mapping):
        properties = schema.get("properties")
        property_schemas = properties if isinstance(properties, Mapping) else {}
        required = schema.get("required")
        if isinstance(required, list):
            for field in required:
                if isinstance(field, str) and field not in value:
                    _add_input_error(errors, path + (field,), "Alan zorunludur.")

        dependent_required = schema.get("dependentRequired")
        if isinstance(dependent_required, Mapping):
            for field, dependencies in dependent_required.items():
                if field not in value or not isinstance(dependencies, list):
                    continue
                for dependency in dependencies:
                    if isinstance(dependency, str) and dependency not in value:
                        _add_input_error(
                            errors,
                            path + (dependency,),
                            f"{field} sağlandığında bu alan da zorunludur.",
                        )

        if schema.get("additionalProperties") is False:
            for field in value:
                if field not in property_schemas:
                    _add_input_error(
                        errors,
                        path + (str(field),),
                        "İzin verilen bir parametre değildir.",
                    )

        for field, property_schema in property_schemas.items():
            if (
                field in value
                and isinstance(field, str)
                and isinstance(property_schema, Mapping)
            ):
                _schema_errors(
                    value[field],
                    property_schema,
                    root=root,
                    path=path + (field,),
                    errors=errors,
                )

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            _add_input_error(
                errors,
                path,
                f"En az {minimum_items} öğe içermelidir.",
            )
        maximum_items = schema.get("maxItems")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            _add_input_error(
                errors,
                path,
                f"En fazla {maximum_items} öğe içermelidir.",
            )
        if schema.get("uniqueItems") is True:
            for index, item in enumerate(value):
                if any(item == previous for previous in value[:index]):
                    _add_input_error(errors, path, "Yinelenen öğeler içermemelidir.")
                    break
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _schema_errors(
                    item,
                    item_schema,
                    root=root,
                    path=path + (index,),
                    errors=errors,
                )

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            _add_input_error(
                errors,
                path,
                f"En az {minimum_length} karakter içermelidir.",
            )
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            _add_input_error(errors, path, "Zorunlu biçimle eşleşmiyor.")
        if schema.get("format") == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                _add_input_error(errors, path, "Geçerli bir takvim tarihi olmalıdır.")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            _add_input_error(errors, path, f"En az {minimum} olmalıdır.")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            _add_input_error(errors, path, f"En fazla {maximum} olmalıdır.")


def _contract_input_errors(
    tool_name: str, arguments: object
) -> dict[str, tuple[str, ...]]:
    """Return contract-schema failures in the canonical envelope representation."""
    errors: dict[str, list[str]] = {}
    schema = _input_schema(tool_name)
    _schema_errors(arguments, schema, root=schema, path=(), errors=errors)
    return {field: tuple(messages) for field, messages in errors.items()}


class ResearchAuthorizationMiddleware(Middleware):
    """Authorize raw V1 calls, then enforce their authoritative input contract."""

    def __init__(self, tool_names: frozenset[str]) -> None:
        self._tool_names = tool_names

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        # Keep the raw arguments opaque until the request has passed this gate.
        tool_name, arguments = context.message.name, context.message.arguments
        if tool_name not in self._tool_names:
            return await call_next(context)
        runtime = (
            _runtime_for(context.fastmcp_context)
            if context.fastmcp_context is not None
            else _UnavailableRuntime()
        )
        if error := await _ensure_authorized(runtime):
            return ToolResult(structured_content=error)
        if field_errors := _contract_input_errors(tool_name, arguments):
            return ToolResult(
                structured_content=_envelope(
                    ErrorCode.INVALID_PARAMS,
                    "İstek parametreleri geçersiz.",
                    field_errors=field_errors,
                )
            )
        result = await call_next(context)
        payload = result.structured_content
        announcements = getattr(runtime, "announcements", None)
        if isinstance(payload, dict) and payload.get("ok") is True and announcements:
            announcement = await announcements.take()
            if announcement is not None:
                return ToolResult(
                    structured_content={**payload, "announcement": announcement}
                )
        return result


async def _call(
    operation: Callable[[], Awaitable[dict[str, Any] | dict[str, object]]],
) -> dict[str, Any] | dict[str, object]:
    try:
        return await operation()
    except Exception as error:  # noqa: BLE001 -- tool failures become MCP envelopes
        return error_envelope(error)


def _invalid_document_id(error: InvalidDocumentId) -> dict[str, Any]:
    return _envelope(
        ErrorCode.INVALID_PARAMS,
        "İstek parametreleri geçersiz.",
        field_errors={"id": (str(error),)},
    )


def _register(mcp: FastMCP, name: str, function: Callable[..., Any]) -> None:
    """Register one callable with its authoritative input and output schemas."""
    inferred = FunctionTool.from_function(
        function,
        name=name,
        description=_tool_description(name),
        output_schema=fastmcp_output_schema(name),
    )
    mcp.add_tool(
        inferred.model_copy(
            update={"parameters": standalone_tool_schema(name, "input")}
        )
    )


def register_tools(mcp: FastMCP, /) -> None:
    """Register the contract-defined research tools."""

    async def start_legal_research(
        ctx: Context,
        soru: str,
        mevzuat_adi: str | None = None,
    ) -> dict[str, Any]:
        """Introduce research tools and optionally resolve a known law title."""
        result: dict[str, Any] = {
            "ok": True,
            "research_performed": False,
            "guidance": [
                "KESİNLİKLE ÖNEMLİ — VERİ GİZLİLİĞİ: Mütalaa'ya giden her argüman anonimleştirilerek yazılır; hiçbir araca gizli bilgi gönderilmez. Gerçek kişi adları, kimlik numaraları, adres ve iletişim bilgileri, yerel dosya yolları, kamuya açık olmayan veya devam eden dava, soruşturma, icra ve başvuru numaraları ile hukuki konuyu belirlemek için gerekmeyen taraf ve belge ayrıntıları argümanlara girmez; olay 'davacı', 'davalı mirasçı', 'muris' gibi soyut rollerle anlatılır. Yayımlanmış mevzuat numaraları ve resmî karar kimlikleri gerektiğinde kullanılabilir.",
                "Bu çıktı hukuki görüş değildir. Sorunun dayanağını aşağıdaki araçlarla araştırın.",
                "Mevzuat adı biliniyorsa mevzuat_ara(title=...) kullanın; bilinmiyorsa kısa hukuki terimlerle query araması yapın.",
                "Sonuçlar tarihe göre sıralıdır. Başlık, numara ve türü doğrulayın; ilk kaydı otomatik seçmeyin.",
                "Yalnız aramadan dönen ID ile mevzuat_madde_getir veya belge_getir çağırın. Metni okumadan hukuki sonuç çıkarmayın.",
                "Madde bilinmiyorsa mevzuat_icinde_ara ile ilgili terimleri bulun. Gerekirse karar_ara veya anayasa_karari_ara ile içtihat araştırın.",
                "Kaynağı ve eksiklikleri belirtin. Kullanıcının açık kaynak tercihini gözetin; kapsam dışı konularda başka kaynak kullanın.",
            ],
            "tools": [
                {"name": name, "description": _tool_description(name)}
                for name in (
                    "mevzuat_ara",
                    "mevzuat_madde_getir",
                    "mevzuat_icinde_ara",
                    "mevzuat_madde_agaci_getir",
                    "karar_ara",
                    "anayasa_karari_ara",
                    "belge_getir",
                )
            ],
        }
        if mevzuat_adi is not None:
            runtime = cast(ToolRuntime, _runtime_for(ctx))
            search = await _call(
                lambda: runtime.legislation.search(
                    title=mevzuat_adi, page=1, page_size=5
                )
            )
            if not search.get("ok"):
                return dict(search)
            result["research_performed"] = True
            result["initial_search"] = search
        return result

    async def search_decisions(
        ctx: Context,
        courts: list[Court],
        query: str | None = None,
        chamber: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        esas_year: int | None = None,
        esas_sequence: int | None = None,
        karar_year: int | None = None,
        karar_sequence: int | None = None,
        sort: Literal[
            "tarih_yeniden_eskiye", "tarih_eskiden_yeniye"
        ] = "tarih_yeniden_eskiye",
        page: int = 1,
        page_size: int = 10,
        include_snippet: bool = False,
    ) -> dict[str, Any]:
        """Search the V1 Bedesten decisions surface."""
        runtime = cast(ToolRuntime, _runtime_for(ctx))
        return await _call(
            lambda: runtime.decisions.search(
                courts=courts,
                query=query,
                chamber=chamber,
                date_from=date_from,
                date_to=date_to,
                esas_year=esas_year,
                esas_sequence=esas_sequence,
                karar_year=karar_year,
                karar_sequence=karar_sequence,
                sort=sort,
                page=page,
                page_size=page_size,
                include_snippet=include_snippet,
            )
        )

    async def search_legislation(
        ctx: Context,
        query: str | None = None,
        title: str | None = None,
        number: str | None = None,
        types: list[LegislationType] | None = None,
        exact_title: bool = False,
        gazette_date_from: date | None = None,
        gazette_date_to: date | None = None,
        gazette_issue: str | None = None,
        page: int = 1,
        page_size: int = 20,
        include_snippet: bool = False,
    ) -> dict[str, Any]:
        """Search the V1 Bedesten legislation surface."""
        runtime = cast(ToolRuntime, _runtime_for(ctx))
        return await _call(
            lambda: runtime.legislation.search(
                query=query,
                title=title,
                number=number,
                types=types,
                exact_title=exact_title,
                gazette_date_from=gazette_date_from,
                gazette_date_to=gazette_date_to,
                gazette_issue=gazette_issue,
                page=page,
                page_size=page_size,
                include_snippet=include_snippet,
            )
        )

    async def search_constitutional(
        ctx: Context,
        kind: ConstitutionalKind,
        query: str | None = None,
        esas_no: str | None = None,
        karar_no: str | None = None,
        application_number: str | None = None,
        page: int = 1,
        page_size: int = 10,
        include_snippet: bool = False,
    ) -> dict[str, Any]:
        """Search the V1 Constitutional Court surface."""
        runtime = cast(ToolRuntime, _runtime_for(ctx))
        return await _call(
            lambda: runtime.constitutional.search(
                kind=kind,
                query=query,
                esas_no=esas_no,
                karar_no=karar_no,
                application_number=application_number,
                page=page,
                page_size=page_size,
                include_snippet=include_snippet,
            )
        )

    async def get_document(
        ctx: Context,
        id: str,
        page: int = 1,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Fetch one page through the parsed document-ID namespace router."""
        runtime = cast(ToolRuntime, _runtime_for(ctx))
        try:
            document_id = parse_document_id(id)
        except InvalidDocumentId as invalid_document_id:
            return _invalid_document_id(invalid_document_id)
        if isinstance(document_id, BedestenId):
            return await _call(
                lambda: runtime.decisions.get_document(
                    document_id=document_id.format(), page=page, refresh=refresh
                )
            )
        if isinstance(document_id, MevzuatId):
            return await _call(
                lambda: runtime.legislation.get_document(
                    legislation_id=document_id.format(), page=page, refresh=refresh
                )
            )
        assert isinstance(document_id, AnayasaId)
        return await _call(
            lambda: runtime.constitutional.get_document(
                id=document_id.format(), page=page, refresh=refresh
            )
        )

    async def get_legislation_article(
        ctx: Context,
        legislation_id: str,
        article_number: str,
        page: int = 1,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Fetch one article from a V1 legislation ID."""
        runtime = cast(ToolRuntime, _runtime_for(ctx))
        return await _call(
            lambda: runtime.legislation.get_article(
                legislation_id=legislation_id,
                article_number=article_number,
                page=page,
                refresh=refresh,
            )
        )

    async def search_in_legislation(
        ctx: Context,
        legislation_id: str,
        query: str,
        page: int = 1,
        page_size: int = 25,
        include_snippet: bool = True,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Search an opaque legislation ID with the local Boolean dialect."""
        runtime = cast(ToolRuntime, _runtime_for(ctx))
        return await _call(
            lambda: runtime.legislation.search_in_legislation(
                legislation_id=legislation_id,
                query=query,
                page=page,
                page_size=page_size,
                include_snippet=include_snippet,
                refresh=refresh,
            )
        )

    async def get_legislation_outline(
        ctx: Context,
        legislation_id: str,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Fetch the normalized article tree for an opaque legislation ID."""
        runtime = cast(ToolRuntime, _runtime_for(ctx))
        return await _call(
            lambda: runtime.legislation.get_outline(
                legislation_id=legislation_id, refresh=refresh
            )
        )

    _register(mcp, "turk_hukuku_sorularinda_once_bu_araci_cagir", start_legal_research)
    _register(mcp, "karar_ara", search_decisions)
    _register(mcp, "mevzuat_ara", search_legislation)
    _register(mcp, "anayasa_karari_ara", search_constitutional)
    _register(mcp, "belge_getir", get_document)
    _register(mcp, "mevzuat_madde_getir", get_legislation_article)
    _register(mcp, "mevzuat_icinde_ara", search_in_legislation)
    _register(mcp, "mevzuat_madde_agaci_getir", get_legislation_outline)
