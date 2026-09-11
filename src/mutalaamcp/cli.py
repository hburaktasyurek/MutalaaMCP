"""Typer CLI. Diagnostics go to stderr and mutations honor the serve lock."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn, Protocol, runtime_checkable

import httpx
import typer
from pydantic import ValidationError
from typer._click import Command, Context, HelpFormatter, echo
from typer._click._compat import get_text_stderr
from typer._click.exceptions import (
    MissingParameter,
    NoArgsIsHelpError,
    NoSuchOption,
    UsageError,
)
from typer.core import TyperCommand, TyperGroup

from mutalaamcp.domain.errors import ErrorCode
from mutalaamcp.ocr import OcrError
from mutalaamcp.runtime import AlreadyRunning, FileLock, mutation_lock
from mutalaamcp.server import V1_TOOL_NAMES, ToolRegistrationError, create_server
from mutalaamcp.settings import Settings

_CONSOLE_SCRIPT = "mutalaamcp"
_TERMS_ACTIVATION_RETRIES = 1
_OFFICIAL_SMOKE_TOOL = "mevzuat_ara"
_OFFICIAL_SMOKE_ARGUMENTS = {"number": "4721", "page_size": 1}
_OFFICIAL_SMOKE_SOURCE = "bedesten"
_UPSTREAM_SMOKE_ERROR_CODES = frozenset(
    {
        ErrorCode.UPSTREAM_RATE_LIMITED.value,
        ErrorCode.UPSTREAM_UNAVAILABLE.value,
    }
)
_TURKISH_HELP_OPTION = "Bu yardım iletisini göster ve çık."
_TURKISH_SUBCOMMAND_METAVAR = "KOMUT [BAĞIMSIZ_DEĞİŞKENLER]..."
_TURKISH_USAGE_PREFIX = "Kullanım: "

_CLICK_STRING_LITERAL = r"(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')"
_CLICK_UNKNOWN_COMMAND_MESSAGE = re.compile(
    rf"No such command (?P<command>{_CLICK_STRING_LITERAL})\."
    rf"(?: Did you mean (?P<suggestion>{_CLICK_STRING_LITERAL})\?)?"
)
_CLICK_UNEXPECTED_EXTRA_ARGUMENT_MESSAGE = re.compile(
    r"Got unexpected extra (?P<form>argument(?:\(s\)|s)?) "
    r"\((?P<arguments>.+)\)"
)


def _parse_click_string_literal(value: str) -> str | None:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return None
    return parsed if isinstance(parsed, str) else None


def _format_turkish_options(
    command: Command, ctx: Context, formatter: HelpFormatter
) -> None:
    arguments = []
    options = []
    for param in command.get_params(ctx):
        record = param.get_help_record(ctx)
        if record is None:
            continue
        option, help_text = record
        help_text = (
            help_text.replace("[required]", "[gerekli]")
            .replace("[default: ", "[varsayılan: ")
            .replace("Show this message and exit.", _TURKISH_HELP_OPTION)
        )
        if param.param_type_name == "argument":
            arguments.append((option, help_text))
        elif param.param_type_name == "option":
            options.append((option, help_text))
    if arguments:
        with formatter.section("Argümanlar"):
            formatter.write_dl(arguments)
    if options:
        with formatter.section("Seçenekler"):
            formatter.write_dl(options)


def _turkish_usage_error_message(error: UsageError) -> str:
    if isinstance(error, NoArgsIsHelpError):
        return "Çalıştırılacak bir komut belirtilmedi."
    if isinstance(error, NoSuchOption):
        message = f"Bilinmeyen seçenek: {error.option_name!r}."
        if error.possibilities:
            suggestions = ", ".join(repr(option) for option in error.possibilities)
            question = (
                "Şunu mu demek istediniz"
                if len(error.possibilities) == 1
                else "Bunlardan birini mi demek istediniz"
            )
            return f"{message} {question}: {suggestions}?"
        return message
    if isinstance(error, MissingParameter):
        param_hint = error.param_hint
        if param_hint is None and error.param is not None and error.ctx is not None:
            param_hint = error.param.get_error_hint(error.ctx)
        if isinstance(param_hint, list | tuple):
            rendered_hint = " / ".join(repr(hint) for hint in param_hint)
        else:
            rendered_hint = str(param_hint or "")
        param_type = error.param_type
        if param_type is None and error.param is not None:
            param_type = error.param.param_type_name
        kind = {
            "argument": "argüman",
            "option": "seçenek",
            "parameter": "parametre",
        }.get(param_type or "", "parametre")
        hint_suffix = f" {rendered_hint}" if rendered_hint else ""
        return f"Eksik {kind}{hint_suffix}."
    message = error.message.strip()
    command_match = _CLICK_UNKNOWN_COMMAND_MESSAGE.fullmatch(message)
    if command_match is not None:
        command_name = _parse_click_string_literal(command_match["command"])
        rendered_suggestion = command_match["suggestion"]
        suggestion = (
            _parse_click_string_literal(rendered_suggestion)
            if rendered_suggestion is not None
            else None
        )
        if command_name is not None:
            localized_message = f"Bilinmeyen komut: {command_name!r}."
            if rendered_suggestion is None:
                return localized_message
            if suggestion is not None:
                return f"{localized_message} Şunu mu demek istediniz: {suggestion!r}?"
    extra_argument_match = _CLICK_UNEXPECTED_EXTRA_ARGUMENT_MESSAGE.fullmatch(message)
    if extra_argument_match is not None:
        form = extra_argument_match["form"]
        kind = "argümanlar" if form == "arguments" else "argüman"
        return f"Beklenmeyen fazladan {kind}: {extra_argument_match['arguments']!r}."
    if message == "Missing command.":
        return "Çalıştırılacak bir komut belirtilmedi."
    return "Komut satırı kullanımı geçersiz."


class _TurkishUsageError(UsageError):
    def __init__(self, error: UsageError) -> None:
        super().__init__(error.message, ctx=error.ctx)
        self._error = error
        self.exit_code = error.exit_code

    def show(self, file: Any | None = None) -> None:
        error = self._error
        if file is None:
            file = get_text_stderr()
        if error.ctx is not None:
            hint = ""
            if error.ctx.command.get_help_option(error.ctx) is not None:
                help_names = error.ctx.command.get_help_option_names(error.ctx)
                if help_names:
                    hint = (
                        f"Yardım için '{error.ctx.command_path} "
                        f"{max(help_names, key=len)}' çalıştırın."
                    )
            echo(
                f"{error.ctx.get_usage()}\n{hint}",
                file=file,
                color=error.ctx.color,
            )
        echo(
            f"Hata: {_turkish_usage_error_message(error)}",
            file=file,
            color=error.ctx.color if error.ctx is not None else None,
        )


class _TurkishCliMixin:
    def _command(self) -> Command:
        if not isinstance(self, Command):
            raise TypeError("_TurkishCliMixin requires a Click command")
        return self

    def format_usage(self, ctx: Context, formatter: HelpFormatter) -> None:
        command = self._command()
        pieces = " ".join(command.collect_usage_pieces(ctx)).replace(
            "[OPTIONS]", "[SEÇENEKLER]"
        )
        formatter.write_usage(ctx.command_path, pieces, prefix=_TURKISH_USAGE_PREFIX)

    def format_options(self, ctx: Context, formatter: HelpFormatter) -> None:
        _format_turkish_options(self._command(), ctx, formatter)

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: Context | None = None,
        **extra: Any,
    ) -> Context:
        try:
            return Command.make_context(
                self._command(), info_name, args, parent=parent, **extra
            )
        except _TurkishUsageError:
            raise
        except UsageError as error:
            raise _TurkishUsageError(error) from None


class _TurkishTyperCommand(_TurkishCliMixin, TyperCommand):
    pass


class _TurkishTyperGroup(_TurkishCliMixin, TyperGroup):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("subcommand_metavar") is None:
            kwargs["subcommand_metavar"] = _TURKISH_SUBCOMMAND_METAVAR
        super().__init__(*args, **kwargs)

    def format_options(self, ctx: Context, formatter: HelpFormatter) -> None:
        _format_turkish_options(self, ctx, formatter)
        self.format_commands(ctx, formatter)

    def format_commands(self, ctx: Context, formatter: HelpFormatter) -> None:
        commands = []
        for subcommand in self.list_commands(ctx):
            command = self.get_command(ctx, subcommand)
            if command is not None and not command.hidden:
                commands.append((subcommand, command))
        if not commands:
            return
        limit = formatter.width - 6 - max(len(name) for name, _ in commands)
        rows = [(name, command.get_short_help_str(limit)) for name, command in commands]
        with formatter.section("Komutlar"):
            formatter.write_dl(rows)

    def invoke(self, ctx: Context) -> Any:
        try:
            return super().invoke(ctx)
        except _TurkishUsageError:
            raise
        except UsageError as error:
            raise _TurkishUsageError(error) from None


class _TurkishTyper(typer.Typer):
    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("cls", _TurkishTyperGroup)
        kwargs.setdefault("rich_markup_mode", None)
        super().__init__(**kwargs)

    def command(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("cls", _TurkishTyperCommand)
        return super().command(*args, **kwargs)


app = _TurkishTyper(
    name="mutalaamcp",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)
auth_app = _TurkishTyper(no_args_is_help=True, pretty_exceptions_enable=False)
cache_app = _TurkishTyper(no_args_is_help=True, pretty_exceptions_enable=False)
ocr_app = _TurkishTyper(no_args_is_help=True, pretty_exceptions_enable=False)
app.add_typer(auth_app, name="auth")
app.add_typer(cache_app, name="cache")
app.add_typer(ocr_app, name="ocr")


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _fail(code: ErrorCode, message: str, exit_code: int = 1) -> NoReturn:
    _err(f"{code}: {message}")
    raise typer.Exit(exit_code)


def _validation_field_label(location: object, *, settings: bool) -> str:
    if not isinstance(location, tuple) or not location:
        return "yapılandırma"
    first, *rest = location
    if not isinstance(first, str):
        return "yapılandırma"
    if settings:
        label = f"MUTALAAMCP_{first.upper()}"
    else:
        label = first
    for part in rest:
        label += f"[{part}]" if isinstance(part, int) else f".{part}"
    return label


def _validation_error_detail(error: Mapping[str, object]) -> str:
    error_type = error.get("type")
    context = error.get("ctx")
    if error_type == "missing":
        return "değer belirtilmelidir."
    if error_type in {"float_parsing", "float_type"}:
        return "geçerli bir sayı olmalıdır."
    if error_type in {"int_parsing", "int_type"}:
        return "geçerli bir tam sayı olmalıdır."
    if error_type == "string_type":
        return "metin olmalıdır."
    if error_type == "path_type":
        return "geçerli bir dosya yolu olmalıdır."
    if error_type == "greater_than" and isinstance(context, Mapping):
        return f"değer {context.get('gt')} değerinden büyük olmalıdır."
    if error_type == "greater_than_equal" and isinstance(context, Mapping):
        return f"değer en az {context.get('ge')} olmalıdır."
    if error_type == "less_than" and isinstance(context, Mapping):
        return f"değer {context.get('lt')} değerinden küçük olmalıdır."
    if error_type == "less_than_equal" and isinstance(context, Mapping):
        return f"değer en çok {context.get('le')} olmalıdır."
    if error_type == "string_too_short" and isinstance(context, Mapping):
        return f"en az {context.get('min_length')} karakter olmalıdır."
    if error_type == "string_too_long" and isinstance(context, Mapping):
        return f"en çok {context.get('max_length')} karakter olmalıdır."
    message = str(error.get("msg") or "").removeprefix("Value error, ").strip()
    if message:
        return message if message.endswith(".") else f"{message}."
    return "geçersiz değer."


def _format_validation_error(exc: ValidationError, *, settings: bool = True) -> str:
    lines = ["Yapılandırma doğrulanamadı:" if settings else "Doğrulama başarısız oldu:"]
    for error in exc.errors():
        lines.append(
            "- "
            f"{_validation_field_label(error.get('loc'), settings=settings)}: "
            f"{_validation_error_detail(error)}"
        )
    return "\n".join(lines)


def _technical_error_detail(exc: BaseException) -> str:
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _fail_cache_command(action: str, exc: BaseException) -> NoReturn:
    _fail(ErrorCode.NOT_CONFIGURED, f"{action}: {_technical_error_detail(exc)}")


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        _fail(ErrorCode.NOT_CONFIGURED, _format_validation_error(exc))


def _http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.http_timeout_seconds,
            connect=settings.http_connect_timeout_seconds,
        )
    )


def _auth_session(settings: Settings, http_client: httpx.AsyncClient):
    from mutalaamcp.auth import AuthSession

    return AuthSession(
        http_client,
        auth_base_url=settings.auth_base_url,
        allowed_terms_base_url=settings.terms_base_url,
        data_dir=settings.data_dir,
    )


async def _present_authorization(authorization) -> None:
    _err(
        f"{authorization.verification_uri} adresini açın ve {authorization.user_code} kodunu girin."
    )
    _err(authorization.verification_uri_complete)
    try:
        webbrowser.open(authorization.verification_uri_complete)
    except Exception:  # noqa: BLE001 -- browser launch must not abort login
        return


def _handle_auth_failure(exc: BaseException) -> NoReturn:
    from mutalaamcp.auth import AuthenticationRequired
    from mutalaamcp.auth.activation import (
        ActivationError,
        ActivationProtocolError,
        activation_error_message,
    )
    from mutalaamcp.auth.device_flow import DeviceFlowError, SessionExpired
    from mutalaamcp.auth.state import AuthStateError

    if isinstance(exc, typer.Exit):
        raise exc
    if isinstance(exc, AlreadyRunning):
        _fail(ErrorCode.ALREADY_RUNNING, str(exc))
    if isinstance(exc, ToolRegistrationError):
        _fail(ErrorCode.NOT_CONFIGURED, str(exc))
    if isinstance(exc, AuthenticationRequired):
        _fail(ErrorCode.AUTHENTICATION_REQUIRED, "Kimlik doğrulaması gerekli.")
    if isinstance(exc, SessionExpired):
        remote_detail = (
            exc.description if exc.description and exc.description.strip() else None
        )
        _fail(
            ErrorCode.SESSION_EXPIRED,
            activation_error_message(ErrorCode.SESSION_EXPIRED, remote_detail),
        )
    if isinstance(exc, ActivationError):
        if exc.terms_url:
            _err(exc.terms_url)
            try:
                webbrowser.open(exc.terms_url)
            except Exception:  # noqa: BLE001 -- terms browser launch is best effort
                _err("Koşullar adresi tarayıcıda açılamadı.")
        _fail(exc.code, activation_error_message(exc.code, exc.remote_detail))
    if isinstance(exc, (ActivationProtocolError, DeviceFlowError, httpx.HTTPError)):
        _fail(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "Kimlik doğrulama hizmetine şu anda erişilemiyor. "
            f"Teknik ayrıntı: {_technical_error_detail(exc)}",
        )
    if isinstance(exc, AuthStateError):
        _err(
            "Yerel kimlik doğrulama durumu okunamadı. "
            f"Teknik ayrıntı: {_technical_error_detail(exc)}"
        )
        raise typer.Exit(1)
    if isinstance(exc, ValidationError):
        _fail(ErrorCode.NOT_CONFIGURED, _format_validation_error(exc, settings=False))
    raise exc


def _is_console_launcher(path: Path) -> bool:
    if path.suffix.lower() in {".py", ".pyc"}:
        return False
    if path.stem.lower() != _CONSOLE_SCRIPT:
        return False
    try:
        if not path.is_file():
            return False
    except OSError:
        return False
    return os.access(path, os.X_OK)


def _launcher_candidates() -> list[Path]:
    names = (
        (f"{_CONSOLE_SCRIPT}.exe", _CONSOLE_SCRIPT)
        if os.name == "nt"
        else (_CONSOLE_SCRIPT,)
    )
    candidates: list[Path] = []
    # A venv's Python may be a symlink to the base interpreter. Keep its bin
    # directory so absolute CLI invocations work without an activated PATH.
    bindir = Path(sys.executable).absolute().parent
    candidates.extend(bindir / name for name in names)
    found = shutil.which(_CONSOLE_SCRIPT)
    if found:
        candidates.append(Path(found))
    return candidates


def _launcher_path() -> str:
    seen: set[Path] = set()
    for raw in _launcher_candidates():
        try:
            path = raw.expanduser().resolve()
        except OSError:
            continue
        if path in seen:
            continue
        seen.add(path)
        if path.is_absolute() and _is_console_launcher(path):
            return str(path)
    _fail(
        ErrorCode.NOT_CONFIGURED,
        "Mutlak bir mutalaamcp konsol başlatıcısı bulunamadı.",
    )


def _stable_launcher(settings: Settings, current_launcher: str) -> str:
    """Create or reuse the launcher that desktop clients will invoke."""
    from mutalaamcp.launcher import LauncherError, ensure_stable_launcher

    try:
        stable_launcher = ensure_stable_launcher(
            settings.data_dir, Path(current_launcher)
        )
    except LauncherError as exc:
        _fail(ErrorCode.NOT_CONFIGURED, str(exc))
    path = Path(stable_launcher).expanduser()
    if not path.is_absolute():
        _fail(ErrorCode.NOT_CONFIGURED, "Kararlı başlatıcının yolu mutlak olmalıdır.")
    return str(path)


def _ensure_active_launcher_state(settings: Settings, current_launcher: str) -> None:
    """Seed version selection on first setup without replacing an active version."""
    from mutalaamcp.update import UpdateError, read_active_version, write_active_version

    if settings.active_version_state_path.exists():
        return
    try:
        active = read_active_version(
            settings.active_version_state_path,
            fallback_launcher=Path(current_launcher),
        )
        write_active_version(settings.active_version_state_path, active)
    except (OSError, UpdateError) as exc:
        _fail(ErrorCode.NOT_CONFIGURED, str(exc))


def _prepare_stable_launcher(settings: Settings) -> str:
    """Prepare and persist the local launcher used by MCP clients."""

    current_launcher = _launcher_path()
    launcher = _stable_launcher(settings, current_launcher)
    _ensure_active_launcher_state(settings, current_launcher)
    return launcher


def _copy_to_clipboard(config: str) -> None:
    """Best-effort copy of the already-rendered, secret-free client configuration."""

    if sys.platform == "darwin":
        command = ["pbcopy"]
    elif os.name == "nt":
        command = ["clip"]
    else:
        _err(
            "Kurulum uyarısı: Bu platformda panoya kopyalama kullanılamıyor; "
            "yazdırılan istemci yapılandırmasını elle kopyalayın."
        )
        return
    try:
        subprocess.run(command, input=config, text=True, check=True, shell=False)
    except Exception:  # noqa: BLE001 -- clipboard copy must never fail setup
        _err(
            "Kurulum uyarısı: İstemci yapılandırması panoya kopyalanamadı; "
            "yazdırılan yapılandırmayı elle kopyalayın."
        )


def _print_stdio_config(client: str, launcher: str) -> None:
    """Print a selected client template without writing any application config."""

    from mutalaamcp.client_templates import render_client_config

    config = render_client_config(client, launcher)
    print(config, end="")
    _copy_to_clipboard(config)


async def _run_login(settings: Settings) -> None:
    from mutalaamcp.auth.activation import ActivationError

    async with _http_client(settings) as client:
        session = _auth_session(settings, client)
        try:
            await session.login(_present_authorization)
        except ActivationError as error:
            if error.code is not ErrorCode.TERMS_REQUIRED:
                raise
            await _retry_terms_activation(settings, session, error)


async def _run_logout(settings: Settings) -> None:
    async with _http_client(settings) as client:
        result = await _auth_session(settings, client).logout()

    if result.cleanup_error is not None:
        _err(f"Oturum kapatma temizleme uyarısı: {result.cleanup_error}")


def _allowlisted_terms_url(settings: Settings, terms_url: str | None) -> str:
    """Revalidate an activation terms URL before passing it to the browser."""
    from mutalaamcp.auth.activation import (
        ActivationProtocolError,
        _allowed_terms_url,
        _parse_allowed_terms_base,
    )

    if terms_url is None:
        _fail(ErrorCode.NOT_CONFIGURED, "terms_required bir koşullar adresi içermedi.")
    try:
        return _allowed_terms_url(
            terms_url, _parse_allowed_terms_base(settings.terms_base_url or "")
        )
    except (ActivationProtocolError, ValueError) as exc:
        _fail(ErrorCode.NOT_CONFIGURED, str(exc))


def _confirm_terms_accepted() -> bool:
    """Require an affirmative acknowledgement before activation is retried."""
    try:
        return typer.confirm(
            "Koşulları tarayıcıda kabul ettim; etkinleştirme yeniden denensin mi?",
            default=False,
            abort=False,
        )
    except (EOFError, typer.Abort):
        return False


async def _retry_terms_activation(settings: Settings, session: Any, error: Any) -> None:
    """Retry activation after an authenticated user accepts allowed terms."""
    from mutalaamcp.auth.activation import ActivationError

    for _attempt in range(_TERMS_ACTIVATION_RETRIES):
        terms_url = _allowlisted_terms_url(settings, error.terms_url)
        _err(f"Girişin tamamlanabilmesi için koşullar kabul edilmelidir: {terms_url}")
        try:
            webbrowser.open(terms_url)
        except Exception:  # noqa: BLE001 -- browser launch must not block login
            _err("Koşullar adresi tarayıcıda açılamadı.")
        if not _confirm_terms_accepted():
            _fail(
                ErrorCode.TERMS_REQUIRED,
                "Girişi tamamlamak için koşulların açıkça kabul edilmesi gerekli.",
            )
        try:
            await session.activate()
            return
        except ActivationError as retry_error:
            if retry_error.code is not ErrorCode.TERMS_REQUIRED:
                raise
            error = retry_error
    raise error


@runtime_checkable
class _JsonDumpable(Protocol):
    def model_dump(self, *, mode: str) -> object: ...


def _structured_payload(result: object) -> object:
    payload = getattr(result, "structured_content", result)
    if isinstance(payload, _JsonDumpable):
        model_dump = payload.model_dump
        return model_dump(mode="json") if callable(model_dump) else payload
    return payload


def _tool_error(payload: object) -> tuple[str | None, str | None] | None:
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None
    code = error.get("code")
    details = error.get("details")
    upstream = details.get("upstream") if isinstance(details, Mapping) else None
    return (
        code if isinstance(code, str) else None,
        upstream if isinstance(upstream, str) else None,
    )


async def _health_check(launcher: str) -> None:
    """Exercise the stable stdio launcher and distinguish upstream smoke outages."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=launcher,
        args=["serve"],
        env=dict(os.environ),
        keep_alive=False,
    )
    try:
        async with Client(transport) as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools}
            if names != V1_TOOL_NAMES:
                expected = ", ".join(sorted(V1_TOOL_NAMES))
                found = ", ".join(sorted(names)) if names else "yok"
                _fail(
                    ErrorCode.NOT_CONFIGURED,
                    "MCP sunucusu tam olarak şu V1 araçlarını sunmalıdır: "
                    f"{expected}; bulunan: {found}",
                )
            local_payload = _structured_payload(
                await client.call_tool("belge_getir", {"id": "invalid"})
            )
            smoke_payload = _structured_payload(
                await client.call_tool(_OFFICIAL_SMOKE_TOOL, _OFFICIAL_SMOKE_ARGUMENTS)
            )
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 -- external launcher boundary
        _fail(
            ErrorCode.NOT_CONFIGURED,
            f"Kararlı başlatıcının MCP sağlık denetimi başarısız oldu ({type(exc).__name__}).",
        )

    local_error = _tool_error(local_payload)
    if local_error is None or local_error[0] != ErrorCode.INVALID_PARAMS.value:
        _fail(
            ErrorCode.NOT_CONFIGURED,
            "Yerel MCP aracı sağlık denetiminde geçersiz belge kimliğini reddetmedi.",
        )

    smoke_error = _tool_error(smoke_payload)
    if smoke_error is None:
        return
    error_code, source = smoke_error
    if source == _OFFICIAL_SMOKE_SOURCE and error_code in _UPSTREAM_SMOKE_ERROR_CODES:
        _err(
            "Bağlantı uyarısı: "
            f"resmî kaynak={source} hata_kodu={error_code}; "
            "yerel başlatıcı sağlıklı, ancak kaynak geçici olarak kullanılamıyor."
        )
        return
    _fail(
        ErrorCode.NOT_CONFIGURED,
        "Resmî kaynak deneme çağrısı başarısız oldu"
        f" (kaynak={source or 'bilinmiyor'}, hata_kodu={error_code or 'bilinmiyor'}).",
    )


