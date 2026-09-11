"""Mütalaa aracılı cihaz yetkilendirme ve belirteç yenileme."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mutalaamcp import __version__
from mutalaamcp.settings import normalize_auth_origin

DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_TOKEN_GRANT_TYPE = "refresh_token"
_SLOW_DOWN_INCREMENT_SECONDS = 5.0
_AUTHORIZE_PATH = "/api/mcp/v1/device/authorize"
_AUTHENTICATE_PATH = "/api/mcp/v1/device/authenticate"
_PENDING = "authorization_pending"
_SLOW_DOWN = "slow_down"
_ACCESS_DENIED = "access_denied"
_EXPIRED_TOKEN = "expired_token"
_INVALID_GRANT = "invalid_grant"
_TERMINAL_POLL_ERRORS = frozenset({_ACCESS_DENIED, _EXPIRED_TOKEN})

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class DeviceFlowError(Exception):
    """Mütalaa cihaz/yenileme isteği kurtarılabilir bekleme olmadan başarısız oldu."""

    def __init__(
        self,
        error: str,
        description: str | None = None,
        *,
        http_status: int | None = None,
    ) -> None:
        self.error = error
        self.description = description
        self.http_status = http_status
        super().__init__(description or error)


class SessionExpired(DeviceFlowError):
    """Mütalaa aracısının ``invalid_grant`` yanıtı oturumun bittiğini gösterir."""

    def __init__(
        self, description: str | None = None, *, http_status: int | None = None
    ) -> None:
        super().__init__(_INVALID_GRANT, description, http_status=http_status)


class DeviceAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    device_code: str = Field(min_length=1)
    user_code: str = Field(min_length=1)
    verification_uri: str = Field(min_length=1)
    verification_uri_complete: str = Field(min_length=1)
    expires_in: float = Field(gt=0)
    interval: float = Field(gt=0)

    @field_validator("expires_in", "interval", mode="before")
    @classmethod
    def _positive_number(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("pozitif bir sayı olmalıdır")
        return value

    def __repr__(self) -> str:
        return (
            "DeviceAuthorization("
            f"user_code={self.user_code!r}, "
            f"verification_uri={self.verification_uri!r}, "
            f"verification_uri_complete={self.verification_uri_complete!r}, "
            f"expires_in={self.expires_in!r}, "
            f"interval={self.interval!r})"
        )

    def __str__(self) -> str:
        return f"{self.verification_uri} adresini ziyaret edin ve {self.user_code} kodunu girin"


class TokenSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    access_token: str = Field(min_length=1)
    refresh_token: str = Field(min_length=1)
    expires_in: float = Field(gt=0)

    @field_validator("expires_in", mode="before")
    @classmethod
    def _positive_expiry(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("expires_in pozitif bir sayı olmalıdır")
        return value


class DeviceFlow:
    """Mütalaa'nın markalı cihaz akışı üzerinden oturum belirteçlerini yönet."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        auth_base_url: str,
        clock: Clock | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self._http = http_client
        self._base = normalize_auth_origin(
            auth_base_url, field="Mütalaa kimlik doğrulama temel URL'si"
        ).rstrip("/")
        self._clock: Clock = clock if clock is not None else _default_clock
        self._sleep: Sleep = sleep if sleep is not None else _default_sleep

    async def authorize(self) -> DeviceAuthorization:
        response = await self._http.post(
            f"{self._base}{_AUTHORIZE_PATH}",
            headers={"Accept": "application/json"},
            json={"client_version": __version__},
        )
        payload = _json_object(response)
        if not response.is_success:
            raise _oauth_error(payload, http_status=response.status_code)
        return _parse_authorization(
            payload, http_status=response.status_code, allowed_origin=self._base
        )

    async def poll(self, authorization: DeviceAuthorization) -> TokenSet:
        interval = authorization.interval
        deadline = self._clock() + authorization.expires_in
        await self._wait(interval, deadline)
        while True:
            self._raise_if_expired(deadline)
            try:
                payload, ok, http_status = await self._authenticate(
                    {
                        "grant_type": DEVICE_CODE_GRANT_TYPE,
                        "device_code": authorization.device_code,
                    }
                )
            except httpx.TransportError:
                interval = max(interval, min(interval * 2, 60.0))
                await self._wait(interval, deadline)
                continue
            except DeviceFlowError as exc:
                if exc.http_status is None or not 500 <= exc.http_status < 600:
                    raise
                interval = max(interval, min(interval * 2, 60.0))
                await self._wait(interval, deadline)
                continue
            if 500 <= http_status < 600:
                interval = max(interval, min(interval * 2, 60.0))
                await self._wait(interval, deadline)
                continue
            if ok:
                return _parse_tokens(payload, http_status=http_status)
            error = _oauth_error_code(payload)
            if error == _PENDING:
                await self._wait(interval, deadline)
                continue
            if error == _SLOW_DOWN:
                interval += _SLOW_DOWN_INCREMENT_SECONDS
                await self._wait(interval, deadline)
                continue
            if error in _TERMINAL_POLL_ERRORS:
                raise _oauth_error(payload, http_status=http_status)
            if error == _INVALID_GRANT:
                raise SessionExpired(
                    _oauth_description(payload), http_status=http_status
                )
            raise _oauth_error(payload, http_status=http_status)

    async def refresh(self, refresh_token: str) -> TokenSet:
        if not refresh_token:
            raise ValueError("refresh_token boş olmamalıdır")
        payload, ok, http_status = await self._authenticate(
            {
                "grant_type": REFRESH_TOKEN_GRANT_TYPE,
                "refresh_token": refresh_token,
            }
        )
        if ok:
            return _parse_tokens(payload, http_status=http_status)
        if (
            _oauth_error_code(payload) == _INVALID_GRANT
            and not 500 <= http_status < 600
        ):
            raise SessionExpired(_oauth_description(payload), http_status=http_status)
        raise _oauth_error(payload, http_status=http_status, expire_invalid_grant=False)

    async def _authenticate(
        self, form: Mapping[str, str]
    ) -> tuple[dict[str, Any], bool, int]:
        response = await self._http.post(
            f"{self._base}{_AUTHENTICATE_PATH}",
            headers={"Accept": "application/json"},
            data=dict(form),
        )
        payload = _json_object(response)
        return payload, response.is_success, response.status_code

    async def _wait(self, interval: float, deadline: float) -> None:
        remaining = deadline - self._clock()
        self._raise_if_expired(deadline, remaining)
        await self._sleep(min(interval, remaining))

    def _raise_if_expired(
        self, deadline: float, remaining: float | None = None
    ) -> None:
        left = deadline - self._clock() if remaining is None else remaining
        if left <= 0:
            raise DeviceFlowError(_EXPIRED_TOKEN, "Cihaz kodunun süresi doldu")


