"""Mütalaa-brokered auth, OS keyring credentials, and account activation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import time

import httpx

from mutalaamcp.auth.activation import (
    ActivationClient,
    ActivationError,
    ActivationProtocolError,
    ActivationRecord,
    activation_error_message,
)
from mutalaamcp.auth.credentials import (
    KEYRING_SERVICE,
    CredentialSnapshot,
    CredentialStore,
)
from mutalaamcp.auth.device_flow import (
    DEVICE_CODE_GRANT_TYPE,
    Clock,
    DeviceAuthorization,
    DeviceFlow,
    DeviceFlowError,
    SessionExpired,
    Sleep,
    TokenSet,
)
from mutalaamcp.auth.state import AuthState, AuthStateError, LocalAuthState
from mutalaamcp.domain.errors import ErrorCode

RevokeSession = Callable[[str | None, str | None], Awaitable[None]]
PresentAuthorization = Callable[[DeviceAuthorization], Awaitable[None]]

_ACTIVATION_REVALIDATION_INTERVAL = timedelta(hours=24)
_ACCESS_TOKEN_EXPIRY_SKEW_SECONDS = 30.0


class AuthenticationRequired(DeviceFlowError):
    """Kimlik doğrulaması gerektiren istek için kullanılabilir yerel oturum yok."""

    code = ErrorCode.AUTHENTICATION_REQUIRED

    def __init__(self) -> None:
        super().__init__(self.code.value, "Kimlik doğrulaması gerekli.")


class AuthSession:
    """Login, refresh, activate, and logout with injected HTTP, clock, and sleep."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        auth_base_url: str,
        allowed_terms_base_url: str,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
        data_dir: Path | None = None,
        credentials: CredentialStore | None = None,
        state: AuthState | None = None,
        device_flow: DeviceFlow | None = None,
        activation_client: ActivationClient | None = None,
    ) -> None:
        self._credentials = (
            credentials
            if credentials is not None
            else CredentialStore(auth_base_url=auth_base_url)
        )
        self._state = state if state is not None else AuthState(data_dir)
        self._credential_lock = asyncio.Lock()
        self._refresh_tasks: dict[str, asyncio.Task[str]] = {}
        self._completed_login_generation: int | None = None
        self._clock: Clock = clock if clock is not None else time
        self._device_flow = (
            device_flow
            if device_flow is not None
            else DeviceFlow(
                http_client,
                auth_base_url=auth_base_url,
                clock=self._clock,
                sleep=sleep,
            )
        )
        self._activation = (
            activation_client
            if activation_client is not None
            else ActivationClient(
                http_client,
                activation_base_url=auth_base_url,
                allowed_terms_base_url=allowed_terms_base_url,
            )
        )

    def access_token(self) -> str | None:
        return self._credentials.access_token()

    def local_state(self) -> LocalAuthState | None:
        return self._state.read()

    def is_logged_out(self) -> bool:
        return self._state.is_logged_out()

    async def ensure_authorized(self) -> None:
        """Use a valid local token, refreshing and revalidating only when due."""
        state = self._state.read()
        if state is not None and state.status == "logged_out":
            raise AuthenticationRequired()
        async with self._credential_lock:
            credentials = self._credentials.snapshot()
            valid_access_token = self._credentials.has_valid_access_token(
                now=self._clock(), skew_seconds=_ACCESS_TOKEN_EXPIRY_SKEW_SECONDS
            )
        if not valid_access_token and credentials.refresh_token is None:
            raise AuthenticationRequired()

        if not valid_access_token:
            try:
                await self.refresh()
            except (httpx.HTTPError, DeviceFlowError) as exc:
                current_state = self._state.read()
                if (
                    _has_successful_activation(state)
                    and _state_matches(state, current_state)
                    and _is_transient_refresh_error(exc)
                ):
                    return
                raise

        current_state = self._state.read()
        if current_state is not None and current_state.status == "logged_out":
            raise AuthenticationRequired()
        if not _state_matches(state, current_state):
            state = current_state
        if not _activation_is_due(state, now=self._clock()):
            return
        try:
            await self.activate(expected_state_generation=_state_generation(state))
        except httpx.HTTPError:
            current_state = self._state.read()
            if current_state is not None and current_state.status == "logged_out":
                raise AuthenticationRequired()
            if _has_successful_activation(state):
                return
            raise
        except (ActivationError, ActivationProtocolError) as exc:
            current_state = self._state.read()
            if current_state is not None and current_state.status == "logged_out":
                raise AuthenticationRequired()
            if _has_successful_activation(state) and _is_server_error(exc):
                return
            raise
        if self._state.is_logged_out():
            raise AuthenticationRequired()

    async def login(self, on_authorization: PresentAuthorization) -> ActivationRecord:
        async with self._credential_lock:
            credentials = self._credentials.snapshot()
            state = self._state.read()
        authorization = await self._device_flow.authorize()
        await on_authorization(authorization)
        tokens = await self._device_flow.poll(authorization)
        expected_generation = _state_generation(state)
        async with self._credential_lock:
            if not self._credentials.matches(credentials) or not _state_matches(
                state, self._state.read()
            ):
                raise AuthenticationRequired()
            self._credentials.replace_session(
                tokens.access_token,
                tokens.refresh_token,
                access_token_expires_at=self._clock() + tokens.expires_in,
            )
            self._completed_login_generation = expected_generation
        return await self.activate(expected_state_generation=expected_generation)

    async def activate(
        self, *, expected_state_generation: int | None = None
    ) -> ActivationRecord:
        async with self._credential_lock:
            state = self._state.read()
            expected_generation = (
                _state_generation(state)
                if expected_state_generation is None
                else expected_state_generation
            )
            allow_logged_out = self._completed_login_generation == expected_generation
            if (
                state is not None
                and state.status == "logged_out"
                and (not allow_logged_out or state.generation != expected_generation)
            ):
                raise AuthenticationRequired()
            credentials = self._credentials.snapshot()
            token = credentials.access_token
        if token is None:
            await self.refresh()
            async with self._credential_lock:
                credentials = self._credentials.snapshot()
                token = credentials.access_token
            if token is None:
                raise AuthenticationRequired()
        record = await self._activation.activate(token)
        async with self._credential_lock:
            if not self._credentials.matches(credentials):
                raise AuthenticationRequired()
            if not self._state.write_activated(
                record,
                expected_generation=expected_generation,
                allow_logged_out=allow_logged_out,
            ):
                raise AuthenticationRequired()
            if allow_logged_out:
                self._completed_login_generation = None
        return record

    async def refresh(self) -> str:
        async with self._credential_lock:
            credentials = self._credentials.snapshot()
            refresh_token = credentials.refresh_token
            if refresh_token is None:
                raise SessionExpired("Yenileme belirteci depolanmamış.")
            task = self._refresh_tasks.get(refresh_token)
            if task is None:
                task = asyncio.create_task(self._rotate_refresh(credentials))
                self._refresh_tasks[refresh_token] = task
                task.add_done_callback(
                    lambda completed: self._forget_refresh_task(
                        refresh_token, completed
                    )
                )
        return await asyncio.shield(task)

    async def _rotate_refresh(self, credentials: CredentialSnapshot) -> str:
        refresh_token = credentials.refresh_token
        if refresh_token is None:
            raise SessionExpired("Yenileme belirteci depolanmamış.")
        try:
            tokens = await self._device_flow.refresh(refresh_token)
        except SessionExpired as exc:
            if exc.http_status is not None and 500 <= exc.http_status < 600:
                raise DeviceFlowError(
                    exc.error,
                    exc.description,
                    http_status=exc.http_status,
                ) from exc
            async with self._credential_lock:
                if self._state.is_logged_out():
                    raise AuthenticationRequired()
                if self._credentials.matches(credentials):
                    self._credentials.delete()
                current = self._credentials.snapshot()
                if current.access_token is not None:
                    return current.access_token
            raise
        async with self._credential_lock:
            if self._state.is_logged_out():
                raise AuthenticationRequired()
            if self._credentials.matches(credentials):
                self._credentials.replace_session(
                    tokens.access_token,
                    tokens.refresh_token,
                    access_token_expires_at=self._clock() + tokens.expires_in,
                )
                return tokens.access_token
            current = self._credentials.snapshot()
            if current.access_token is not None:
                return current.access_token
        raise AuthenticationRequired()

    def _forget_refresh_task(
        self, refresh_token: str, completed: asyncio.Task[str]
    ) -> None:
        if self._refresh_tasks.get(refresh_token) is completed:
            del self._refresh_tasks[refresh_token]

    async def logout(
        self, *, revoke_session: RevokeSession | None = None
    ) -> LogoutResult:
        refresh_token: str | None = None
        cleanup_error: str | None = None
        async with self._credential_lock:
            access_token = self._credentials.access_token()
            try:
                refresh_token = self._credentials.load_refresh_token()
            except Exception as exc:  # noqa: BLE001  # capture material without blocking local logout
                cleanup_error = _safe_cleanup_error("credential_read", exc)
            self._state.write_logged_out()
            try:
                self._credentials.delete()
            except Exception as exc:  # noqa: BLE001  # deletion must not revert logged_out
                cleanup_error = cleanup_error or _safe_cleanup_error(
                    "credential_delete", exc
                )
        if revoke_session is not None:
            try:
                await revoke_session(access_token, refresh_token)
            except Exception as exc:  # noqa: BLE001  # best-effort remote cleanup after local logout
                cleanup_error = cleanup_error or _safe_cleanup_error(
                    "remote_revoke", exc
                )
        return LogoutResult(cleanup_error=cleanup_error)