def _confirm_ocr_install() -> bool:
    """Ask for an affirmative OCR download decision; EOF is the default no."""

    try:
        return typer.confirm(
            "İsteğe bağlı yerel OCR modeli şimdi yüklensin mi?",
            default=False,
            abort=False,
        )
    except (EOFError, typer.Abort):
        return False


@app.command()
def serve() -> None:
    """Yerel stdio MCP sunucusunu çalıştır."""

    settings = load_settings()
    try:
        with FileLock(settings.serve_lock_path):
            create_server().run(transport="stdio")
    except AlreadyRunning as exc:
        _fail(ErrorCode.ALREADY_RUNNING, str(exc))
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 -- CLI boundary converts errors to exits
        _handle_auth_failure(exc)


@app.command()
def setup(
    client: str = typer.Option(
        "claude-desktop",
        "--client",
        help="Yazdırılacak stdio yapılandırması için desteklenen masaüstü istemcisi kısaltması.",
    ),
    transport: str = typer.Option(
        "stdio", "--transport", help="stdio veya uygulama içi OAuth için http (Codex)."
    ),
) -> None:
    """Yerel başlatıcıyı hazırla ve seçilen istemci yapılandırmasını yazdır."""

    settings = load_settings()
    try:
        if transport == "http":
            if client != "codex":
                raise ValueError("HTTP giriş akışı için --client codex seçin.")
            from mutalaamcp.native_service import install_native_service

            install_native_service(settings, _launcher_path())
            config = f'[mcp_servers.mutalaamcp]\nurl = "http://127.0.0.1:{settings.http_port}/mcp"\n'
            print(config, end="")
            _copy_to_clipboard(config)
            _err("Uygulamada MutalaaMCP için Kimliği Doğrula seçin.")
            return
        if transport != "stdio":
            raise ValueError("--transport stdio veya http olmalıdır.")
        launcher = _prepare_stable_launcher(settings)
        _print_stdio_config(client, launcher)
    except AlreadyRunning as exc:
        _fail(ErrorCode.ALREADY_RUNNING, str(exc))
    except ValueError as exc:
        _fail(ErrorCode.INVALID_PARAMS, str(exc))
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        _fail(ErrorCode.NOT_CONFIGURED, f"Yerel kurulum tamamlanamadı: {exc}")
    except Exception as exc:  # noqa: BLE001 -- CLI boundary converts errors to exits
        _handle_auth_failure(exc)


