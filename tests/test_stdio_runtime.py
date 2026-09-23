"""Real stdio clients share one runtime without sharing MCP sessions."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from mutalaamcp import __version__
from mutalaamcp.runtime import FileLock, serve_lock_held
from mutalaamcp.stdio_runtime import _Endpoint, _probe, _proof, _read_endpoint


def _environment(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "MUTALAAMCP_DATA_DIR": str(root / "data"),
            "MUTALAAMCP_CACHE_DIR": str(root / "cache"),
            "MUTALAAMCP_MODEL_DIR": str(root / "models"),
            "MUTALAAMCP_AUTH_BASE_URL": "https://mutalaa.invalid",
            "MUTALAAMCP_TERMS_BASE_URL": "https://mutalaa.invalid/terms",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        }
    )
    environment.pop("MUTALAAMCP_RUNTIME_READY_FILE", None)
    return environment


async def _start(environment: dict[str, str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "mutalaamcp",
        "serve",
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=2**20,
    )


async def _send(process: asyncio.subprocess.Process, message: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message).encode() + b"\n")
    await process.stdin.drain()


async def _request(
    process: asyncio.subprocess.Process, method: str, params: dict | None = None
) -> dict:
    await _send(
        process,
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert process.stdout is not None
    while line := await asyncio.wait_for(process.stdout.readline(), timeout=20):
        result = json.loads(line)
        if result.get("id") == 1:
            return result
    assert process.stderr is not None
    pytest.fail((await process.stderr.read()).decode())


async def _initialize(process: asyncio.subprocess.Process) -> dict:
    response = await _request(
        process,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "concurrent-client-test", "version": "1"},
        },
    )
    assert "result" in response, response
    await _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    return response["result"]


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        assert process.stdin is not None
        process.stdin.close()
        try:
            await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.communicate()


@pytest.mark.asyncio
@pytest.mark.parametrize("kill_first", [False, True])
async def test_two_stdio_clients_survive_first_client_exit(
    tmp_path: Path, kill_first: bool
) -> None:
    environment = _environment(tmp_path)
    clients = [await _start(environment)]
    try:
        # The forced-exit case deterministically kills the frontend that
        # launched the backend. The other case exercises simultaneous startup.
        first = await _initialize(clients[0]) if kill_first else None
        clients.append(await _start(environment))
        initialized = await asyncio.gather(
            *(
                _initialize(client)
                for client in (clients[1:] if kill_first else clients)
            ),
            return_exceptions=True,
        )
        if first is not None:
            initialized.insert(0, first)
        for result in initialized:
            if isinstance(result, BaseException):
                raise result
        assert [item["serverInfo"]["version"] for item in initialized] == [
            __version__,
            __version__,
        ]
        results = await asyncio.gather(
            *(_request(client, "tools/list") for client in clients)
        )
        assert [len(result["result"]["tools"]) for result in results] == [8, 8]
        assert serve_lock_held(tmp_path / "data" / "serve.lock")
        endpoint_path = tmp_path / "data" / "stdio-runtime.json"
        endpoint = _read_endpoint(endpoint_path)
        assert endpoint is not None

        async with httpx.AsyncClient(trust_env=False) as http:
            unauthorized = await http.post(f"{endpoint.url}/mcp", json={})
            assert unauthorized.status_code == 401
            cross_site = await http.get(
                f"{endpoint.url}/health",
                headers={"Origin": "https://attacker.invalid"},
            )
            assert cross_site.status_code == 403
            wrong_host = await http.get(
                f"{endpoint.url}/health", headers={"Host": "attacker.invalid"}
            )
            assert wrong_host.status_code == 403

        if kill_first:
            clients[0].kill()
        await _stop(clients[0])
        assert "result" in await _request(clients[1], "ping")
        assert len((await _request(clients[1], "tools/list"))["result"]["tools"]) == 8

        replacement = await _start(environment)
        clients.append(replacement)
        await _initialize(replacement)
        assert _read_endpoint(endpoint_path) == endpoint

        changed_environment = environment | {
            "MUTALAAMCP_CACHE_DIR": str(tmp_path / "different-cache")
        }
        marker = tmp_path / "collision-ready.marker"
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        changed_environment["MUTALAAMCP_RUNTIME_READY_FILE"] = str(marker)
        mismatched = await _start(changed_environment)
        try:
            _, mismatch_error = await asyncio.wait_for(
                mismatched.communicate(), timeout=10
            )
            assert mismatched.returncode == 1
            assert b"already_running" in mismatch_error
            assert marker.read_bytes() == b"ready\n"
            assert "result" in await _request(clients[1], "ping")
        finally:
            await _stop(mismatched)

        # The shared runtime must still enforce authorization before research.
        result = await _request(
            clients[1],
            "tools/call",
            {"name": "belge_getir", "arguments": {"document_id": "invalid"}},
        )
        assert "authentication_required" in json.dumps(result)

        maintenance = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mutalaamcp",
            "cache",
            "clear",
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(maintenance.communicate(), timeout=10)
        assert maintenance.returncode == 1
        assert b"already_running" in error
    finally:
        for client in clients:
            await _stop(client)

    for _ in range(100):
        if not serve_lock_held(tmp_path / "data" / "serve.lock"):
            break
        await asyncio.sleep(0.1)
    assert not serve_lock_held(tmp_path / "data" / "serve.lock")
    assert not (tmp_path / "data" / "stdio-runtime.json").exists()


@pytest.mark.asyncio
async def test_stale_endpoint_recovers_and_acknowledges_runtime_readiness(
    tmp_path: Path,
) -> None:
    from mutalaamcp.stdio_runtime import _write_endpoint

    data = tmp_path / "data"
    data.mkdir()
    endpoint_path = data / "stdio-runtime.json"
    stale = _Endpoint(port=1, token="stale-local-test-token", configuration="stale")
    _write_endpoint(endpoint_path, stale)
    marker = data / "runtime-ready.marker"
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    environment = _environment(tmp_path)
    environment["MUTALAAMCP_RUNTIME_READY_FILE"] = str(marker)
    client = await _start(environment)
    try:
        await _initialize(client)
        assert marker.read_bytes() == b"ready\n"
        assert _read_endpoint(endpoint_path) != stale
    finally:
        await _stop(client)
    for _ in range(100):
        if not serve_lock_held(data / "serve.lock"):
            break
        await asyncio.sleep(0.1)
    assert not serve_lock_held(data / "serve.lock")


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_cache", [False, True])
async def test_failed_backend_does_not_signal_readiness_or_keep_mutation_lock(
    tmp_path: Path,
    failed_cache: bool,
) -> None:
    if failed_cache:
        (tmp_path / "cache").write_text("not a directory", encoding="utf-8")
    marker = tmp_path / "runtime-ready.marker"
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    if not failed_cache:
        marker.write_bytes(b"invalid existing marker")
    environment = _environment(tmp_path)
    environment["MUTALAAMCP_RUNTIME_READY_FILE"] = str(marker)
    client = await _start(environment)
    try:
        _, error = await asyncio.wait_for(client.communicate(), timeout=10)
        assert client.returncode == 1
        assert b"not_configured" in error
        assert marker.read_bytes() == (
            b"" if failed_cache else b"invalid existing marker"
        )
        assert not serve_lock_held(tmp_path / "data" / "serve.lock")
        assert not (tmp_path / "data" / "stdio-runtime.json").exists()
    finally:
        await _stop(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("genuine", [False, True])
async def test_listener_must_prove_identity_without_receiving_its_secret(
    genuine: bool,
) -> None:
    endpoint = _Endpoint(port=18769, token="private-token", configuration="test")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        assert endpoint.token not in str(request.url)
        nonce = request.url.params["nonce"]
        proof = _proof(endpoint.token, nonce) if genuine else "forged-proof"
        return httpx.Response(200, json={"proof": proof})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _probe(client, endpoint) is genuine


@pytest.mark.asyncio
async def test_backend_exits_if_startup_parent_disappears(tmp_path: Path) -> None:
    from mutalaamcp.settings import Settings
    from mutalaamcp.stdio_runtime import _spawn_backend

    settings = Settings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        model_dir=tmp_path / "models",
        auth_base_url="https://mutalaa.invalid",
        terms_base_url="https://mutalaa.invalid/terms",
    )
    child = _spawn_backend(settings)
    try:
        for _ in range(100):
            if (settings.data_dir / "stdio-runtime.json").exists():
                break
            await asyncio.sleep(0.05)
        assert serve_lock_held(settings.serve_lock_path)
        assert child.stdin is not None
        child.stdin.close()  # Same EOF as an abruptly killed startup frontend.
        assert await asyncio.to_thread(child.wait, timeout=5) == 1
        assert not serve_lock_held(settings.serve_lock_path)
    finally:
        if child.poll() is None:
            child.kill()
            await asyncio.to_thread(child.wait)


@pytest.mark.asyncio
async def test_legacy_selector_validates_runtime_before_detaching_cache_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mutalaamcp.settings import Settings
    from mutalaamcp.stdio_runtime import _connection

    settings = Settings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        model_dir=tmp_path / "models",
    )
    marker = tmp_path / "runtime-ready.marker"
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    monkeypatch.setenv("MUTALAAMCP_RUNTIME_READY_FILE", str(marker))

    def fail_to_spawn(_settings: Settings) -> None:
        assert settings.cache_db_path.exists()
        assert not serve_lock_held(settings.serve_lock_path)
        assert marker.read_bytes() == b"ready\n"
        raise OSError("test spawn failure")

    monkeypatch.setattr("mutalaamcp.stdio_runtime._spawn_backend", fail_to_spawn)
    with pytest.raises(OSError, match="test spawn failure"):
        async with _connection(settings):
            pytest.fail("No connection can be returned without a backend")
    assert not serve_lock_held(settings.serve_lock_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes and ownership")
def test_endpoint_refuses_symlinks_and_world_readable_credentials(
    tmp_path: Path,
) -> None:
    path = tmp_path / "stdio-runtime.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="kullanıcıya özel"):
        _read_endpoint(path)
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(RuntimeError, match="kullanıcıya özel"):
        _read_endpoint(link)


@pytest.mark.asyncio
async def test_stdio_clients_share_token_refresh_and_preserve_tool_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keyring

    from mutalaamcp.auth.activation import ActivationRecord
    from mutalaamcp.auth.credentials import CredentialStore
    from mutalaamcp.auth.state import AuthState
    from mutalaamcp.settings import Settings
    from mutalaamcp.stdio_runtime import _connection, _Leases, _serve_backend
    from mutalaamcp.tools import HttpClients

    settings = Settings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        model_dir=tmp_path / "models",
        auth_base_url="https://mutalaa.invalid",
        terms_base_url="https://mutalaa.invalid/terms",
    )
    passwords: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        keyring, "get_password", lambda service, user: passwords.get((service, user))
    )
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda service, user, value: passwords.__setitem__((service, user), value),
    )
    monkeypatch.setattr(
        keyring,
        "delete_password",
        lambda service, user: passwords.pop((service, user), None),
    )
    credentials = CredentialStore(auth_base_url=settings.auth_base_url)
    credentials.replace_session("test-access", "test-refresh")
    AuthState(settings.data_dir).write_activated(
        ActivationRecord(
            status="active",
            plan="mcp_local_free",
            features=("local_research",),
            checked_at=datetime.now(UTC).isoformat(),
        )
    )
    refreshes = 0
    runtimes = 0

    async def auth_request(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes
        if request.method == "POST":
            refreshes += 1
            assert b"test-refresh" in request.content
            await asyncio.sleep(0.2)
            return httpx.Response(
                200,
                json={
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                },
            )
        assert request.url.path == "/api/mcp/v1/announcement"
        return httpx.Response(200, json={"announcement": None})

    def research_request(_request: httpx.Request) -> httpx.Response:
        pytest.fail("No external research request should be made by this test")

    def http_clients(_settings: Settings) -> HttpClients:
        nonlocal runtimes
        runtimes += 1
        return HttpClients(
            auth=httpx.AsyncClient(transport=httpx.MockTransport(auth_request)),
            research=httpx.AsyncClient(transport=httpx.MockTransport(research_request)),
        )

    monkeypatch.setattr("mutalaamcp.tools.create_http_clients", http_clients)
    leases = _Leases()
    lock = FileLock(settings.serve_lock_path).acquire()
    backend = asyncio.create_task(_serve_backend(settings, leases))
    clients: list[asyncio.subprocess.Process] = []
    try:
        for _ in range(100):
            if (settings.data_dir / "stdio-runtime.json").exists():
                break
            await asyncio.sleep(0.05)
        # Keep the in-process test backend alive during frontend startup. Its
        # HTTP/keyring dependencies are mocked; both stdio clients are real.
        async with _connection(settings):
            clients = [await _start(_environment(tmp_path)) for _ in range(2)]
            initialized = await asyncio.gather(
                *(_initialize(client) for client in clients), return_exceptions=True
            )
            for result in initialized:
                if isinstance(result, BaseException):
                    raise result
            results = await asyncio.gather(
                *(
                    _request(
                        client,
                        "tools/call",
                        {
                            "name": "turk_hukuku_sorularinda_once_bu_araci_cagir",
                            "arguments": {
                                "soru": "Kiracının gider sorumluluğu",
                                "skill_durumu": "installed",
                            },
                        },
                    )
                    for client in clients
                )
            )
            for result in results:
                payload = result["result"]["structuredContent"]
                assert payload["ok"] is True
                assert payload["research_performed"] is False
                assert "companion_skill" not in payload
            assert runtimes == 1
            assert refreshes == 1
            assert credentials.load_refresh_token() == "rotated-refresh"
    finally:
        for client in clients:
            await _stop(client)
        try:
            await asyncio.wait_for(backend, timeout=10)
        finally:
            lock.release()
            if leases.shutdown_lock is not None:
                leases.shutdown_lock.release()
            (settings.data_dir / "stdio-runtime.json").unlink(missing_ok=True)
