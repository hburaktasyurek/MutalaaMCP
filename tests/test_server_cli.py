"""Behavioral server/CLI foundation tests. No live network, keyring, browser, or user dirs."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Self

import pytest
import typer
from typer.testing import CliRunner

from mutalaamcp.auth import AuthenticationRequired, LogoutResult
from mutalaamcp.auth.activation import ActivationError, activation_error_message
from mutalaamcp.auth.device_flow import SessionExpired
from mutalaamcp.cli import _launcher_path, _run_login, app
from mutalaamcp.domain.errors import ErrorCode
from mutalaamcp.runtime import AlreadyRunning, FileLock, serve_lock_held
from mutalaamcp.server import V1_TOOL_NAMES, ToolRegistrationError, create_server
from mutalaamcp.settings import Settings

ACCESS_TOKEN = "at_cli_must_not_leak_9f3c7e21"
REFRESH_TOKEN = "rt_cli_must_not_leak_1a2b4c"
SECRETS = (ACCESS_TOKEN, REFRESH_TOKEN)
EXPECTED_PUBLIC_TOOL_NAMES = frozenset(
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
LEGACY_PUBLIC_TOOL_NAMES = frozenset(
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


try:
    runner = CliRunner(mix_stderr=False)
except TypeError:
    runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("MUTALAAMCP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MUTALAAMCP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MUTALAAMCP_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("MUTALAAMCP_AUTH_BASE_URL", "https://mutalaa.test")
    monkeypatch.setenv("MUTALAAMCP_TERMS_BASE_URL", "https://mutalaa.test/kosullar")
    monkeypatch.setattr("mutalaamcp.cli.webbrowser.open", lambda *_a, **_k: False)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    return tmp_path


def _cli_text(result: object) -> str:
    chunks = [getattr(result, "output", None) or ""]
    stdout = getattr(result, "stdout", None)
    if stdout:
        chunks.append(stdout)
    try:
        stderr = result.stderr
    except (AttributeError, ValueError):
        stderr = ""
    if stderr:
        chunks.append(stderr)
    return "\n".join(chunks)


def assert_no_tokens(result: object) -> None:
    blob = _cli_text(result)
    for secret in SECRETS:
        assert secret not in blob


def assert_no_stdio_config(result: object) -> None:
    blob = _cli_text(result)
    assert '"env": {}' not in blob
    assert '"args":' not in blob


def test_root_help_is_turkish_first_and_keeps_command_identifiers() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    output = _cli_text(result)
    assert "Kullanım:" in output
    assert "Seçenekler:" in output
    assert "Komutlar:" in output
    assert "Bu yardım iletisini göster ve çık." in output
    assert "Usage:" not in output
    assert "Options:" not in output
    assert "Commands:" not in output
    for command in ("serve", "setup", "update", "auth", "cache", "ocr"):
        assert command in output


@pytest.mark.parametrize(
    ("subgroup", "commands"),
    [
        ("auth", ("login", "logout", "status")),
        ("cache", ("clear", "refresh")),
        ("ocr", ("install", "remove", "status")),
    ],
)
def test_subgroup_help_is_turkish_first(
    subgroup: str, commands: tuple[str, ...]
) -> None:
    result = runner.invoke(app, [subgroup, "--help"])

    assert result.exit_code == 0
    output = _cli_text(result)
    assert "Kullanım:" in output
    assert "Seçenekler:" in output
    assert "Komutlar:" in output
    assert "Bu yardım iletisini göster ve çık." in output
    assert "Usage:" not in output
    assert "Options:" not in output
    assert "Commands:" not in output
    for command in commands:
        assert command in output


@pytest.mark.parametrize(
    ("args", "expected_error"),
    [
        ([], "Çalıştırılacak bir komut belirtilmedi."),
        (["--bilinmeyen"], "Bilinmeyen seçenek: '--bilinmeyen'."),
        (["bilinmeyen"], "Bilinmeyen komut: 'bilinmeyen'."),
        (["cache", "refresh"], "Eksik argüman"),
        (
            ["cache", "clear", "fazla"],
            "Beklenmeyen fazladan argüman: 'fazla'.",
        ),
    ],
)
def test_cli_parser_errors_are_turkish(args: list[str], expected_error: str) -> None:
    result = runner.invoke(app, args)

    assert result.exit_code == 2
    output = _cli_text(result)
    assert "Kullanım:" in output
    assert "Yardım için" in output
    assert expected_error in output
    assert "Error:" not in output
    assert "No such command" not in output
    assert "Got unexpected extra argument" not in output
    assert "Missing command." not in output
    assert "Try '" not in output


def test_settings_validation_errors_are_turkish_and_field_specific(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUTALAAMCP_HTTP_TIMEOUT_SECONDS", "0")

    result = runner.invoke(app, ["cache", "clear"])

    assert result.exit_code == 1
    output = _cli_text(result)
    assert ErrorCode.NOT_CONFIGURED in output
    assert "Yapılandırma doğrulanamadı:" in output
    assert (
        "MUTALAAMCP_HTTP_TIMEOUT_SECONDS: değer 0.0 değerinden büyük olmalıdır."
        in output
    )
    assert "validation error" not in output
    assert "Input should" not in output


def test_auth_status_uses_stable_no_auth_state_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mutalaamcp.auth.state.AuthState.read", lambda _self: None)

    result = runner.invoke(app, ["auth", "status"])

    assert result.exit_code == 0
    assert result.output.strip() == "status=none"


def _write_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _block_tools_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "mutalaamcp.tools", None)


def _install_tools_module(
    monkeypatch: pytest.MonkeyPatch, register_tools: object
) -> None:
    fake = ModuleType("mutalaamcp.tools")
    fake.register_tools = register_tools  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mutalaamcp.tools", fake)


@asynccontextmanager
async def _empty_lifespan(_server: object) -> AsyncIterator[dict[str, object]]:
    yield {}


async def _silent_login(_settings: object) -> None:
    return None


@asynccontextmanager
async def _null_http(_settings: object) -> Iterator[object]:
    yield object()


def test_auth_login_uses_shared_activation_message_and_preserves_remote_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = ErrorCode.EMAIL_VERIFICATION_REQUIRED
    remote_detail = "verify this email in English"

    class _Session:
        async def login(self, _present_authorization: object) -> None:
            raise ActivationError(
                code,
                remote_detail,
                http_status=403,
            )

    monkeypatch.setattr("mutalaamcp.cli._http_client", _null_http)
    monkeypatch.setattr(
        "mutalaamcp.cli._auth_session", lambda _settings, _client: _Session()
    )

    result = runner.invoke(app, ["auth", "login"])

    assert result.exit_code == 1
    output = _cli_text(result)
    message = activation_error_message(code, remote_detail)
    assert f"{code}: {message}" in output
    assert f"Uzak hizmet ayrıntısı: {remote_detail}" in output


def test_auth_login_session_expired_is_turkish_first_for_invalid_grant_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_grant_detail = "The authorization grant is invalid, expired, or revoked."

    class _Session:
        async def login(self, _present_authorization: object) -> None:
            raise SessionExpired(invalid_grant_detail)

    monkeypatch.setattr("mutalaamcp.cli._http_client", _null_http)
    monkeypatch.setattr(
        "mutalaamcp.cli._auth_session", lambda _settings, _client: _Session()
    )

    result = runner.invoke(app, ["auth", "login"])

    assert result.exit_code == 1
    assert result.output.strip() == (
        f"{ErrorCode.SESSION_EXPIRED}: Oturumun süresi doldu veya oturum geçersiz. "
        f"Uzak hizmet ayrıntısı: {invalid_grant_detail}"
    )


def _patch_setup_prereqs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "mutalaamcp.cli._launcher_path",
        lambda: str(tmp_path / "bin" / "mutalaamcp"),
    )
    monkeypatch.setattr(
        "mutalaamcp.cli._stable_launcher",
        lambda _settings, launcher: launcher,
    )
    monkeypatch.setattr(
        "mutalaamcp.cli._ensure_active_launcher_state",
        lambda _settings, _launcher: None,
    )


def _patch_fastmcp_client(
    monkeypatch: pytest.MonkeyPatch,
    names: frozenset[str],
    *,
    smoke_payload: dict[str, object] | None = None,
) -> None:
    class _Client:
        def __init__(self, _transport: object) -> None:
            self._names = names

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def list_tools(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(name=name) for name in self._names]

        async def call_tool(
            self, name: str, _arguments: dict[str, object]
        ) -> dict[str, object]:
            if name == "belge_getir":
                return {
                    "ok": False,
                    "error": {"code": ErrorCode.INVALID_PARAMS.value},
                }
            assert name == "mevzuat_ara"
            return {"items": []} if smoke_payload is None else smoke_payload

    monkeypatch.setattr("fastmcp.Client", _Client)


def test_importing_server_does_not_mutate_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[tuple[object, ...]] = []
    orig_open = open
    orig_path_open = Path.open
    orig_mkdir = Path.mkdir
    orig_touch = Path.touch
    orig_write_text = Path.write_text
    orig_write_bytes = Path.write_bytes
    orig_os_mkdir = os.mkdir
    orig_os_makedirs = os.makedirs

    def _record(op: str, target: object) -> None:
        writes.append((op, str(target)))

    def guarded_open(file: object, mode: str = "r", *args: object, **kwargs: object):
        if any(flag in str(mode) for flag in "wax+"):
            _record("open", file)
        return orig_open(file, mode, *args, **kwargs)

    def guarded_path_open(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if any(flag in str(mode) for flag in "wax+"):
            _record("Path.open", self)
        return orig_path_open(self, mode, *args, **kwargs)

    def guarded_mkdir(self: Path, *args: object, **kwargs: object):
        _record("Path.mkdir", self)
        return orig_mkdir(self, *args, **kwargs)

    def guarded_touch(self: Path, *args: object, **kwargs: object):
        _record("Path.touch", self)
        return orig_touch(self, *args, **kwargs)

    def guarded_write_text(self: Path, *args: object, **kwargs: object):
        _record("Path.write_text", self)
        return orig_write_text(self, *args, **kwargs)

    def guarded_write_bytes(self: Path, *args: object, **kwargs: object):
        _record("Path.write_bytes", self)
        return orig_write_bytes(self, *args, **kwargs)

    def guarded_os_mkdir(path: object, *args: object, **kwargs: object):
        _record("os.mkdir", path)
        return orig_os_mkdir(path, *args, **kwargs)

    def guarded_os_makedirs(path: object, *args: object, **kwargs: object):
        _record("os.makedirs", path)
        return orig_os_makedirs(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
    monkeypatch.setattr(Path, "touch", guarded_touch)
    monkeypatch.setattr(Path, "write_text", guarded_write_text)
    monkeypatch.setattr(Path, "write_bytes", guarded_write_bytes)
    monkeypatch.setattr(os, "mkdir", guarded_os_mkdir)
    monkeypatch.setattr(os, "makedirs", guarded_os_makedirs)

    sys.modules.pop("mutalaamcp.server", None)
    importlib.import_module("mutalaamcp.server")

    leaked = [
        item
        for item in writes
        if "__pycache__" not in item[1] and not item[1].endswith(".pyc")
    ]
    assert leaked == []


def test_missing_tool_registrar_raises_tool_registration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_tools_module(monkeypatch)
    with pytest.raises(ToolRegistrationError, match="gereklidir"):
        create_server(lifespan_factory=_empty_lifespan)


def test_broken_tool_registrar_raises_tool_registration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_tools_module(monkeypatch, register_tools=object())
    with pytest.raises(ToolRegistrationError, match="çağrılabilir değildir"):
        create_server(lifespan_factory=_empty_lifespan)


def test_auth_login_fails_when_the_local_stable_launcher_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_setup_prereqs(monkeypatch, tmp_path)
    monkeypatch.setattr("mutalaamcp.cli._run_login", _silent_login)

    class _FailingClient:
        def __init__(self, _transport: object) -> None:
            return None

        async def __aenter__(self) -> Self:
            raise RuntimeError("launcher exited")

        async def __aexit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr("fastmcp.Client", _FailingClient)

    result = runner.invoke(app, ["auth", "login"])

    assert result.exit_code == 1
    assert "Kararlı başlatıcının MCP sağlık denetimi başarısız oldu" in _cli_text(
        result
    )
    assert_no_stdio_config(result)
    assert_no_tokens(result)


@pytest.mark.parametrize(
    "found",
    [
        frozenset(),
        frozenset({"karar_ara"}),
        LEGACY_PUBLIC_TOOL_NAMES,
        frozenset(EXPECTED_PUBLIC_TOOL_NAMES | {"extra_tool"}),
    ],
)
def test_auth_login_health_rejects_wrong_tool_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, found: frozenset[str]
) -> None:
    _patch_setup_prereqs(monkeypatch, tmp_path)
    monkeypatch.setattr("mutalaamcp.cli._run_login", _silent_login)
    assert V1_TOOL_NAMES == EXPECTED_PUBLIC_TOOL_NAMES
    assert not V1_TOOL_NAMES & LEGACY_PUBLIC_TOOL_NAMES
    _patch_fastmcp_client(monkeypatch, found)
    result = runner.invoke(app, ["auth", "login"])
    assert result.exit_code == 1
    blob = _cli_text(result)
    assert ErrorCode.NOT_CONFIGURED in blob
    assert "V1 araçlarını" in blob
    assert_no_stdio_config(result)
    assert_no_tokens(result)


def test_setup_prints_selected_config_without_writing_client_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_setup_prereqs(monkeypatch, tmp_path)
    for name in (
        "MUTALAAMCP_AUTH_BASE_URL",
        "MUTALAAMCP_TERMS_BASE_URL",
    ):
        monkeypatch.delenv(name)
    rendered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "mutalaamcp.cli._print_stdio_config",
        lambda client, launcher: rendered.append((client, launcher)),
    )

    result = runner.invoke(app, ["setup", "--client", "cursor"])

    assert result.exit_code == 0
    assert rendered == [("cursor", str(tmp_path / "bin" / "mutalaamcp"))]
    assert not (tmp_path / "client-config.json").exists()
    assert not (tmp_path / "data" / "config.json").exists()
    assert_no_tokens(result)


@pytest.mark.parametrize(
    ("platform", "os_name", "command"),
    [
        ("darwin", "posix", ["pbcopy"]),
        ("win32", "nt", ["clip"]),
    ],
)
def test_printed_client_config_is_copied_without_shell_or_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
    os_name: str,
    command: list[str],
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def copy_to_clipboard(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("mutalaamcp.cli.sys.platform", platform)
    monkeypatch.setattr("mutalaamcp.cli.os.name", os_name)
    monkeypatch.setattr("mutalaamcp.cli.subprocess.run", copy_to_clipboard)

    from mutalaamcp.cli import _print_stdio_config

    _print_stdio_config("cursor", str(tmp_path / "bin" / "mutalaamcp"))

    printed = capsys.readouterr().out
    assert calls == [
        (
            command,
            {
                "input": printed,
                "text": True,
                "check": True,
                "shell": False,
            },
        )
    ]
    assert all(secret not in printed for secret in SECRETS)


def test_setup_prints_config_when_clipboard_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_setup_prereqs(monkeypatch, tmp_path)
    monkeypatch.setattr("mutalaamcp.cli.sys.platform", "darwin")
    monkeypatch.setattr(
        "mutalaamcp.cli.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("clipboard disabled")),
    )

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0
    assert '"mcpServers"' in _cli_text(result)
    assert "İstemci yapılandırması panoya kopyalanamadı" in _cli_text(result)
    assert_no_tokens(result)


def test_setup_warns_when_clipboard_copy_is_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[object] = []
    monkeypatch.setattr("mutalaamcp.cli.sys.platform", "linux")
    monkeypatch.setattr("mutalaamcp.cli.os.name", "posix")
    monkeypatch.setattr(
        "mutalaamcp.cli.subprocess.run",
        lambda *_args, **_kwargs: calls.append(object()),
    )

    from mutalaamcp.cli import _print_stdio_config

    _print_stdio_config("cursor", str(tmp_path / "bin" / "mutalaamcp"))

    captured = capsys.readouterr()
    assert '"mcpServers"' in captured.out
    assert "Bu platformda panoya kopyalama kullanılamıyor" in captured.err
    assert calls == []


def test_settings_uses_official_public_defaults_below_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "MUTALAAMCP_AUTH_BASE_URL",
        "MUTALAAMCP_TERMS_BASE_URL",
    ):
        monkeypatch.delenv(name)
    monkeypatch.setenv("MUTALAAMCP_DATA_DIR", str(tmp_path / "data"))

    defaults = Settings()

    assert defaults.auth_base_url == "https://mutalaa.tr"
    assert defaults.terms_base_url == "https://mutalaa.tr/terms"
    monkeypatch.setenv("MUTALAAMCP_AUTH_BASE_URL", "https://auth.environment.test")
    monkeypatch.setenv(
        "MUTALAAMCP_TERMS_BASE_URL", "https://terms.environment.test/kosullar"
    )

    from_environment = Settings()

    assert from_environment.auth_base_url == "https://auth.environment.test"
    assert from_environment.terms_base_url == "https://terms.environment.test/kosullar"


@pytest.mark.parametrize(
    ("field_name", "expected_error"),
    (
        (
            "auth_base_url",
            "Mütalaa kimlik doğrulama temel URL'si, açıkça belirtilmiş bir geri döngü test kökeni olmadıkça https olmalıdır",
        ),
        (
            "terms_base_url",
            "koşullar temel URL'si, açıkça belirtilmiş bir geri döngü test kökeni olmadıkça https olmalıdır",
        ),
    ),
)
def test_settings_reject_non_loopback_http_auth_endpoints(
    field_name: str, expected_error: str
) -> None:
    with pytest.raises(ValueError, match=expected_error):
        Settings(**{field_name: "http://credential-sink.test"})


def test_settings_allow_http_only_for_explicit_loopback_auth_origins() -> None:
    settings = Settings(
        auth_base_url="http://localhost:4318/",
        terms_base_url="http://[::1]:4319/kosullar/",
    )

    assert settings.auth_base_url == "http://localhost:4318"
    assert settings.terms_base_url == "http://[::1]:4319/kosullar"


def test_auth_login_exercises_the_generated_stable_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_setup_prereqs(monkeypatch, tmp_path)
    monkeypatch.setattr("mutalaamcp.cli._run_login", _silent_login)
    _patch_fastmcp_client(monkeypatch, V1_TOOL_NAMES)
    current = tmp_path / "bin" / "mutalaamcp"
    stable = tmp_path / "data" / "bin" / "mutalaamcp"
    generation_calls: list[tuple[Path, str]] = []
    transport_calls: list[tuple[str, list[str], dict[str, str]]] = []

    def _stable(settings: Settings, launcher: str) -> str:
        generation_calls.append((settings.data_dir, launcher))
        return str(stable)

    class _Transport:
        def __init__(
            self,
            command: str,
            args: list[str],
            env: dict[str, str],
            keep_alive: bool,
        ) -> None:
            assert keep_alive is False
            transport_calls.append((command, args, env))

    monkeypatch.setattr("mutalaamcp.cli._stable_launcher", _stable)
    monkeypatch.setattr("fastmcp.client.transports.StdioTransport", _Transport)

    result = runner.invoke(app, ["auth", "login"])

    assert result.exit_code == 0
    assert generation_calls == [(tmp_path / "data", str(current))]
    assert [(command, args) for command, args, _environment in transport_calls] == [
        (str(stable), ["serve"])
    ]
    assert {
        name: transport_calls[0][2][name]
        for name in (
            "MUTALAAMCP_AUTH_BASE_URL",
            "MUTALAAMCP_TERMS_BASE_URL",
        )
    } == {
        "MUTALAAMCP_AUTH_BASE_URL": "https://mutalaa.test",
        "MUTALAAMCP_TERMS_BASE_URL": "https://mutalaa.test/kosullar",
    }


def test_auth_login_warns_when_only_the_official_source_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_setup_prereqs(monkeypatch, tmp_path)
    monkeypatch.setattr("mutalaamcp.cli._run_login", _silent_login)
    _patch_fastmcp_client(
        monkeypatch,
        V1_TOOL_NAMES,
        smoke_payload={
            "error": {
                "code": ErrorCode.UPSTREAM_UNAVAILABLE.value,
                "details": {"upstream": "bedesten"},
            }
        },
    )

    result = runner.invoke(app, ["auth", "login"])

    assert result.exit_code == 0
    assert "resmî kaynak=bedesten" in _cli_text(result)
    assert ErrorCode.UPSTREAM_UNAVAILABLE.value in _cli_text(result)


def test_auth_login_retries_terms_activation_without_repeating_device_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terms_url = "https://mutalaa.test/kosullar"
    opened: list[str] = []

    class _TermsSession:
        login_calls = 0
        activate_calls = 0

        async def login(self, _present: object) -> None:
            self.login_calls += 1
            raise ActivationError(
                ErrorCode.TERMS_REQUIRED,
                "accept terms",
                http_status=403,
                terms_url=terms_url,
            )

        async def activate(self) -> object:
            self.activate_calls += 1
            return object()

    session = _TermsSession()
    monkeypatch.setattr("mutalaamcp.cli._http_client", _null_http)
    monkeypatch.setattr(
        "mutalaamcp.cli._auth_session", lambda _settings, _client: session
    )
    monkeypatch.setattr("mutalaamcp.cli._confirm_terms_accepted", lambda: True)
    monkeypatch.setattr(
        "mutalaamcp.cli.webbrowser.open", lambda url: opened.append(url)
    )

    asyncio.run(_run_login(Settings()))

    assert session.login_calls == 1
    assert session.activate_calls == 1
    assert opened == [terms_url]


def test_launcher_resolver_accepts_absolute_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = _write_executable(tmp_path / "bin" / "mutalaamcp")
    monkeypatch.setattr("mutalaamcp.cli._launcher_candidates", lambda: [launcher])
    resolved = _launcher_path()
    assert Path(resolved).is_absolute()
    assert Path(resolved) == launcher.resolve()


@pytest.mark.parametrize("kind", ["module", "relative", "unavailable"])
def test_launcher_resolver_rejects_module_relative_and_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.chdir(tmp_path)
    if kind == "module":
        candidate = _write_executable(tmp_path / "mutalaamcp.py")
    elif kind == "relative":
        relative = Path("mutalaamcp")
        orig_resolve = Path.resolve

        def keep_relative(self: Path, *args: object, **kwargs: object) -> Path:
            if not self.is_absolute() and self.name == "mutalaamcp":
                return Path("mutalaamcp")
            return orig_resolve(self, *args, **kwargs)

        _write_executable(tmp_path / "mutalaamcp")
        monkeypatch.setattr(Path, "resolve", keep_relative)
        candidate = relative
    else:
        candidate = tmp_path / "mutalaamcp"
    monkeypatch.setattr("mutalaamcp.cli._launcher_candidates", lambda: [candidate])
    with pytest.raises(typer.Exit) as caught:
        _launcher_path()
    code = getattr(caught.value, "exit_code", getattr(caught.value, "code", None))
    assert code == 1


def test_cache_refresh_authorizes_before_provider_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Auth:
        async def ensure_authorized(self) -> None:
            calls.append("authorize")

    class _Decisions:
        async def get_document(
            self, document_id: str, *, refresh: bool
        ) -> dict[str, object]:
            assert document_id == "bedesten:decision-42"
            assert refresh is True
            calls.append("provider")
            return {"ok": True, "id": document_id}

    class _Runtime:
        auth = _Auth()
        decisions = _Decisions()

        async def aclose(self) -> None:
            calls.append("close")

    async def create_runtime(_settings: Settings) -> _Runtime:
        return _Runtime()

    monkeypatch.setattr("mutalaamcp.tools.create_runtime", create_runtime)
    settings = Settings(
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
        model_dir=tmp_path / "models",
    )
    monkeypatch.setattr("mutalaamcp.cli.load_settings", lambda: settings)

    @contextmanager
    def no_mutation_lock(_path: Path) -> Iterator[None]:
        yield

    monkeypatch.setattr("mutalaamcp.cli.mutation_lock", no_mutation_lock)

    result = runner.invoke(app, ["cache", "refresh", "bedesten:decision-42"])

    assert result.exit_code == 0, (
        f"CLI output: {_cli_text(result)}\n"
        f"Exception: {result.exception!r}\n"
        f"Calls: {calls}"
    )
    assert '"id":"bedesten:decision-42"' in _cli_text(result)
    assert calls == ["authorize", "provider", "close"]
    assert_no_tokens(result)


def test_cache_refresh_stops_before_cache_or_provider_when_auth_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Auth:
        async def ensure_authorized(self) -> None:
            calls.append("authorize")
            raise AuthenticationRequired()

    class _Decisions:
        async def get_document(self, *_args: object, **_kwargs: object) -> object:
            pytest.fail("provider must not be reached after authorization failure")

    class _Runtime:
        auth = _Auth()
        decisions = _Decisions()

        @property
        def cache(self) -> object:
            pytest.fail("cache must not be accessed after authorization failure")

        async def aclose(self) -> None:
            calls.append("close")

    async def create_runtime(_settings: Settings) -> _Runtime:
        return _Runtime()

    monkeypatch.setattr("mutalaamcp.tools.create_runtime", create_runtime)
    settings = Settings(
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
        model_dir=tmp_path / "models",
    )
    monkeypatch.setattr("mutalaamcp.cli.load_settings", lambda: settings)

    @contextmanager
    def no_mutation_lock(_path: Path) -> Iterator[None]:
        yield

    monkeypatch.setattr("mutalaamcp.cli.mutation_lock", no_mutation_lock)

    result = runner.invoke(app, ["cache", "refresh", "bedesten:decision-42"])

    assert result.exit_code == 1
    assert ErrorCode.AUTHENTICATION_REQUIRED in _cli_text(result)
    assert calls == ["authorize", "close"]

    assert_no_tokens(result)


def test_cache_refresh_reports_unexpected_failures_in_turkish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def create_runtime(_settings: Settings) -> object:
        raise RuntimeError("upstream diagnostic")

    settings = Settings(
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
        model_dir=tmp_path / "models",
    )
    monkeypatch.setattr("mutalaamcp.tools.create_runtime", create_runtime)
    monkeypatch.setattr("mutalaamcp.cli.load_settings", lambda: settings)

    @contextmanager
    def no_mutation_lock(_path: Path) -> Iterator[None]:
        yield

    monkeypatch.setattr("mutalaamcp.cli.mutation_lock", no_mutation_lock)
    result = runner.invoke(app, ["cache", "refresh", "bedesten:decision-42"])

    assert result.exit_code == 1
    assert "Önbellek yenilenemedi: RuntimeError: upstream diagnostic" in _cli_text(
        result
    )


def test_cache_clear_holds_mutation_lock_during_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "cache" / "cache.sqlite3"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"placeholder")
    lock_path = tmp_path / "data" / "serve.lock"
    held_during_clear: list[bool] = []

    class FakeStore:
        def __init__(self, path: Path, *, max_bytes: int) -> None:
            self.path = path
            self.max_bytes = max_bytes

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def clear(self) -> None:
            held_during_clear.append(serve_lock_held(lock_path))
            with pytest.raises(AlreadyRunning):
                FileLock(lock_path).acquire()

    monkeypatch.setattr("mutalaamcp.cache.store.CacheStore", FakeStore)
    result = runner.invoke(app, ["cache", "clear"])
    assert result.exit_code == 0
    assert held_during_clear == [True]
    assert serve_lock_held(lock_path) is False
    assert_no_tokens(result)


def test_cache_clear_reports_failures_with_technical_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "cache" / "cache.sqlite3"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"placeholder")

    class FailingStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def clear(self) -> None:
            raise OSError("database is read-only")

    monkeypatch.setattr("mutalaamcp.cache.store.CacheStore", FailingStore)
    result = runner.invoke(app, ["cache", "clear"])

    assert result.exit_code == 1
    assert "Yerel önbellek silinemedi: OSError: database is read-only" in _cli_text(
        result
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv Python symlinks")
def test_launcher_discovery_keeps_venv_directory_without_activated_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    python = bindir / "python"
    python.symlink_to(sys.executable)
    launcher = bindir / "mutalaamcp"
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    monkeypatch.setattr("mutalaamcp.cli.sys.executable", str(python))
    monkeypatch.setattr("mutalaamcp.cli.shutil.which", lambda _: None)

    assert _launcher_path() == str(launcher.resolve())


def test_login_rejects_running_server_before_starting_device_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def login(_settings: Settings) -> None:
        calls.append("login")

    monkeypatch.setattr("mutalaamcp.cli._run_login", login)
    with FileLock(tmp_path / "data" / "serve.lock"):
        result = runner.invoke(app, ["auth", "login"])

    assert result.exit_code == 1
    assert ErrorCode.ALREADY_RUNNING in _cli_text(result)
    assert "istemciyi kapatın" in _cli_text(result)
    assert calls == []


def test_login_releases_lock_before_health_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "data" / "serve.lock"
    checks: list[str] = []

    async def login(_settings: Settings) -> None:
        assert serve_lock_held(lock_path)
        checks.append("login")

    async def health(_launcher: str) -> None:
        with FileLock(lock_path):
            checks.append("health")

    monkeypatch.setattr("mutalaamcp.cli._run_login", login)
    monkeypatch.setattr("mutalaamcp.cli._prepare_stable_launcher", lambda _: "launcher")
    monkeypatch.setattr("mutalaamcp.cli._health_check", health)
    result = runner.invoke(app, ["auth", "login"])

    assert result.exit_code == 0
    assert checks == ["login", "health"]


def test_ocr_mutation_is_rejected_while_serve_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        "mutalaamcp.ocr.remove_ocr", lambda _settings: calls.append(object())
    )
    lock_path = tmp_path / "data" / "serve.lock"

    with FileLock(lock_path):
        result = runner.invoke(app, ["ocr", "remove"])

    assert result.exit_code == 1
    assert ErrorCode.ALREADY_RUNNING in _cli_text(result)
    assert calls == []


def test_logout_delegates_local_auth_session_logout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeAuthSession:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def logout(self) -> LogoutResult:
            calls.append("logout")
            return LogoutResult(cleanup_error=None)

    monkeypatch.setattr("mutalaamcp.auth.AuthSession", FakeAuthSession)
    monkeypatch.setattr("mutalaamcp.cli._http_client", _null_http)
    result = runner.invoke(app, ["auth", "logout"])
    assert result.exit_code == 0
    assert calls == ["logout"]
    assert_no_tokens(result)


def test_cli_help_handles_legacy_output_encoding() -> None:
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "mutalaamcp", "--help"],
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        check=True,
    )
    assert "Kullanım" in result.stdout.decode("utf-8")


def test_update_installs_the_verified_channel_offer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mutalaamcp.update import (
        ActiveVersion,
        PackageIntegrity,
        UpdateOffer,
        UpdateResult,
        VerifiedUpdateManifest,
    )

    manifest = VerifiedUpdateManifest(
        index_url="https://pypi.org/simple",
        package=PackageIntegrity(
            name="mutalaamcp",
            version="2.0.0",
            hashes=("0" * 64,),
            url="https://releases.test/mutalaamcp-2.0.0-py3-none-any.whl",
        ),
        dependencies=(),
    )
    offer = UpdateOffer(version="2.0.0", manifest=manifest)
    monkeypatch.setattr(
        "mutalaamcp.update.fetch_update_offer", lambda _client, _url: offer
    )
    monkeypatch.setattr("mutalaamcp.cli._launcher_path", lambda: "/stable/mutalaamcp")
    installs: list[dict[str, object]] = []

    def install(_settings, version, **kwargs):
        installs.append({"version": version, **kwargs})
        return UpdateResult(
            previous=ActiveVersion("1.0.0", Path("/stable/mutalaamcp")),
            active=ActiveVersion("2.0.0", Path("/candidate/mutalaamcp")),
        )

    monkeypatch.setattr("mutalaamcp.update.update_to_version", install)

    result = runner.invoke(app, ["update"])

    assert result.exit_code == 0
    assert installs and installs[0]["version"] == "2.0.0"
    assert installs[0]["integrity_manifest"] == manifest
    assert '"active_version":"2.0.0"' in _cli_text(result)


def test_update_pins_must_match_the_channel_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mutalaamcp.update import UpdateOffer

    offer = UpdateOffer(
        version="2.0.0",
        manifest=None,  # type: ignore[arg-type] -- pinned mismatch fails before use
    )
    monkeypatch.setattr(
        "mutalaamcp.update.fetch_update_offer", lambda _client, _url: offer
    )
    monkeypatch.setattr(
        "mutalaamcp.update.update_to_version",
        lambda *_a, **_k: pytest.fail("update_to_version must not run"),
    )

    result = runner.invoke(app, ["update", "--version", "1.9.9"])

    assert result.exit_code != 0
    assert "2.0.0" in _cli_text(result)


def test_update_reports_channel_failures_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mutalaamcp.update import UpdateError

    def fail(_client, _url):
        raise UpdateError("Güncelleme bildirimine erişilemiyor.")

    monkeypatch.setattr("mutalaamcp.update.fetch_update_offer", fail)

    result = runner.invoke(app, ["update"])

    assert result.exit_code != 0
    assert ErrorCode.UPSTREAM_UNAVAILABLE.value in _cli_text(result)


def test_update_while_the_managed_service_runs_explains_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mutalaamcp.update import UpdateOffer

    offer = UpdateOffer(version="2.0.0", manifest=None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "mutalaamcp.update.fetch_update_offer", lambda _client, _url: offer
    )
    lock_path = tmp_path / "data" / "serve.lock"
    with FileLock(lock_path):
        result = runner.invoke(app, ["update"])

    assert result.exit_code != 0
    assert "hizmet" in _cli_text(result).lower()


def test_serve_signals_readiness_probe_when_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A candidate that loses the serve lock still proved it starts; without the
    # signal the selector would roll back a healthy update on any collision.
    marker = tmp_path / "data" / "runtime-ready.marker"
    marker.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    monkeypatch.setenv("MUTALAAMCP_RUNTIME_READY_FILE", str(marker))
    lock = FileLock(tmp_path / "data" / "serve.lock")
    lock.acquire()

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 1
    assert marker.read_bytes() == b"ready\n"