@app.command("serve-http")
def serve_http() -> None:
    """Yerel HTTP ve uygulama içi OAuth sunucusunu çalıştır."""
    from mutalaamcp.native_service import run_native_server

    settings = load_settings()
    try:
        with FileLock(settings.serve_lock_path):
            run_native_server(settings)
    except AlreadyRunning as exc:
        _fail(ErrorCode.ALREADY_RUNNING, str(exc))


@app.command("service")
def native_service(
    action: str = typer.Argument("status", help="start, stop veya status"),
) -> None:
    """Uygulama içi giriş için yerel HTTP hizmetini yönet."""
    from mutalaamcp.native_service import (
        install_native_service,
        native_service_ready,
        stop_native_service,
    )

    settings = load_settings()
    try:
        if action == "start":
            install_native_service(settings, _launcher_path())
        elif action == "stop":
            stop_native_service()
        elif action != "status":
            _fail(ErrorCode.INVALID_PARAMS, "service start, stop veya status kullanın.")
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        _fail(ErrorCode.NOT_CONFIGURED, f"Yerel hizmet işlemi tamamlanamadı: {exc}")
    print("status=running" if native_service_ready(settings) else "status=stopped")


@auth_app.command("login")
def auth_login() -> None:
    """Mütalaa cihaz oturumunu aç ve yerel MCP bağlantısını doğrula."""

    settings = load_settings()
    try:
        # Reject a running client before opening the browser or replacing its
        # credentials. Release the lock before the health-check child starts.
        with FileLock(settings.serve_lock_path):
            asyncio.run(_run_login(settings))
            launcher = _prepare_stable_launcher(settings)
        asyncio.run(_health_check(launcher))
    except AlreadyRunning as exc:
        _fail(
            ErrorCode.ALREADY_RUNNING,
            f"{exc} Giriş yapmadan önce MutalaaMCP kullanan istemciyi kapatın; "
            "ardından mutalaamcp auth login komutunu yeniden çalıştırın.",
        )
    except Exception as exc:  # noqa: BLE001 -- CLI boundary converts errors to exits
        _handle_auth_failure(exc)