@dataclass(frozen=True, slots=True)
class LogoutResult:
    """Local logout outcome. ``cleanup_error`` never contains tokens or PII."""

    cleanup_error: str | None = None


def _safe_cleanup_error(stage: str, exc: BaseException) -> str:
    return f"{stage} başarısız oldu ({type(exc).__name__})"


def _activation_is_due(state: LocalAuthState | None, *, now: float) -> bool:
    if state is None or state.status != "active" or state.checked_at is None:
        return True
    try:
        checked_at = datetime.fromisoformat(state.checked_at)
    except ValueError:
        return True
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    current_time = datetime.fromtimestamp(now, UTC)
    return (
        current_time - checked_at.astimezone(UTC) >= _ACTIVATION_REVALIDATION_INTERVAL
    )


def _has_successful_activation(state: LocalAuthState | None) -> bool:
    return state is not None and state.status == "active"


def _is_server_error(exc: ActivationError | ActivationProtocolError) -> bool:
    status = exc.http_status
    return status is not None and 500 <= status < 600


def _is_transient_refresh_error(exc: httpx.HTTPError | DeviceFlowError) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    if isinstance(exc, httpx.HTTPError):
        return True
    status = exc.http_status
    return status is not None and 500 <= status < 600


def _state_generation(state: LocalAuthState | None) -> int:
    return 0 if state is None else state.generation


def _state_matches(
    expected: LocalAuthState | None, current: LocalAuthState | None
) -> bool:
    return _state_generation(expected) == _state_generation(current)


__all__ = [
    "DEVICE_CODE_GRANT_TYPE",
    "KEYRING_SERVICE",
    "ActivationClient",
    "ActivationError",
    "ActivationProtocolError",
    "ActivationRecord",
    "AuthSession",
    "AuthState",
    "AuthStateError",
    "AuthenticationRequired",
    "Clock",
    "CredentialStore",
    "DeviceAuthorization",
    "DeviceFlow",
    "DeviceFlowError",
    "LocalAuthState",
    "LogoutResult",
    "PresentAuthorization",
    "RevokeSession",
    "SessionExpired",
    "Sleep",
    "TokenSet",
    "activation_error_message",
]
