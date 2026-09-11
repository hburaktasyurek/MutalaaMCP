"""Refresh-token persistence in the OS keyring; access tokens stay in memory."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from math import isfinite

import keyring
from keyring.errors import PasswordDeleteError

from mutalaamcp.settings import normalize_auth_origin

KEYRING_SERVICE = "MutalaaMCP"
_REFRESH_USERNAME_PREFIX = "refresh_token:v1:"


@dataclass(frozen=True, slots=True)
class CredentialSnapshot:
    """One coherent local credential version."""

    access_token: str | None
    access_token_expires_at: float | None
    refresh_token: str | None
    generation: int


class CredentialStore:
    """Persist refresh tokens under a Mütalaa-auth-origin-specific keyring name.

    Access tokens and their authoritative expiry remain process-memory only.
    Every successful rotation persists the replacement refresh token before
    publishing the new access token to callers.
    """

    def __init__(self, *, auth_base_url: str) -> None:
        normalized_auth_origin = normalize_auth_origin(
            auth_base_url, field="Mütalaa kimlik doğrulama temel URL'si"
        )
        self._username = (
            _REFRESH_USERNAME_PREFIX
            + sha256(normalized_auth_origin.encode()).hexdigest()
        )
        self._access_token: str | None = None
        self._access_token_expires_at: float | None = None
        self._generation = 0

    @property
    def keyring_username(self) -> str:
        """Return the non-secret Mütalaa authentication namespace key."""
        return self._username

    def access_token(self) -> str | None:
        return self._access_token

    def has_access_token(self) -> bool:
        """Return whether this process has an access token, without exposing it."""
        return self._access_token is not None

    def has_refresh_token(self) -> bool:
        """Return whether a usable refresh credential is stored, without exposing it."""
        return self.load_refresh_token() is not None

    def has_valid_access_token(self, *, now: float, skew_seconds: float) -> bool:
        """Return whether the in-memory token remains usable beyond clock skew."""
        expires_at = self._access_token_expires_at
        return (
            self._access_token is not None
            and expires_at is not None
            and now + skew_seconds < expires_at
        )

    def load_refresh_token(self) -> str | None:
        token = keyring.get_password(KEYRING_SERVICE, self._username)
        if token is None or token == "":
            return None
        return token

    def snapshot(self) -> CredentialSnapshot:
        """Return the current access/refresh pair and its mutation generation."""
        return CredentialSnapshot(
            access_token=self._access_token,
            access_token_expires_at=self._access_token_expires_at,
            refresh_token=self.load_refresh_token(),
            generation=self._generation,
        )

    def matches(self, snapshot: CredentialSnapshot) -> bool:
        """Return whether no local credential mutation followed ``snapshot``."""
        return (
            self._generation == snapshot.generation
            and self._access_token == snapshot.access_token
            and self._access_token_expires_at == snapshot.access_token_expires_at
            and self.load_refresh_token() == snapshot.refresh_token
        )

    def replace_session(
        self,
        access_token: str,
        refresh_token: str,
        *,
        access_token_expires_at: float | None = None,
    ) -> None:
        if not access_token:
            raise ValueError("access_token boş olmamalıdır")
        if not refresh_token:
            raise ValueError("refresh_token boş olmamalıdır")
        if access_token_expires_at is not None and (
            isinstance(access_token_expires_at, bool)
            or not isinstance(access_token_expires_at, (int, float))
            or not isfinite(access_token_expires_at)
        ):
            raise ValueError("access_token_expires_at sonlu olmalıdır")
        keyring.set_password(KEYRING_SERVICE, self._username, refresh_token)
        self._access_token = access_token
        self._access_token_expires_at = access_token_expires_at
        self._generation += 1

    def clear_access_token(self) -> None:
        if self._access_token is not None:
            self._access_token = None
            self._access_token_expires_at = None
            self._generation += 1

    def delete(self) -> None:
        self._access_token = None
        self._access_token_expires_at = None
        self._generation += 1
        try:
            keyring.delete_password(KEYRING_SERVICE, self._username)
        except PasswordDeleteError:
            if keyring.get_password(KEYRING_SERVICE, self._username) is not None:
                raise