@auth_app.command("logout")
def auth_logout() -> None:
    """Yerel kimlik bilgilerini ve etkinleştirme durumunu temizle."""

    settings = load_settings()
    try:
        asyncio.run(_run_logout(settings))
    except Exception as exc:  # noqa: BLE001 -- CLI boundary converts errors to exits
        _handle_auth_failure(exc)


@auth_app.command("status")
def auth_status() -> None:
    """Yerel oturum ve etkinleştirme durumunu göster."""

    from mutalaamcp.auth.state import AuthState, AuthStateError

    settings = load_settings()
    try:
        state = AuthState(settings.data_dir).read()
    except AuthStateError as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    if state is None:
        print("status=none")
        return
    status = state.status
    plan = state.plan
    checked_at = state.checked_at
    print(
        json.dumps(
            {
                "status": status,
                **({"plan": plan} if plan is not None else {}),
                **({"checked_at": checked_at} if checked_at is not None else {}),
            },
            separators=(",", ":"),
        )
    )


@cache_app.command("clear")
def cache_clear() -> None:
    """Yerel belge önbelleğini sil."""

    from mutalaamcp.cache.store import CacheStore

    settings = load_settings()
    db = settings.cache_db_path
    try:
        with mutation_lock(settings.serve_lock_path):
            if not db.is_file():
                return
            with CacheStore(db, max_bytes=settings.cache_max_bytes) as store:
                store.clear()
    except AlreadyRunning as exc:
        _fail(ErrorCode.ALREADY_RUNNING, str(exc))
    except Exception as exc:  # noqa: BLE001 -- CLI boundary reports cache failures
        _fail_cache_command("Yerel önbellek silinemedi", exc)


