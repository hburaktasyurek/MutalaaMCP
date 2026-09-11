"""Behavioral auth tests. No live network, OS keyring, or user directories."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from keyring.errors import PasswordDeleteError

from mutalaamcp import __version__
from mutalaamcp.auth import (
    DEVICE_CODE_GRANT_TYPE,
    KEYRING_SERVICE,
    ActivationError,
    ActivationProtocolError,
    ActivationRecord,
    AuthenticationRequired,
    AuthSession,
    DeviceAuthorization,
    DeviceFlow,
    DeviceFlowError,
    SessionExpired,
    TokenSet,
    activation_error_message,
)
from mutalaamcp.auth.credentials import CredentialStore
from mutalaamcp.auth.state import AuthState
from mutalaamcp.domain.errors import ErrorCode

AUTH_BASE = "https://mutalaa.test"
TERMS_BASE = "https://mutalaa.test/terms"
ALLOWED_TERMS = "https://mutalaa.test/terms"
ACCESS_TOKEN = "at_memory_only_9f3c7e21"
REFRESH_TOKEN = "rt_keyring_only_1a2b4c"
ROTATED_ACCESS = "at_rotated_memory_77aa"
ROTATED_REFRESH = "rt_rotated_keyring_88bb"
DEVICE_CODE = "dc_secret_must_not_persist"
USER_CODE = "WDJB-MJHT"
VERIFICATION_URI = "https://mutalaa.test/mcp/activate"
VERIFICATION_COMPLETE = "https://mutalaa.test/mcp/activate?user_code=WDJB-MJHT"
ACTIVE_CONTRACT = json.loads(
    (Path(__file__).parents[1] / "contracts/mcp-activation/v1/active.json").read_text()
)
TERMS_REQUIRED_CONTRACT = json.loads(
    (
        Path(__file__).parents[1] / "contracts/mcp-activation/v1/terms-required.json"
    ).read_text()
)
CHECKED_AT = ACTIVE_CONTRACT["checked_at"]
PII_EMAIL = "jane.doe@example.com"
PII_NAME = "Jane Doe"
PII_SUB = "user_12345"

ACTIVE_BODY = {
    **ACTIVE_CONTRACT,
    "email": PII_EMAIL,
    "name": PII_NAME,
    "sub": PII_SUB,
}

AUTHORIZE_BODY = {
    "device_code": DEVICE_CODE,
    "user_code": USER_CODE,
    "verification_uri": VERIFICATION_URI,
    "verification_uri_complete": VERIFICATION_COMPLETE,
    "expires_in": 600,
    "interval": 5,
}

TOKENS_BODY = {
    "access_token": ACCESS_TOKEN,
    "refresh_token": REFRESH_TOKEN,
    "expires_in": 3600,
}

SECRETS = (
    ACCESS_TOKEN,
    ROTATED_ACCESS,
    DEVICE_CODE,
    PII_EMAIL,
    PII_NAME,
    PII_SUB,
)


class InMemoryKeyring:
    """Dict-backed keyring. Never touches the OS keyring."""

    def __init__(self) -> None:
        self.passwords: dict[tuple[str, str], str] = {}
        self.delete_error: BaseException | None = None
        self.before_delete: Any = None

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.before_delete is not None:
            self.before_delete()
        if self.delete_error is not None:
            raise self.delete_error
        key = (service, username)
        if key not in self.passwords:
            raise PasswordDeleteError("not found")
        del self.passwords[key]


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise AssertionError(f"negative sleep {seconds}")
        self.sleeps.append(seconds)
        self.now += seconds


class AuthHttp:
    """httpx.MockTransport handler for Mütalaa device auth and activation."""

    def __init__(self) -> None:
        self.authorize_status = 200
        self.authorize_body: Any = dict(AUTHORIZE_BODY)
        self.poll: list[tuple[int, dict[str, Any]]] = []
        self.authenticate_headers: dict[str, str] = {}
        self.authenticate_text: str | None = None
        self.activate_status = 200
        self.activate_body: Any = dict(ACTIVE_BODY)
        self.activate_headers: dict[str, str] = {}
        self.activate_text: str | None = None
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/mcp/v1/device/authorize":
            return httpx.Response(self.authorize_status, json=self.authorize_body)
        if path == "/api/mcp/v1/device/authenticate":
            if not self.poll:
                raise AssertionError(f"unexpected authenticate: {request.content!r}")
            status, body = self.poll.pop(0)
            if self.authenticate_text is not None:
                return httpx.Response(
                    status,
                    text=self.authenticate_text,
                    headers=self.authenticate_headers,
                )
            return httpx.Response(status, json=body, headers=self.authenticate_headers)
        if path == "/api/mcp/v1/activate":
            if self.activate_text is not None:
                return httpx.Response(
                    self.activate_status,
                    text=self.activate_text,
                    headers=self.activate_headers,
                )
            return httpx.Response(
                self.activate_status,
                json=self.activate_body,
                headers=self.activate_headers,
            )
        raise AssertionError(f"unexpected {request.method} {request.url}")


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> InMemoryKeyring:
    backend = InMemoryKeyring()
    monkeypatch.setattr("mutalaamcp.auth.credentials.keyring", backend)
    return backend


def _http(handler: AuthHttp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0)


def _credentials() -> CredentialStore:
    return CredentialStore(auth_base_url=AUTH_BASE)


def _session(http: httpx.AsyncClient, data_dir: Path, clock: FakeClock) -> AuthSession:
    return AuthSession(
        http,
        auth_base_url=AUTH_BASE,
        allowed_terms_base_url=TERMS_BASE,
        clock=clock.time,
        sleep=clock.sleep,
        data_dir=data_dir,
        credentials=_credentials(),
        state=AuthState(data_dir),
    )


def _flow(http: httpx.AsyncClient, clock: FakeClock) -> DeviceFlow:
    return DeviceFlow(
        http,
        auth_base_url=AUTH_BASE,
        clock=clock.time,
        sleep=clock.sleep,
    )


class GateDeviceFlow:
    """Refresh dependency that proves gate tests make no provider HTTP calls."""

    def __init__(self, outcome: TokenSet | BaseException) -> None:
        self._outcome = outcome
        self.refresh_tokens: list[str] = []

    async def refresh(self, refresh_token: str) -> TokenSet:
        self.refresh_tokens.append(refresh_token)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class BlockingRefreshDeviceFlow:
    """Refresh dependency with caller-controlled response order."""

    def __init__(self, outcomes: list[TokenSet | BaseException]) -> None:
        self._outcomes = outcomes
        self.refresh_tokens: list[str] = []
        self.started = [asyncio.Event() for _ in outcomes]
        self.release = [asyncio.Event() for _ in outcomes]

    async def refresh(self, refresh_token: str) -> TokenSet:
        index = len(self.refresh_tokens)
        self.refresh_tokens.append(refresh_token)
        self.started[index].set()
        await self.release[index].wait()
        outcome = self._outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class GateActivationClient:
    """Activation dependency for authorization-gate behavior."""

    def __init__(self, outcome: ActivationRecord | BaseException) -> None:
        self._outcome = outcome
        self.access_tokens: list[str] = []

    async def activate(self, access_token: str) -> ActivationRecord:
        self.access_tokens.append(access_token)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _gate_http() -> httpx.AsyncClient:
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected gate HTTP request: {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request))


def _gate_session(
    http: httpx.AsyncClient,
    data_dir: Path,
    clock: FakeClock,
    *,
    device_flow: Any,
    activation_client: Any,
) -> AuthSession:
    return AuthSession(
        http,
        auth_base_url=AUTH_BASE,
        allowed_terms_base_url=TERMS_BASE,
        clock=clock.time,
        sleep=clock.sleep,
        data_dir=data_dir,
        credentials=_credentials(),
        state=AuthState(data_dir),
        device_flow=device_flow,
        activation_client=activation_client,
    )


def _active_record(*, checked_at: str = CHECKED_AT) -> ActivationRecord:
    return ActivationRecord.model_validate({**ACTIVE_BODY, "checked_at": checked_at})


def _refresh_tokens() -> TokenSet:
    return TokenSet(
        access_token=ROTATED_ACCESS,
        refresh_token=ROTATED_REFRESH,
        expires_in=3600,
    )


def _clock_after(checked_at: str, *, hours: float) -> FakeClock:
    return FakeClock(
        now=(datetime.fromisoformat(checked_at) + timedelta(hours=hours)).timestamp()
    )


def _authorization(**overrides: Any) -> DeviceAuthorization:
    payload = {
        "device_code": DEVICE_CODE,
        "user_code": USER_CODE,
        "verification_uri": VERIFICATION_URI,
        "verification_uri_complete": VERIFICATION_COMPLETE,
        "expires_in": 10,
        "interval": 5,
        **overrides,
    }
    return DeviceAuthorization.model_validate(payload)


def _form(request: httpx.Request) -> dict[str, str]:
    return {
        key: values[0] for key, values in parse_qs(request.content.decode()).items()
    }


def _state_path(data_dir: Path) -> Path:
    return data_dir / "auth_state.json"


def _persisted_text(data_dir: Path) -> str:
    chunks: list[str] = []
    for path in data_dir.rglob("*"):
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def assert_no_persisted_secrets(
    data_dir: Path,
    keyring: InMemoryKeyring,
    *,
    allowed_refresh: str | None = None,
) -> None:
    blob = _persisted_text(data_dir)
    for secret in SECRETS:
        assert secret not in blob
    assert "access_token" not in blob
    assert DEVICE_CODE not in blob
    if _state_path(data_dir).is_file():
        payload = json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
        assert set(payload) <= {
            "status",
            "plan",
            "features",
            "checked_at",
            "generation",
        }
        generation = payload.pop("generation")
        assert isinstance(generation, int) and not isinstance(generation, bool)
        assert generation >= 1
        if payload.get("status") == "active":
            assert payload == {
                "status": "active",
                "plan": "mcp_local_free",
                "features": ["local_research"],
                "checked_at": CHECKED_AT,
            }
        elif payload.get("status") == "logged_out":
            assert payload == {"status": "logged_out"}
    expected_username = _credentials().keyring_username
    for (service, username), value in keyring.passwords.items():
        assert service == KEYRING_SERVICE
        assert username == expected_username
        for secret in SECRETS:
            assert secret not in value
        if allowed_refresh is not None:
            assert value == allowed_refresh
    if allowed_refresh is None:
        assert keyring.passwords == {}
    else:
        assert keyring.passwords == {
            (KEYRING_SERVICE, expected_username): allowed_refresh
        }


def test_refresh_tokens_are_isolated_by_normalized_auth_origin(
    fake_keyring: InMemoryKeyring,
) -> None:
    production = CredentialStore(auth_base_url="https://MUTALAA.test:443/")
    same_deployment = CredentialStore(auth_base_url=AUTH_BASE)
    other_deployment = CredentialStore(auth_base_url="https://mutalaa.other.test")

    production.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)

    assert production.keyring_username == same_deployment.keyring_username
    assert other_deployment.keyring_username != production.keyring_username
    assert other_deployment.load_refresh_token() is None
    assert fake_keyring.passwords == {
        (KEYRING_SERVICE, production.keyring_username): REFRESH_TOKEN
    }


def _oauth_error(error: str, description: str | None = None) -> dict[str, str]:
    payload = {"error": error}
    if description is not None:
        payload["error_description"] = description
    return payload


async def _noop_authorization(authorization: DeviceAuthorization) -> None:
    assert authorization.user_code == USER_CODE


@pytest.mark.asyncio
async def test_device_start_pending_slow_down_then_success(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    handler = AuthHttp()
    handler.poll = [
        (400, _oauth_error("authorization_pending")),
        (400, _oauth_error("slow_down")),
        (200, TOKENS_BODY),
    ]
    clock = FakeClock()
    presented: list[DeviceAuthorization] = []

    async def on_authorization(authorization: DeviceAuthorization) -> None:
        presented.append(authorization)

    async with _http(handler) as http:
        session = _session(http, tmp_path, clock)
        record = await session.login(on_authorization)

    assert presented[0].user_code == USER_CODE
    assert DEVICE_CODE not in repr(presented[0])
    assert DEVICE_CODE not in str(presented[0])
    assert clock.sleeps == [5.0, 5.0, 10.0]
    assert session.access_token() == ACCESS_TOKEN
    assert record.status == "active"
    assert record.plan == "mcp_local_free"
    assert record.features == ("local_research",)
    assert (
        json.loads(_state_path(tmp_path).read_text(encoding="utf-8"))["status"]
        == "active"
    )

    authorize, first_poll, slow_down, success = handler.requests[:4]
    assert authorize.method == "POST"
    assert json.loads(authorize.content) == {"client_version": __version__}
    assert _form(first_poll) == {
        "grant_type": DEVICE_CODE_GRANT_TYPE,
        "device_code": DEVICE_CODE,
    }
    assert _form(slow_down)["grant_type"] == DEVICE_CODE_GRANT_TYPE
    assert _form(success)["device_code"] == DEVICE_CODE
    assert_no_persisted_secrets(tmp_path, fake_keyring, allowed_refresh=REFRESH_TOKEN)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "url"),
    [
        ("verification_uri", "https://api.workos.com/activate"),
        (
            "verification_uri_complete",
            "https://api.workos.com/activate?user_code=WDJB-MJHT",
        ),
    ],
)
async def test_authorize_rejects_verification_url_outside_mutalaa_origin(
    field: str, url: str
) -> None:
    handler = AuthHttp()
    handler.authorize_body = {**AUTHORIZE_BODY, field: url}
    clock = FakeClock()

    async with _http(handler) as http:
        with pytest.raises(DeviceFlowError) as caught:
            await _flow(http, clock).authorize()

    assert caught.value.error == "invalid_response"
    assert caught.value.http_status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("html", "json", "timeout"))
async def test_poll_recovers_from_transient_failure_without_new_device_code(
    failure: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["accept"] == "application/json"
        if len(requests) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("temporary", request=request)
            if failure == "html":
                return httpx.Response(502, text="<html>Bad gateway</html>")
            return httpx.Response(503, json={"error": "temporarily_unavailable"})
        return httpx.Response(200, json=TOKENS_BODY)

    clock = FakeClock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        tokens = await _flow(http, clock).poll(_authorization(expires_in=60))

    assert tokens.access_token == ACCESS_TOKEN
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert clock.sleeps == [5.0, 10.0]


@pytest.mark.asyncio
async def test_poll_transient_failure_still_respects_device_deadline() -> None:
    clock = FakeClock(now=0.0)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(502, text="<html>Bad gateway</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DeviceFlowError) as caught:
            await _flow(http, clock).poll(_authorization(expires_in=12, interval=5))

    assert caught.value.error == "expired_token"
    assert clock.sleeps == [5.0, 7.0]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_poll_expires_at_exact_deadline() -> None:
    handler = AuthHttp()
    handler.poll = [
        (400, _oauth_error("authorization_pending")),
        (400, _oauth_error("authorization_pending")),
        (200, TOKENS_BODY),
    ]
    clock = FakeClock(now=0.0)
    async with _http(handler) as http:
        flow = _flow(http, clock)
        with pytest.raises(DeviceFlowError) as caught:
            await flow.poll(_authorization(expires_in=10, interval=5))
    assert caught.value.error == "expired_token"
    assert caught.value.description == "Cihaz kodunun süresi doldu"
    assert clock.now == 10.0
    assert clock.sleeps == [5.0, 5.0]
    assert len(handler.requests) == 1


@pytest.mark.asyncio
async def test_poll_sleeps_remaining_time_before_deadline() -> None:
    handler = AuthHttp()
    handler.poll = [
        (400, _oauth_error("authorization_pending")),
        (400, _oauth_error("authorization_pending")),
        (200, TOKENS_BODY),
    ]
    clock = FakeClock(now=0.0)
    async with _http(handler) as http:
        with pytest.raises(DeviceFlowError) as caught:
            await _flow(http, clock).poll(_authorization(expires_in=7, interval=5))
    assert caught.value.error == "expired_token"
    assert clock.sleeps == [5.0, 2.0]
    assert clock.now == 7.0
    assert len(handler.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "description", "exc_type"),
    [
        ("access_denied", "denied by user", DeviceFlowError),
        ("expired_token", "server expired", DeviceFlowError),
        ("invalid_grant", "grant gone", SessionExpired),
    ],
)
async def test_poll_terminal_oauth_errors(
    error: str, description: str, exc_type: type[DeviceFlowError]
) -> None:
    handler = AuthHttp()
    handler.poll = [(400, _oauth_error(error, description))]
    clock = FakeClock()
    async with _http(handler) as http:
        with pytest.raises(exc_type) as caught:
            await _flow(http, clock).poll(_authorization())
    assert caught.value.error == error
    assert caught.value.description == description
    assert clock.sleeps == [5.0]
    assert len(handler.poll) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("poll", "refresh"))
async def test_malformed_successful_token_payload_is_invalid_response(
    operation: str,
) -> None:
    handler = AuthHttp()
    handler.poll = [(200, {"access_token": ACCESS_TOKEN})]
    clock = FakeClock()
    async with _http(handler) as http:
        flow = _flow(http, clock)
        with pytest.raises(DeviceFlowError) as caught:
            if operation == "poll":
                await flow.poll(_authorization())
            else:
                await flow.refresh(REFRESH_TOKEN)

    assert caught.value.error == "invalid_response"
    assert (
        caught.value.description
        == "Belirteç yanıtı access_token, refresh_token ve expires_in içermelidir"
    )
    assert caught.value.http_status == 200


@pytest.mark.asyncio
async def test_refresh_rotates_refresh_token_access_token_stays_in_memory(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    handler = AuthHttp()
    handler.poll = [
        (
            200,
            {
                "access_token": ROTATED_ACCESS,
                "refresh_token": ROTATED_REFRESH,
                "expires_in": 3600,
            },
        )
    ]
    clock = FakeClock()
    async with _http(handler) as http:
        session = _session(http, tmp_path, clock)
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        token = await session.refresh()
    assert token == ROTATED_ACCESS
    assert session.access_token() == ROTATED_ACCESS
    assert _form(handler.requests[0]) == {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
    }
    assert ACCESS_TOKEN not in fake_keyring.passwords.values()
    assert ROTATED_ACCESS not in fake_keyring.passwords.values()
    assert_no_persisted_secrets(tmp_path, fake_keyring, allowed_refresh=ROTATED_REFRESH)


@pytest.mark.asyncio
async def test_refresh_invalid_grant_deletes_credentials(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    handler = AuthHttp()
    handler.poll = [(400, _oauth_error("invalid_grant", "revoked"))]
    clock = FakeClock()
    async with _http(handler) as http:
        session = _session(http, tmp_path, clock)
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        with pytest.raises(SessionExpired) as caught:
            await session.refresh()
    assert caught.value.error == "invalid_grant"
    assert caught.value.http_status == 400
    assert session.access_token() is None
    assert fake_keyring.passwords == {}
    assert_no_persisted_secrets(tmp_path, fake_keyring)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "headers"),
    [
        pytest.param(
            500,
            "<html><title>upstream failure</title></html>",
            {"content-type": "text/html"},
            id="500-html",
        ),
        pytest.param(
            503,
            "",
            {"content-type": "text/plain"},
            id="503-empty",
        ),
        pytest.param(
            503,
            '{"error":"invalid_grant","error_description":"not a grant failure"}',
            {"content-type": "text/html"},
            id="503-mislabelled-invalid-grant",
        ),
    ],
)
async def test_prior_active_session_grace_uses_refresh_http_status(
    tmp_path: Path,
    fake_keyring: InMemoryKeyring,
    status: int,
    body: str,
    headers: dict[str, str],
) -> None:
    state = AuthState(tmp_path)
    state.write_activated(_active_record())
    handler = AuthHttp()
    handler.authenticate_text = body
    handler.authenticate_headers = headers
    handler.poll = [(status, {}), (status, {})]
    clock = _clock_after(CHECKED_AT, hours=23)

    async with _http(handler) as http:
        with pytest.raises(DeviceFlowError) as caught:
            await _flow(http, clock).refresh(REFRESH_TOKEN)
        assert caught.value.http_status == status
        assert not isinstance(caught.value, SessionExpired)

        session = _session(http, tmp_path, clock)
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        await session.ensure_authorized()

    assert len(handler.requests) == 2
    assert session.access_token() == ACCESS_TOKEN
    assert_no_persisted_secrets(tmp_path, fake_keyring, allowed_refresh=REFRESH_TOKEN)


@pytest.mark.asyncio
async def test_logout_wins_in_flight_refresh(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    device_flow = BlockingRefreshDeviceFlow([_refresh_tokens()])
    activation_client = GateActivationClient(_active_record())
    clock = FakeClock()

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=device_flow,
            activation_client=activation_client,
        )
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        refresh_task = asyncio.create_task(session.refresh())
        await device_flow.started[0].wait()

        logout = await session.logout()
        device_flow.release[0].set()

        with pytest.raises(AuthenticationRequired):
            await refresh_task

    assert logout.cleanup_error is None
    assert session.is_logged_out()
    assert session.access_token() is None
    assert fake_keyring.passwords == {}
    assert_no_persisted_secrets(tmp_path, fake_keyring)


@pytest.mark.asyncio
async def test_concurrent_refresh_invalid_grant_shares_expiration(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    device_flow = BlockingRefreshDeviceFlow(
        [SessionExpired("revoked", http_status=400)]
    )
    activation_client = GateActivationClient(_active_record())
    clock = FakeClock()

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=device_flow,
            activation_client=activation_client,
        )
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        first_refresh = asyncio.create_task(session.refresh())
        await device_flow.started[0].wait()
        second_refresh = asyncio.create_task(session.refresh())
        await asyncio.sleep(0)
        assert device_flow.refresh_tokens == [REFRESH_TOKEN]

        device_flow.release[0].set()
        outcomes = await asyncio.gather(
            first_refresh, second_refresh, return_exceptions=True
        )

    assert all(isinstance(outcome, SessionExpired) for outcome in outcomes)
    assert outcomes[0] is outcomes[1]
    assert session.access_token() is None
    assert_no_persisted_secrets(tmp_path, fake_keyring)


@pytest.mark.asyncio
async def test_late_invalid_grant_adopts_newer_rotated_credentials(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    newer_refresh_token = "rt_newer_rotation"
    device_flow = BlockingRefreshDeviceFlow(
        [SessionExpired("old token", http_status=400), _refresh_tokens()]
    )
    activation_client = GateActivationClient(_active_record())
    clock = FakeClock()

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=device_flow,
            activation_client=activation_client,
        )
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        expired_refresh = asyncio.create_task(session.refresh())
        await device_flow.started[0].wait()

        session._credentials.replace_session("at_newer_rotation", newer_refresh_token)
        rotated_refresh = asyncio.create_task(session.refresh())
        await device_flow.started[1].wait()

        device_flow.release[1].set()
        assert await rotated_refresh == ROTATED_ACCESS
        device_flow.release[0].set()
        assert await expired_refresh == ROTATED_ACCESS

    assert device_flow.refresh_tokens == [REFRESH_TOKEN, newer_refresh_token]
    assert session.access_token() == ROTATED_ACCESS
    assert_no_persisted_secrets(tmp_path, fake_keyring, allowed_refresh=ROTATED_REFRESH)


@pytest.mark.asyncio
async def test_concurrent_refresh_success_coalesces_rotation(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    device_flow = BlockingRefreshDeviceFlow([_refresh_tokens()])
    activation_client = GateActivationClient(_active_record())
    clock = FakeClock()

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=device_flow,
            activation_client=activation_client,
        )
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        first_refresh = asyncio.create_task(session.refresh())
        await device_flow.started[0].wait()
        second_refresh = asyncio.create_task(session.refresh())
        await asyncio.sleep(0)
        assert device_flow.refresh_tokens == [REFRESH_TOKEN]

        device_flow.release[0].set()
        assert await first_refresh == ROTATED_ACCESS
        assert await second_refresh == ROTATED_ACCESS

    assert session.access_token() == ROTATED_ACCESS
    assert_no_persisted_secrets(tmp_path, fake_keyring, allowed_refresh=ROTATED_REFRESH)


@pytest.mark.asyncio
async def test_ensure_authorized_rejects_logged_out_state(tmp_path: Path) -> None:
    state = AuthState(tmp_path)
    state.write_logged_out()
    device_flow = GateDeviceFlow(_refresh_tokens())
    activation_client = GateActivationClient(_active_record())
    clock = FakeClock()

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=device_flow,
            activation_client=activation_client,
        )
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        with pytest.raises(AuthenticationRequired):
            await session.ensure_authorized()

    assert device_flow.refresh_tokens == []
    assert activation_client.access_tokens == []


@pytest.mark.asyncio
async def test_ensure_authorized_rejects_missing_credentials(tmp_path: Path) -> None:
    device_flow = GateDeviceFlow(_refresh_tokens())
    activation_client = GateActivationClient(_active_record())
    clock = FakeClock()

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=device_flow,
            activation_client=activation_client,
        )
        with pytest.raises(AuthenticationRequired):
            await session.ensure_authorized()

    assert device_flow.refresh_tokens == []
    assert activation_client.access_tokens == []


@pytest.mark.asyncio
async def test_ensure_authorized_propagates_refresh_invalid_grant(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    state = AuthState(tmp_path)
    state.write_activated(_active_record())
    device_flow = GateDeviceFlow(SessionExpired("revoked", http_status=400))
    activation_client = GateActivationClient(_active_record())
    clock = _clock_after(CHECKED_AT, hours=23)

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=device_flow,
            activation_client=activation_client,
        )
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        with pytest.raises(SessionExpired) as caught:
            await session.ensure_authorized()

    assert caught.value.error == "invalid_grant"
    assert device_flow.refresh_tokens == [REFRESH_TOKEN]
    assert activation_client.access_tokens == []
    assert session.access_token() is None
    assert_no_persisted_secrets(tmp_path, fake_keyring)


@pytest.mark.asyncio
async def test_ensure_authorized_skips_fresh_activation(
    tmp_path: Path,
) -> None:
    state = AuthState(tmp_path)
    state.write_activated(_active_record())
    device_flow = GateDeviceFlow(_refresh_tokens())
    activation_client = GateActivationClient(_active_record())
    clock = _clock_after(CHECKED_AT, hours=23)

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=device_flow,
            activation_client=activation_client,
        )
        session._credentials.replace_session(
            ACCESS_TOKEN,
            REFRESH_TOKEN,
            access_token_expires_at=clock.time() + 3600,
        )
        await session.ensure_authorized()
        await session.ensure_authorized()

    assert device_flow.refresh_tokens == []
    assert activation_client.access_tokens == []
    local_state = session.local_state()
    assert local_state is not None
    assert local_state.checked_at == CHECKED_AT


@pytest.mark.asyncio
async def test_ensure_authorized_refreshes_at_access_token_expiry_skew(
    tmp_path: Path,
) -> None:
    state = AuthState(tmp_path)
    state.write_activated(_active_record())
    device_flow = GateDeviceFlow(_refresh_tokens())
    activation_client = GateActivationClient(_active_record())
    clock = _clock_after(CHECKED_AT, hours=23)

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=device_flow,
            activation_client=activation_client,
        )
        session._credentials.replace_session(
            ACCESS_TOKEN,
            REFRESH_TOKEN,
            access_token_expires_at=clock.time() + 30,
        )
        await session.ensure_authorized()

    assert device_flow.refresh_tokens == [REFRESH_TOKEN]
    assert activation_client.access_tokens == []


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed_hours", [24, 25])
async def test_ensure_authorized_rechecks_activation_at_24_hours(
    tmp_path: Path, elapsed_hours: int
) -> None:
    rechecked_at = "2026-09-03T20:00:00+00:00"
    state = AuthState(tmp_path)
    state.write_activated(_active_record())
    device_flow = GateDeviceFlow(_refresh_tokens())
    activation_client = GateActivationClient(_active_record(checked_at=rechecked_at))
    clock = _clock_after(CHECKED_AT, hours=elapsed_hours)

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=device_flow,
            activation_client=activation_client,
        )
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        await session.ensure_authorized()

    assert device_flow.refresh_tokens == [REFRESH_TOKEN]
    assert activation_client.access_tokens == [ROTATED_ACCESS]
    local_state = session.local_state()
    assert local_state is not None
    assert local_state.checked_at == rechecked_at


@pytest.mark.asyncio
async def test_ensure_authorized_fails_first_activation_network_error(
    tmp_path: Path,
) -> None:
    device_flow = GateDeviceFlow(_refresh_tokens())
    activation_client = GateActivationClient(httpx.ConnectError("offline"))
    clock = FakeClock()

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=device_flow,
            activation_client=activation_client,
        )
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        with pytest.raises(httpx.ConnectError):
            await session.ensure_authorized()

    assert device_flow.refresh_tokens == [REFRESH_TOKEN]
    assert activation_client.access_tokens == [ROTATED_ACCESS]
    assert session.local_state() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refresh_failure",
    [
        pytest.param(httpx.ConnectError("offline"), id="network"),
        pytest.param(DeviceFlowError("server_error", http_status=503), id="server-503"),
    ],
)
async def test_ensure_authorized_grants_prior_active_session_refresh_grace(
    tmp_path: Path,
    fake_keyring: InMemoryKeyring,
    refresh_failure: BaseException,
) -> None:
    state = AuthState(tmp_path)
    state.write_activated(_active_record())
    device_flow = GateDeviceFlow(refresh_failure)
    activation_client = GateActivationClient(_active_record())
    clock = _clock_after(CHECKED_AT, hours=23)

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=device_flow,
            activation_client=activation_client,
        )
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        await session.ensure_authorized()

    assert device_flow.refresh_tokens == [REFRESH_TOKEN]
    assert activation_client.access_tokens == []
    assert session.access_token() == ACCESS_TOKEN
    assert_no_persisted_secrets(tmp_path, fake_keyring, allowed_refresh=REFRESH_TOKEN)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "activation_failure",
    [
        pytest.param(httpx.ConnectError("offline"), id="network"),
        pytest.param(
            ActivationError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "offline",
                http_status=503,
            ),
            id="server-error",
        ),
    ],
)
async def test_ensure_authorized_grants_prior_active_session_grace(
    tmp_path: Path, activation_failure: BaseException
) -> None:
    state = AuthState(tmp_path)
    state.write_activated(_active_record())
    device_flow = GateDeviceFlow(_refresh_tokens())
    activation_client = GateActivationClient(activation_failure)
    clock = _clock_after(CHECKED_AT, hours=24)

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=device_flow,
            activation_client=activation_client,
        )
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        await session.ensure_authorized()

    assert device_flow.refresh_tokens == [REFRESH_TOKEN]
    assert activation_client.access_tokens == [ROTATED_ACCESS]
    local_state = session.local_state()
    assert local_state is not None
    assert local_state.checked_at == CHECKED_AT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("activation_failure", "expected_code", "expected_terms_url"),
    [
        pytest.param(
            ActivationError(
                ErrorCode.ACCOUNT_DISABLED,
                "disabled",
                http_status=403,
            ),
            ErrorCode.ACCOUNT_DISABLED,
            None,
            id="account-disabled",
        ),
        pytest.param(
            ActivationError(
                ErrorCode.TERMS_REQUIRED,
                "accept terms",
                http_status=403,
                terms_url=ALLOWED_TERMS,
            ),
            ErrorCode.TERMS_REQUIRED,
            ALLOWED_TERMS,
            id="terms-required",
        ),
        pytest.param(
            ActivationError(
                ErrorCode.ACCOUNT_LINK_REQUIRED,
                "link account",
                http_status=409,
            ),
            ErrorCode.ACCOUNT_LINK_REQUIRED,
            None,
            id="account-link-required",
        ),
    ],
)
async def test_ensure_authorized_propagates_explicit_activation_errors(
    tmp_path: Path,
    activation_failure: ActivationError,
    expected_code: ErrorCode,
    expected_terms_url: str | None,
) -> None:
    state = AuthState(tmp_path)
    state.write_activated(_active_record())
    device_flow = GateDeviceFlow(_refresh_tokens())
    activation_client = GateActivationClient(activation_failure)
    clock = _clock_after(CHECKED_AT, hours=24)

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=device_flow,
            activation_client=activation_client,
        )
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        with pytest.raises(ActivationError) as caught:
            await session.ensure_authorized()

    assert caught.value is activation_failure
    assert caught.value.code is expected_code
    assert caught.value.terms_url == expected_terms_url
    assert device_flow.refresh_tokens == [REFRESH_TOKEN]
    assert activation_client.access_tokens == [ROTATED_ACCESS]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ErrorCode.SESSION_EXPIRED, "Oturumun süresi doldu veya oturum geçersiz."),
        (ErrorCode.EMAIL_VERIFICATION_REQUIRED, "E-posta doğrulaması gerekli."),
        (
            ErrorCode.TERMS_REQUIRED,
            "Devam etmek için koşulları kabul etmeniz gerekli.",
        ),
        (ErrorCode.ACCOUNT_DISABLED, "Hesabınız devre dışı bırakılmış."),
        (ErrorCode.ACCOUNT_LINK_REQUIRED, "Hesap bağlantısı gerekli."),
        (
            ErrorCode.UPSTREAM_RATE_LIMITED,
            "Kimlik doğrulama hizmeti şu anda çok fazla istek alıyor.",
        ),
        (
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "Kimlik doğrulama hizmetine şu anda erişilemiyor.",
        ),
    ],
)
def test_activation_error_message_is_stable_and_labels_remote_detail(
    code: ErrorCode, expected: str
) -> None:
    remote_detail = "upstream detail"
    error = ActivationError(code, remote_detail, http_status=403)

    assert activation_error_message(code) == expected
    assert activation_error_message(code, remote_detail) == (
        f"{expected} Uzak hizmet ayrıntısı: {remote_detail}"
    )
    assert error.remote_detail == remote_detail
    assert error.message == activation_error_message(code, remote_detail)


@pytest.mark.asyncio
async def test_activation_success_maps_active_record(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    handler = AuthHttp()
    clock = FakeClock()
    async with _http(handler) as http:
        session = _session(http, tmp_path, clock)
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        record = await session.activate()
    request = handler.requests[0]
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["User-Agent"] == f"MutalaaMCP/{__version__}"
    assert json.loads(request.content) == {"client_version": __version__}
    assert record.status == "active"
    assert record.features == ("local_research",)
    assert_no_persisted_secrets(tmp_path, fake_keyring, allowed_refresh=REFRESH_TOKEN)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "headers", "code", "terms_url", "retry_after"),
    [
        (
            401,
            {"code": "invalid_or_expired_token", "message": "gone"},
            {},
            ErrorCode.SESSION_EXPIRED,
            None,
            None,
        ),
        (
            401,
            {"error": {"code": "invalid_or_expired_token", "message": "nested gone"}},
            {},
            ErrorCode.SESSION_EXPIRED,
            None,
            None,
        ),
        (
            403,
            {"code": "email_verification_required", "message": "verify"},
            {},
            ErrorCode.EMAIL_VERIFICATION_REQUIRED,
            None,
            None,
        ),
        (
            403,
            TERMS_REQUIRED_CONTRACT,
            {},
            ErrorCode.TERMS_REQUIRED,
            TERMS_REQUIRED_CONTRACT["terms_url"],
            None,
        ),
        (
            403,
            {
                "code": "terms_required",
                "message": "accept nested",
                "details": {"terms_url": f"{ALLOWED_TERMS}/mcp"},
            },
            {},
            ErrorCode.TERMS_REQUIRED,
            f"{ALLOWED_TERMS}/mcp",
            None,
        ),
        (
            403,
            {
                "error": {
                    "code": "terms_required",
                    "message": "accept error",
                    "details": {"terms_url": ALLOWED_TERMS},
                }
            },
            {},
            ErrorCode.TERMS_REQUIRED,
            ALLOWED_TERMS,
            None,
        ),
        (
            403,
            {"code": "account_disabled", "message": "disabled"},
            {},
            ErrorCode.ACCOUNT_DISABLED,
            None,
            None,
        ),
        (
            409,
            {"code": "account_link_required", "message": "link"},
            {},
            ErrorCode.ACCOUNT_LINK_REQUIRED,
            None,
            None,
        ),
        (
            429,
            {"code": "rate_limited", "message": "slow", "retry_after": 7},
            {},
            ErrorCode.UPSTREAM_RATE_LIMITED,
            None,
            7.0,
        ),
        (
            429,
            {
                "error": {
                    "code": "rate_limited",
                    "message": "nested",
                    "retry_after": "3.5",
                }
            },
            {},
            ErrorCode.UPSTREAM_RATE_LIMITED,
            None,
            3.5,
        ),
        (
            429,
            {"code": "rate_limited", "message": "header only"},
            {"Retry-After": "12"},
            ErrorCode.UPSTREAM_RATE_LIMITED,
            None,
            12.0,
        ),
        (
            503,
            {"code": "temporarily_unavailable", "message": "down"},
            {},
            ErrorCode.UPSTREAM_UNAVAILABLE,
            None,
            None,
        ),
    ],
)
async def test_activation_error_mapping(
    tmp_path: Path,
    status: int,
    body: dict[str, Any],
    headers: dict[str, str],
    code: ErrorCode,
    terms_url: str | None,
    retry_after: float | None,
) -> None:
    handler = AuthHttp()
    handler.activate_status = status
    handler.activate_body = body
    handler.activate_headers = headers
    clock = FakeClock()
    async with _http(handler) as http:
        session = _session(http, tmp_path, clock)
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        with pytest.raises(ActivationError) as caught:
            await session.activate()
    assert caught.value.code is code
    assert caught.value.http_status == status
    assert caught.value.terms_url == terms_url
    assert caught.value.retry_after == retry_after
    assert session.local_state() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "text"),
    [
        (500, {"code": "invalid_or_expired_token", "message": "x"}, None),
        (401, {"code": "nope", "message": "x"}, None),
        (403, {"code": "terms_required", "message": "missing url"}, None),
        (429, {"code": "rate_limited", "message": "missing retry"}, None),
        (
            200,
            {
                "status": "active",
                "plan": "mcp_local_free",
                "features": ["local_research", "extra"],
                "checked_at": CHECKED_AT,
            },
            None,
        ),
        (
            200,
            {
                "status": "pending",
                "plan": "mcp_local_free",
                "features": ["local_research"],
                "checked_at": CHECKED_AT,
            },
            None,
        ),
        (403, None, "not-json"),
        (403, ["not", "an", "object"], None),
    ],
)
async def test_activation_protocol_errors(
    tmp_path: Path,
    status: int,
    body: Any,
    text: str | None,
) -> None:
    handler = AuthHttp()
    handler.activate_status = status
    handler.activate_body = body
    handler.activate_text = text
    clock = FakeClock()
    async with _http(handler) as http:
        session = _session(http, tmp_path, clock)
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        with pytest.raises(ActivationProtocolError) as caught:
            await session.activate()
    assert caught.value.http_status == status
    assert session.local_state() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terms_url",
    [
        "http://mutalaa.test/kosullar",
        "https://evil.test/kosullar",
        "https://mutalaa.test.evil.test/kosullar",
        "https://evil.mutalaa.test/kosullar",
        "https://user:pass@mutalaa.test/kosullar",
        "https://mutalaa.test@evil.test/kosullar",
        "https://mutalaa.test:4443/kosullar",
        "https://mutalaa.test/other",
        "https://mutalaa.test/kosullarextra",
        "https://mutalaa.test/kosullar/../secret",
        "https://mutalaa.test/kosullar%2f..%2fsecret",
        "https://mutalaa.test/kosullar/%2e%2e/admin",
        " https://mutalaa.test/kosullar",
        "https://mutalaa.test/kosullar ",
        "javascript:alert(1)",
        "ftp://mutalaa.test/kosullar",
        "//mutalaa.test/kosullar",
        "https://127.0.0.1/kosullar",
        "",
    ],
)
async def test_terms_url_allowlist_rejects_attacks(
    tmp_path: Path, terms_url: str
) -> None:
    handler = AuthHttp()
    handler.activate_status = 403
    handler.activate_body = {
        "code": "terms_required",
        "message": "accept",
        "terms_url": terms_url,
    }
    clock = FakeClock()
    async with _http(handler) as http:
        session = _session(http, tmp_path, clock)
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        with pytest.raises(ActivationProtocolError) as caught:
            await session.activate()
    assert caught.value.http_status == 403
    assert "terms_url" in str(caught.value)


@pytest.mark.asyncio
async def test_terms_url_allowlist_accepts_https_path_under_base(
    tmp_path: Path,
) -> None:
    allowed = f"{ALLOWED_TERMS}/mcp?v=1"
    handler = AuthHttp()
    handler.activate_status = 403
    handler.activate_body = {
        "code": "terms_required",
        "message": "accept",
        "terms_url": allowed,
    }
    clock = FakeClock()
    async with _http(handler) as http:
        session = _session(http, tmp_path, clock)
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        with pytest.raises(ActivationError) as caught:
            await session.activate()
    assert caught.value.code is ErrorCode.TERMS_REQUIRED
    assert caught.value.terms_url == allowed


@pytest.mark.asyncio
async def test_login_clears_logged_out_only_after_activation(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    handler = AuthHttp()
    handler.poll = [(200, TOKENS_BODY)]
    handler.activate_status = 403
    handler.activate_body = {
        "code": "email_verification_required",
        "message": "verify",
    }
    clock = FakeClock()
    state = AuthState(tmp_path)
    state.write_logged_out()
    assert state.is_logged_out()

    async with _http(handler) as http:
        session = _session(http, tmp_path, clock)
        with pytest.raises(ActivationError) as caught:
            await session.login(_noop_authorization)
        assert caught.value.code is ErrorCode.EMAIL_VERIFICATION_REQUIRED
        assert session.is_logged_out()
        assert session.access_token() == ACCESS_TOKEN
        assert (
            fake_keyring.get_password(
                KEYRING_SERVICE, session._credentials.keyring_username
            )
            == REFRESH_TOKEN
        )
        assert json.loads(_state_path(tmp_path).read_text(encoding="utf-8")) == {
            "status": "logged_out",
            "generation": 1,
        }

        handler.activate_status = 200
        handler.activate_body = dict(ACTIVE_BODY)
        record = await session.activate()

    assert record.status == "active"
    assert session.local_state() is not None
    assert session.local_state().status == "active"
    assert not session.is_logged_out()
    assert_no_persisted_secrets(tmp_path, fake_keyring, allowed_refresh=REFRESH_TOKEN)


@pytest.mark.asyncio
async def test_activate_cannot_clear_logout_without_completed_login(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    state = AuthState(tmp_path)
    state.write_logged_out()
    handler = AuthHttp()
    clock = FakeClock()

    async with _http(handler) as http:
        session = _session(http, tmp_path, clock)
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        with pytest.raises(AuthenticationRequired):
            await session.activate()

    assert session.is_logged_out()
    assert handler.requests == []
    assert fake_keyring.passwords == {
        (KEYRING_SERVICE, session._credentials.keyring_username): REFRESH_TOKEN
    }


def test_logout_tombstone_rejects_stale_cross_process_activation(
    tmp_path: Path,
) -> None:
    activating_process = AuthState(tmp_path)
    logout_process = AuthState(tmp_path)
    activating_process.write_activated(_active_record())
    observed_generation = activating_process.generation()

    logout_process.write_logged_out()

    assert (
        activating_process.write_activated(
            _active_record(), expected_generation=observed_generation
        )
        is False
    )
    state = logout_process.read()
    assert state is not None and state.status == "logged_out"


@pytest.mark.asyncio
async def test_ensure_authorized_rejects_logout_during_activation(
    tmp_path: Path,
) -> None:
    state = AuthState(tmp_path)
    state.write_activated(_active_record())
    clock = _clock_after(CHECKED_AT, hours=24)

    class LogoutDuringActivation:
        async def activate(self, _access_token: str) -> ActivationRecord:
            AuthState(tmp_path).write_logged_out()
            return _active_record()

    async with _gate_http() as http:
        session = _gate_session(
            http,
            tmp_path,
            clock,
            device_flow=GateDeviceFlow(_refresh_tokens()),
            activation_client=LogoutDuringActivation(),
        )
        session._credentials.replace_session(
            ACCESS_TOKEN,
            REFRESH_TOKEN,
            access_token_expires_at=clock.time() + 3600,
        )
        with pytest.raises(AuthenticationRequired):
            await session.ensure_authorized()

    assert session.is_logged_out()


@pytest.mark.asyncio
async def test_logout_writes_logged_out_before_credential_deletion(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    handler = AuthHttp()
    clock = FakeClock()
    order: list[str] = []

    def before_delete() -> None:
        payload = json.loads(_state_path(tmp_path).read_text(encoding="utf-8"))
        order.append(payload["status"])

    fake_keyring.before_delete = before_delete
    async with _http(handler) as http:
        session = _session(http, tmp_path, clock)
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        result = await session.logout()
    assert order == ["logged_out"]
    assert result.cleanup_error is None
    assert session.is_logged_out()
    assert session.access_token() is None
    assert fake_keyring.passwords == {}
    assert json.loads(_state_path(tmp_path).read_text(encoding="utf-8")) == {
        "status": "logged_out",
        "generation": 1,
    }
    assert_no_persisted_secrets(tmp_path, fake_keyring)


@pytest.mark.asyncio
async def test_logout_stays_logged_out_when_keyring_delete_fails(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    handler = AuthHttp()
    clock = FakeClock()
    fake_keyring.delete_error = RuntimeError("keyring down")
    async with _http(handler) as http:
        session = _session(http, tmp_path, clock)
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        result = await session.logout()
    assert session.is_logged_out()
    assert result.cleanup_error == "credential_delete başarısız oldu (RuntimeError)"
    assert ACCESS_TOKEN not in result.cleanup_error
    assert REFRESH_TOKEN not in result.cleanup_error
    assert json.loads(_state_path(tmp_path).read_text(encoding="utf-8")) == {
        "status": "logged_out",
        "generation": 1,
    }


@pytest.mark.asyncio
async def test_logout_reports_password_delete_error_when_token_remains(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    handler = AuthHttp()
    clock = FakeClock()
    fake_keyring.delete_error = PasswordDeleteError("keyring refused deletion")
    async with _http(handler) as http:
        session = _session(http, tmp_path, clock)
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        result = await session.logout()

    assert session.is_logged_out()
    assert (
        result.cleanup_error == "credential_delete başarısız oldu (PasswordDeleteError)"
    )
    assert fake_keyring.passwords == {
        (KEYRING_SERVICE, session._credentials.keyring_username): REFRESH_TOKEN
    }


@pytest.mark.asyncio
async def test_logout_stays_logged_out_when_remote_revoke_fails(
    tmp_path: Path, fake_keyring: InMemoryKeyring
) -> None:
    handler = AuthHttp()
    clock = FakeClock()

    async def revoke(access_token: str | None, refresh_token: str | None) -> None:
        assert access_token == ACCESS_TOKEN
        assert refresh_token == REFRESH_TOKEN
        raise RuntimeError(f"revoke {access_token} {refresh_token}")

    async with _http(handler) as http:
        session = _session(http, tmp_path, clock)
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        result = await session.logout(revoke_session=revoke)
    assert session.is_logged_out()
    assert result.cleanup_error == "remote_revoke başarısız oldu (RuntimeError)"
    assert ACCESS_TOKEN not in (result.cleanup_error or "")
    assert REFRESH_TOKEN not in (result.cleanup_error or "")
    assert session.access_token() is None
    assert fake_keyring.passwords == {}


@pytest.mark.asyncio
async def test_logout_stays_logged_out_when_credential_read_fails(
    tmp_path: Path,
) -> None:
    handler = AuthHttp()
    clock = FakeClock()

    class BoomStore(CredentialStore):
        def load_refresh_token(self) -> str | None:
            raise RuntimeError("read failed")

    async with _http(handler) as http:
        session = AuthSession(
            http,
            auth_base_url=AUTH_BASE,
            allowed_terms_base_url=TERMS_BASE,
            clock=clock.time,
            sleep=clock.sleep,
            data_dir=tmp_path,
            credentials=BoomStore(auth_base_url=AUTH_BASE),
            state=AuthState(tmp_path),
        )
        session._credentials.replace_session(ACCESS_TOKEN, REFRESH_TOKEN)
        result = await session.logout()
    assert session.is_logged_out()
    assert result.cleanup_error == "credential_read başarısız oldu (RuntimeError)"
    assert ACCESS_TOKEN not in result.cleanup_error
