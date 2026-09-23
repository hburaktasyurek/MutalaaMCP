"""Independent stdio connections backed by one private, per-user runtime.

Only the backend owns credentials, cache and the existing maintenance lock.
Each frontend holds a streaming lease; the backend exits after the last frontend
disconnects, including when a client kills its frontend without a clean shutdown.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mutalaamcp import __version__
from mutalaamcp.native_service import LoopbackGuard
from mutalaamcp.runtime import AlreadyRunning, FileLock
from mutalaamcp.settings import Settings

_START_TIMEOUT = 20.0
_IDLE_TIMEOUT = 2.0
_SETTINGS_ENV = "MUTALAAMCP_STDIO_SETTINGS"
_ENDPOINT_NAME = "stdio-runtime.json"


def _settings_json(settings: Settings) -> str:
    return json.dumps(
        settings.model_dump(),
        sort_keys=True,
        default=lambda value: (
            sorted(value) if isinstance(value, (set, frozenset)) else str(value)
        ),
    )


def _configuration(settings: Settings) -> str:
    return hashlib.sha256(_settings_json(settings).encode()).hexdigest()


@dataclass(frozen=True)
class _Endpoint:
    port: int
    token: str = field(repr=False)
    configuration: str
    version: str = __version__

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _read_endpoint(path: Path) -> _Endpoint | None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(status.st_mode) or (
        os.name == "posix"
        and (status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) != 0o600)
    ):
        raise RuntimeError("Yerel sunucu bağlantı dosyası kullanıcıya özel değil.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or type(value.get("port")) is not int
            or not 1 <= value["port"] <= 65535
            or not all(
                isinstance(value.get(key), str) and value[key]
                for key in ("token", "configuration", "version")
            )
        ):
            return None
        return _Endpoint(**value)
    except (ValueError, TypeError):
        return None


def _write_endpoint(path: Path, endpoint: _Endpoint) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".stdio-runtime-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(asdict(endpoint), output)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _proof(token: str, nonce: str) -> str:
    return hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()


async def _probe(client: httpx.AsyncClient, endpoint: _Endpoint) -> bool:
    # Authenticate the listener before sending the bearer credential. A stale
    # descriptor must not hand it to another process that reused the old port.
    nonce = secrets.token_hex(32)
    try:
        response = await client.get(
            f"{endpoint.url}/health", params={"nonce": nonce}, timeout=0.5
        )
        value = response.json()
        return (
            response.status_code == 200
            and isinstance(value, dict)
            and isinstance(value.get("proof"), str)
            and hmac.compare_digest(value["proof"], _proof(endpoint.token, nonce))
        )
    except (httpx.HTTPError, ValueError):
        return False


def _startup_lock(settings: Settings) -> FileLock:
    return FileLock(settings.data_dir / "stdio-start.lock")


async def _acquire_startup_lock(settings: Settings) -> FileLock:
    lock = _startup_lock(settings)
    deadline = time.monotonic() + _START_TIMEOUT
    while True:
        try:
            return lock.acquire()
        except AlreadyRunning:
            if time.monotonic() >= deadline:
                raise RuntimeError("Yerel sunucunun başlatılması zaman aşımına uğradı.")
            await asyncio.sleep(0.05)


def _spawn_backend(settings: Settings) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment[_SETTINGS_ENV] = _settings_json(settings)
    # The frontend acknowledges its selector only after the backend is ready.
    environment.pop("MUTALAAMCP_RUNTIME_READY_FILE", None)
    options: dict[str, Any] = (
        # Windows DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP. Numeric flags
        # also keep this module importable/type-checkable on POSIX.
        {"creationflags": 0x00000008 | 0x00000200}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    return subprocess.Popen(
        [sys.executable, "-m", "mutalaamcp.stdio_runtime"],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **options,
    )


async def _stop_backend(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)


@asynccontextmanager
async def _connection(
    settings: Settings,
) -> AsyncIterator[tuple[_Endpoint, AsyncIterator[bytes]]]:
    from mutalaamcp.tools import _signal_runtime_ready, create_runtime

    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        lock = await _acquire_startup_lock(settings)
        child: subprocess.Popen[bytes] | None = None
        readiness_signaled = False
        try:
            path = settings.data_dir / _ENDPOINT_NAME
            endpoint = _read_endpoint(path)
            if endpoint is None or not await _probe(client, endpoint):
                # Preserve exclusion against HTTP, older stdio servers and all
                # maintenance commands. Only an idle installation may start.
                with FileLock(settings.serve_lock_path):
                    if os.environ.get("MUTALAAMCP_RUNTIME_READY_FILE") is not None:
                        # First upgrade may still run an older selector without
                        # the rollback lock. Validate in its own child process
                        # before spawning a detached cache owner. A timeout here
                        # kills the actual cache owner, as in the old protocol.
                        runtime = await create_runtime(settings)
                        try:
                            _signal_runtime_ready()
                            readiness_signaled = True
                        finally:
                            await runtime.aclose()
                child = _spawn_backend(settings)
                deadline = time.monotonic() + _START_TIMEOUT
                while True:
                    endpoint = _read_endpoint(path)
                    if endpoint is not None and await _probe(client, endpoint):
                        break
                    if child.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Paylaşılan yerel MCP sunucusu başlatılamadı."
                        )
                    await asyncio.sleep(0.05)
            if (
                endpoint.configuration != _configuration(settings)
                or endpoint.version != __version__
            ):
                # Preserve the selector's collision/readiness behavior; a
                # live backend must never cause a cache rollback underneath it.
                raise AlreadyRunning(settings.serve_lock_path)
            async with AsyncExitStack() as lease:
                response = await asyncio.wait_for(
                    lease.enter_async_context(
                        client.stream(
                            "GET",
                            f"{endpoint.url}/lease",
                            headers={"Authorization": f"Bearer {endpoint.token}"},
                            timeout=None,
                        )
                    ),
                    timeout=_START_TIMEOUT,
                )
                response.raise_for_status()
                stream = response.aiter_bytes()
                await asyncio.wait_for(anext(stream), timeout=_START_TIMEOUT)
                # A failed readiness acknowledgement must stop a newly created
                # backend before the selector can restore its cache backup.
                if not readiness_signaled:
                    _signal_runtime_ready()
                if child is not None:
                    assert child.stdin is not None
                    child.stdin.write(b"R")
                    child.stdin.close()
                lock.release()
                # A live lease now keeps the backend up independently of the
                # process that happened to launch it first.
                child = None
                yield endpoint, stream
        finally:
            # Failed candidates must stop before the selector restores a cache.
            try:
                if child is not None:
                    await _stop_backend(child)
                    if child.stdin is not None:
                        child.stdin.close()
                    # Termination need not run the child's finally block.
                    # Startup is still locked, preventing descriptor races.
                    (settings.data_dir / _ENDPOINT_NAME).unlink(missing_ok=True)
            finally:
                lock.release()


def _http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    **kwargs: Any,
) -> httpx.AsyncClient:
    kwargs.update(trust_env=False, follow_redirects=False)
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(30, read=None),
        auth=auth,
        **kwargs,
    )


async def _watch_lease(stream: AsyncIterator[bytes]) -> None:
    async for _ in stream:
        pass
    raise RuntimeError("Paylaşılan yerel MCP sunucusuyla bağlantı kapandı.")


async def run_stdio(settings: Settings) -> None:
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.server import create_proxy

    from mutalaamcp.server import RESEARCH_INSTRUCTIONS, SERVER_NAME

    async with _connection(settings) as (endpoint, stream):
        transport = StreamableHttpTransport(
            f"{endpoint.url}/mcp",
            headers={"Authorization": f"Bearer {endpoint.token}"},
            httpx_client_factory=_http_client,
        )
        proxy = create_proxy(
            transport,
            name=SERVER_NAME,
            version=__version__,
            instructions=RESEARCH_INSTRUCTIONS,
            strict_input_validation=False,
        )
        serving = asyncio.create_task(proxy.run_stdio_async(show_banner=False))
        watching = asyncio.create_task(_watch_lease(stream))
        try:
            done, _ = await asyncio.wait(
                (serving, watching), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                await task
        finally:
            for task in (serving, watching):
                task.cancel()
            await asyncio.gather(serving, watching, return_exceptions=True)


class _Authorization:
    def __init__(self, app: ASGIApp, *, token: str) -> None:
        self.app = app
        self.token = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] != "/health":
            supplied = dict(scope.get("headers", [])).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.token):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


@dataclass
class _Leases:
    clients: int = 0
    last_active: float = field(default_factory=time.monotonic)
    shutdown_lock: FileLock | None = None


async def _serve_backend(settings: Settings, leases: _Leases) -> None:
    import uvicorn

    from mutalaamcp.server import create_server
    from mutalaamcp.tools import create_runtime

    @asynccontextmanager
    async def lifespan(_server: Any) -> AsyncIterator[dict[str, Any]]:
        runtime = await create_runtime(settings)
        try:
            yield {"runtime": runtime}
        finally:
            await runtime.aclose()

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.setblocking(False)
        endpoint = _Endpoint(
            port=listener.getsockname()[1],
            token=secrets.token_urlsafe(32),
            configuration=_configuration(settings),
        )
        mcp = create_server(lifespan_factory=lifespan)

        @mcp.custom_route("/health", methods=["GET"])
        async def health(request: Request) -> JSONResponse:
            nonce = request.query_params.get("nonce", "")
            if len(nonce) != 64:
                return JSONResponse({"error": "invalid_nonce"}, status_code=400)
            return JSONResponse({"proof": _proof(endpoint.token, nonce)})

        @mcp.custom_route("/lease", methods=["GET"])
        async def lease(_request: Request) -> StreamingResponse:
            async def body() -> AsyncIterator[bytes]:
                leases.clients += 1
                try:
                    yield b"ready\n"
                    while True:
                        await asyncio.sleep(60)
                        yield b"\n"
                finally:
                    leases.clients -= 1
                    leases.last_active = time.monotonic()

            return StreamingResponse(body(), media_type="application/octet-stream")

        application = mcp.http_app(
            path="/mcp",
            stateless_http=True,
            middleware=[
                Middleware(LoopbackGuard, port=endpoint.port),
                Middleware(_Authorization, token=endpoint.token),
            ],
        )
        server = uvicorn.Server(
            uvicorn.Config(
                application,
                log_level="error",
                access_log=False,
                timeout_graceful_shutdown=2,
            )
        )
        running = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if running.done():
                    await running
                    raise RuntimeError("Yerel MCP çalışma zamanı açılamadı.")
                await asyncio.sleep(0.01)
            _write_endpoint(settings.data_dir / _ENDPOINT_NAME, endpoint)
            while not running.done():
                await asyncio.sleep(0.1)
                if (
                    leases.clients
                    or time.monotonic() - leases.last_active < _IDLE_TIMEOUT
                ):
                    continue
                try:
                    leases.shutdown_lock = _startup_lock(settings).acquire()
                except AlreadyRunning:
                    continue
                # Retain the startup lock through shutdown and serve-lock
                # release so a new frontend cannot attach to a dying runtime.
                server.should_exit = True
                break
            await running
        finally:
            server.should_exit = True
            if not running.done():
                await running


def _watch_startup_parent() -> None:
    # Before handoff, EOF means the frontend died (including TerminateProcess
    # on Windows). Do not leave a detached runtime mutating the cache while its
    # selector rolls back. A separate thread also covers blocking initialization.
    if sys.stdin.buffer.read(1) != b"R":
        os._exit(1)


def _backend_main() -> None:
    threading.Thread(target=_watch_startup_parent, daemon=True).start()
    settings = Settings.model_validate_json(os.environ.pop(_SETTINGS_ENV))
    leases = _Leases()
    try:
        with FileLock(settings.serve_lock_path):
            try:
                asyncio.run(_serve_backend(settings, leases))
            finally:
                (settings.data_dir / _ENDPOINT_NAME).unlink(missing_ok=True)
    finally:
        if leases.shutdown_lock is not None:
            leases.shutdown_lock.release()


if __name__ == "__main__":
    _backend_main()