async def _refresh_cached_document(
    settings: Settings, document_id: str
) -> dict[str, object]:
    """Force a canonical document refresh through its owning service boundary."""

    from mutalaamcp.domain.ids import (
        AnayasaId,
        BedestenId,
        MevzuatId,
        parse_document_id,
    )
    from mutalaamcp.tools import create_runtime

    parsed = parse_document_id(document_id)
    runtime = await create_runtime(settings)
    try:
        await runtime.auth.ensure_authorized()
        canonical_id = parsed.format()
        if isinstance(parsed, BedestenId):
            return await runtime.decisions.get_document(canonical_id, refresh=True)
        if isinstance(parsed, MevzuatId):
            return await runtime.legislation.get_document(canonical_id, refresh=True)
        if isinstance(parsed, AnayasaId):
            return await runtime.constitutional.get_document(
                id=canonical_id, refresh=True
            )
        raise AssertionError(
            f"Desteklenmeyen ayrıştırılmış belge kimliği: {type(parsed)!r}"
        )
    finally:
        await runtime.aclose()


def _raise_refresh_error(result: dict[str, object]) -> None:
    if result.get("ok") is not False:
        return
    error = result.get("error")
    if not isinstance(error, dict):
        _fail(
            ErrorCode.NOT_CONFIGURED,
            "Önbellek yenileme geçersiz bir hata zarfı döndürdü.",
        )
    raw_code: object = error.get("code")
    message: object = error.get("message")
    if not isinstance(raw_code, str):
        _fail(
            ErrorCode.NOT_CONFIGURED,
            "Önbellek yenileme geçersiz bir hata zarfı döndürdü.",
        )
    try:
        code = ErrorCode(raw_code)
    except ValueError:
        code = ErrorCode.NOT_CONFIGURED
    detail = (
        message.strip()
        if isinstance(message, str) and message.strip()
        else "Hata iletisinin ayrıntısı sağlanmadı."
    )
    _fail(code, f"Önbellek yenilenemedi: {detail}")