def _default_clock() -> float:
    return time.monotonic()


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _parse_authorization(
    payload: Mapping[str, Any],
    *,
    http_status: int | None = None,
    allowed_origin: str,
) -> DeviceAuthorization:
    try:
        authorization = DeviceAuthorization.model_validate(payload)
    except ValidationError as exc:
        raise DeviceFlowError(
            "invalid_response",
            "Cihaz yetkilendirme yanıtında gerekli alanlar eksik",
            http_status=http_status,
        ) from exc
    if not _same_origin(authorization.verification_uri, allowed_origin):
        raise DeviceFlowError(
            "invalid_response",
            "Doğrulama adresi Mütalaa kimlik doğrulama kökeniyle eşleşmelidir",
            http_status=http_status,
        )
    if not _same_origin(authorization.verification_uri_complete, allowed_origin):
        raise DeviceFlowError(
            "invalid_response",
            "Tam doğrulama adresi Mütalaa kimlik doğrulama kökeniyle eşleşmelidir",
            http_status=http_status,
        )
    return authorization


def _same_origin(candidate: str, allowed_origin: str) -> bool:
    try:
        parsed = urlsplit(candidate)
        allowed = urlsplit(allowed_origin)
        candidate_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        allowed_port = allowed.port or (443 if allowed.scheme == "https" else 80)
    except ValueError:
        return False
    return (
        parsed.scheme == allowed.scheme
        and parsed.hostname is not None
        and allowed.hostname is not None
        and parsed.hostname.rstrip(".").lower() == allowed.hostname.rstrip(".").lower()
        and candidate_port == allowed_port
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )


def _parse_tokens(
    payload: Mapping[str, Any], *, http_status: int | None = None
) -> TokenSet:
    try:
        return TokenSet.model_validate(payload)
    except ValidationError as exc:
        raise DeviceFlowError(
            "invalid_response",
            "Belirteç yanıtı access_token, refresh_token ve expires_in içermelidir",
            http_status=http_status,
        ) from exc


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise DeviceFlowError(
            "invalid_response",
            f"Yanıt JSON değildi (HTTP {response.status_code})",
            http_status=response.status_code,
        ) from exc
    if not isinstance(payload, dict):
        raise DeviceFlowError(
            "invalid_response",
            "Yanıt JSON'u bir nesne olmalıdır",
            http_status=response.status_code,
        )
    return cast(dict[str, Any], payload)


def _oauth_error_code(payload: Mapping[str, Any]) -> str | None:
    error = payload.get("error")
    return error if isinstance(error, str) and error else None


def _oauth_description(payload: Mapping[str, Any]) -> str | None:
    description = payload.get("error_description")
    return description if isinstance(description, str) and description else None


def _oauth_error(
    payload: Mapping[str, Any],
    *,
    http_status: int | None = None,
    expire_invalid_grant: bool = True,
) -> DeviceFlowError:
    error = _oauth_error_code(payload)
    if error == _INVALID_GRANT and expire_invalid_grant:
        return SessionExpired(_oauth_description(payload), http_status=http_status)
    if error is None:
        return DeviceFlowError(
            "invalid_response",
            "OAuth hata yanıtında error alanı eksik",
            http_status=http_status,
        )
    return DeviceFlowError(error, _oauth_description(payload), http_status=http_status)
