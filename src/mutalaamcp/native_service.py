"""Loopback HTTP runtime and per-user startup registration for native OAuth."""

from __future__ import annotations

import asyncio
import os
import plistlib
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mutalaamcp import __version__
from mutalaamcp.settings import Settings

_LABEL = "tr.mutalaa.mcp"


class LoopbackGuard:
    """Reject DNS rebinding and cross-site browser requests on every HTTP route."""

    def __init__(self, app: ASGIApp, *, port: int) -> None:
        self.app = app
        self.host = f"127.0.0.1:{port}".encode()
        self.origin = b"http://" + self.host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            origin = headers.get(b"origin")
            if headers.get(b"host") != self.host or (
                origin is not None and origin != self.origin
            ):
                await JSONResponse({"error": "invalid_origin"}, status_code=403)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def create_native_server(settings: Settings):
    from mutalaamcp.auth.native import NativeOAuth
    from mutalaamcp.server import create_server
    from mutalaamcp.tools import ToolRuntime, tool_lifespan

    provider = NativeOAuth(port=settings.http_port, data_dir=settings.data_dir)

    @asynccontextmanager
    async def lifespan(mcp):
        try:
            async with tool_lifespan(mcp) as context:
                runtime = context["runtime"]
                if isinstance(runtime, ToolRuntime):
                    provider.session = runtime.auth
                yield context
        finally:
            await provider.aclose()

    server = create_server(lifespan_factory=lifespan, auth=provider)

    @server.custom_route("/health", methods=["GET"])
    async def health(_request):
        return JSONResponse(
            {"service": "MutalaaMCP", "version": __version__, "transport": "http"}
        )

    return server


def run_native_server(settings: Settings) -> None:
    server = create_native_server(settings)
    asyncio.run(
        server.run_http_async(
            host="127.0.0.1",
            port=settings.http_port,
            path="/mcp",
            stateless_http=True,
            show_banner=False,
            log_level="warning",
            middleware=[Middleware(LoopbackGuard, port=settings.http_port)],
            uvicorn_config={"access_log": False},
        )
    )


def native_service_ready(settings: Settings) -> bool:
    try:
        response = httpx.get(
            f"http://127.0.0.1:{settings.http_port}/health", timeout=1, trust_env=False
        )
        value = response.json()
        return response.status_code == 200 and value == {
            "service": "MutalaaMCP",
            "version": __version__,
            "transport": "http",
        }
    except (httpx.HTTPError, ValueError):
        return False


def install_native_service(settings: Settings, launcher: str) -> None:
    """Install an OS-owned per-user service; research remains on loopback."""
    environment = {
        "MUTALAAMCP_HTTP_PORT": str(settings.http_port),
        "MUTALAAMCP_DATA_DIR": str(settings.data_dir),
        "MUTALAAMCP_CACHE_DIR": str(settings.cache_dir),
        "MUTALAAMCP_MODEL_DIR": str(settings.model_dir),
        "MUTALAAMCP_AUTH_BASE_URL": settings.auth_base_url,
        "MUTALAAMCP_TERMS_BASE_URL": settings.terms_base_url,
    }
    if sys.platform == "darwin":
        directory = Path.home() / "Library" / "LaunchAgents"
        directory.mkdir(parents=True, exist_ok=True)
        plist = directory / f"{_LABEL}.plist"
        payload: dict[str, Any] = {
            "Label": _LABEL,
            "ProgramArguments": [launcher, "serve-http"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "EnvironmentVariables": environment,
            "ProcessType": "Background",
            "ThrottleInterval": 10,
        }
        plist.write_bytes(plistlib.dumps(payload))
        plist.chmod(0o600)
        domain = f"gui/{os.getuid()}"
        registered = (
            subprocess.run(
                ["launchctl", "print", f"{domain}/{_LABEL}"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        if registered:
            subprocess.run(
                ["launchctl", "bootout", f"{domain}/{_LABEL}"],
                check=True,
                capture_output=True,
            )
        subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist)],
            check=True,
            capture_output=True,
        )
    elif os.name == "nt":
        # Task Scheduler starts a per-user worker at sign-in, including after reboot.
        # Keep paths/config in a wrapper so /TR is short and has one quoted command.
        from mutalaamcp.launcher import _cmd_quote

        wrapper = settings.data_dir / "native-service.cmd"
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        lines = ["@echo off", "setlocal DisableDelayedExpansion"]
        for key, value in environment.items():
            if any(ch in value for ch in ('"', "\r", "\n", "\0")):
                raise ValueError("Hizmet ayarı desteklenmeyen karakter içeriyor.")
            lines.append(f'set "{key}={value.replace("%", "%%")}"')
        lines.append(f"{_cmd_quote(Path(launcher))} serve-http")
        wrapper.write_text("\n".join(lines) + "\n", encoding="utf-8")
        subprocess.run(
            [
                "schtasks",
                "/Create",
                "/TN",
                _LABEL,
                "/SC",
                "ONLOGON",
                "/TR",
                f'"{wrapper}"',
                "/F",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["schtasks", "/Run", "/TN", _LABEL], check=True, capture_output=True
        )
    else:
        raise ValueError(
            "Uygulama içi giriş hizmeti macOS ve Windows için desteklenir."
        )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if native_service_ready(settings):
            return
        time.sleep(0.25)
    raise RuntimeError(
        "Yerel HTTP hizmeti başlatılamadı. Diğer MutalaaMCP sunucusunu kapatıp kurulumu yeniden deneyin."
    )


def stop_native_service() -> None:
    """Stop the registered worker without deleting credentials or client config."""
    if sys.platform == "darwin":
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{_LABEL}"],
            check=True,
            capture_output=True,
        )
    elif os.name == "nt":
        subprocess.run(
            ["schtasks", "/End", "/TN", _LABEL], check=True, capture_output=True
        )
    else:
        raise ValueError("Yerel hizmet bu platformda desteklenmiyor.")