@cache_app.command("refresh")
def cache_refresh(document_id: str) -> None:
    """Ad alanlı kimliğiyle önbellekteki bir belgeyi yeniden doğrula."""

    from mutalaamcp.auth.activation import ActivationError, ActivationProtocolError
    from mutalaamcp.auth.device_flow import DeviceFlowError
    from mutalaamcp.auth.state import AuthStateError
    from mutalaamcp.cache import CacheError
    from mutalaamcp.conversion.documents import DocumentConversionError
    from mutalaamcp.domain.ids import InvalidDocumentId, parse_document_id
    from mutalaamcp.net import SafeHttpError
    from mutalaamcp.providers.anayasa.client import AnayasaProtocolError
    from mutalaamcp.providers.bedesten.legislation import LegislationProviderError

    settings = load_settings()
    try:
        canonical_id = parse_document_id(document_id).format()
        with mutation_lock(settings.serve_lock_path):
            result = asyncio.run(_refresh_cached_document(settings, canonical_id))
        _raise_refresh_error(result)
    except typer.Exit:
        raise
    except AlreadyRunning as exc:
        _fail(ErrorCode.ALREADY_RUNNING, str(exc))
    except InvalidDocumentId as exc:
        _fail(
            ErrorCode.INVALID_PARAMS,
            f"Belge kimliği geçersiz: {_technical_error_detail(exc)}",
        )
    except (
        ActivationError,
        ActivationProtocolError,
        AuthStateError,
        DeviceFlowError,
        httpx.HTTPError,
        ValidationError,
    ) as exc:
        _handle_auth_failure(exc)
    except (
        AnayasaProtocolError,
        CacheError,
        DocumentConversionError,
        LegislationProviderError,
        OcrError,
        OSError,
        RuntimeError,
        SafeHttpError,
        ValueError,
    ) as exc:
        _fail_cache_command("Önbellek yenilenemedi", exc)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


@ocr_app.command("install")
def ocr_install() -> None:
    """Açık onaydan sonra isteğe bağlı yerel OCR modelini yükle."""

    from mutalaamcp.ocr import OcrError, install_ocr

    settings = load_settings()
    try:
        with mutation_lock(settings.serve_lock_path):
            if not _confirm_ocr_install():
                _err("Açık onay verilmediği için OCR yüklenmedi.")
                return
            status = install_ocr(settings, consent=True)
    except AlreadyRunning as exc:
        _fail(ErrorCode.ALREADY_RUNNING, str(exc))
    except OcrError as exc:
        _fail(ErrorCode.NOT_CONFIGURED, str(exc))
    print(json.dumps(status.as_dict(), separators=(",", ":")))


@ocr_app.command("remove")
def ocr_remove() -> None:
    """Etkin isteğe bağlı yerel OCR modelini devre dışı bırak ve kaldır."""

    from mutalaamcp.ocr import OcrError, remove_ocr

    settings = load_settings()
    try:
        with mutation_lock(settings.serve_lock_path):
            status = remove_ocr(settings)
    except AlreadyRunning as exc:
        _fail(ErrorCode.ALREADY_RUNNING, str(exc))
    except OcrError as exc:
        _fail(ErrorCode.NOT_CONFIGURED, str(exc))
    print(json.dumps(status.as_dict(), separators=(",", ":")))


@ocr_app.command("status")
def ocr_status() -> None:
    """Doğrulanmış yerel OCR kurulumunu ve çalışma zamanı durumunu göster."""

    from mutalaamcp.ocr import inspect_ocr

    status = inspect_ocr(load_settings())
    print(json.dumps(status.as_dict(), separators=(",", ":")))


@app.command()
def update(
    version: str = typer.Option(..., "--version", help="Yüklenecek tam paket sürümü."),
) -> None:
    """Atomik etkinleştirmeden önce yalıtılmış adayı yükle ve sağlık denetiminden geçir."""

    from mutalaamcp.update import UpdateError, update_to_version

    settings = load_settings()
    try:
        with mutation_lock(settings.serve_lock_path):
            result = update_to_version(
                settings,
                version,
                current_launcher=_launcher_path(),
            )
    except AlreadyRunning as exc:
        _fail(ErrorCode.ALREADY_RUNNING, str(exc))
    except UpdateError as exc:
        _fail(ErrorCode.NOT_CONFIGURED, str(exc))
    print(
        json.dumps(
            {
                "previous_version": result.previous.version,
                "active_version": result.active.version,
            },
            separators=(",", ":"),
        )
    )


def main() -> None:
    # Redirected Windows output can default to cp1252, which cannot represent
    # Turkish help/error messages. MCP output also requires UTF-8.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    app()
