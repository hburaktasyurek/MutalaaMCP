"""Local activation / logged-out state as minimal JSON. No PII, tokens, or queries."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal, cast

import platformdirs
from filelock import FileLock

from mutalaamcp.auth.activation import ActivationRecord

_APP_NAME = "MutalaaMCP"
_STATE_FILENAME = "auth_state.json"
_ACTIVE = "active"
_LOGGED_OUT = "logged_out"
_PLAN = "mcp_local_free"
_FEATURE = "local_research"


class AuthStateError(Exception):
    """Local auth state file is missing required fields or is corrupt."""


class LocalAuthState:
    __slots__ = (
        "checked_at",
        "features",
        "generation",
        "plan",
        "session_generation",
        "status",
    )

    def __init__(
        self,
        status: Literal["active", "logged_out"],
        *,
        plan: Literal["mcp_local_free"] | None = None,
        features: tuple[str, ...] = (),
        checked_at: str | None = None,
        generation: int = 0,
        session_generation: int | None = None,
    ) -> None:
        self.status = status
        self.plan = plan
        self.features = features
        self.checked_at = checked_at
        self.generation = generation
        # Every write advances generation for race checks; only a new session
        # changes the OAuth binding. Legacy states used generation for both.
        self.session_generation = (
            generation if session_generation is None else session_generation
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LocalAuthState):
            return NotImplemented
        return (
            self.status == other.status
            and self.plan == other.plan
            and self.features == other.features
            and self.checked_at == other.checked_at
            and self.generation == other.generation
            and self.session_generation == other.session_generation
        )

    def __repr__(self) -> str:
        return (
            "LocalAuthState("
            f"status={self.status!r}, plan={self.plan!r}, "
            f"features={self.features!r}, checked_at={self.checked_at!r}, "
            f"generation={self.generation!r}, "
            f"session_generation={self.session_generation!r})"
        )


class AuthState:
    """Atomic JSON state under ``platformdirs.user_data_dir("MutalaaMCP")``."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir

    def read(self) -> LocalAuthState | None:
        return self._read_unlocked()

    def _read_unlocked(self) -> LocalAuthState | None:
        path = self._path()
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthStateError(
                "Kimlik doğrulama durum dosyası geçerli JSON değil"
            ) from exc
        if not isinstance(payload, dict):
            raise AuthStateError("Kimlik doğrulama durumu JSON'u bir nesne olmalıdır")
        return _parse_state(cast(dict[str, Any], payload))

    def write_activated(
        self,
        record: ActivationRecord,
        *,
        expected_generation: int | None = None,
        new_session: bool = False,
    ) -> bool:
        """Advance the write version, preserving OAuth grants on revalidation."""
        with FileLock(self._lock_path()):
            state = self._read_unlocked()
            generation = 0 if state is None else state.generation
            if expected_generation is not None and generation != expected_generation:
                return False
            if state is not None and state.status == _LOGGED_OUT and not new_session:
                return False
            _atomic_replace(
                self._path(),
                {
                    "status": _ACTIVE,
                    "plan": record.plan,
                    "features": list(record.features),
                    "checked_at": record.checked_at,
                    "generation": generation + 1,
                    "session_generation": (
                        state.session_generation
                        if state is not None
                        and state.status == _ACTIVE
                        and not new_session
                        else generation + 1
                    ),
                },
            )
            return True

    def write_logged_out(self) -> None:
        """Atomically advance the logout tombstone across processes."""
        with FileLock(self._lock_path()):
            state = self._read_unlocked()
            generation = 0 if state is None else state.generation
            _atomic_replace(
                self._path(),
                {
                    "status": _LOGGED_OUT,
                    "generation": generation + 1,
                },
            )

    def generation(self) -> int:
        state = self.read()
        return 0 if state is None else state.generation

    def is_logged_out(self) -> bool:
        state = self.read()
        return state is not None and state.status == _LOGGED_OUT

    def _path(self) -> Path:
        return self._directory() / _STATE_FILENAME

    def _lock_path(self) -> Path:
        return self._directory() / f".{_STATE_FILENAME}.lock"

    def _directory(self) -> Path:
        if self._data_dir is None:
            self._data_dir = Path(platformdirs.user_data_dir(_APP_NAME))
        return self._data_dir


def _status_from_json(value: object) -> Literal["active", "logged_out"]:
    if value == _LOGGED_OUT:
        return "logged_out"
    if value == _ACTIVE:
        return "active"
    raise AuthStateError(f"Bilinmeyen kimlik doğrulama durumu: {value!r}")


def _plan_from_json(value: object) -> Literal["mcp_local_free"]:
    if value == _PLAN:
        return "mcp_local_free"
    raise AuthStateError("Etkin kimlik doğrulama durumu planı mcp_local_free olmalıdır")


def _parse_state(payload: dict[str, Any]) -> LocalAuthState:
    status = _status_from_json(payload.get("status"))
    if status == "logged_out":
        return LocalAuthState(status, generation=_generation_from_json(payload))
    plan = _plan_from_json(payload.get("plan"))
    features = payload.get("features")
    checked_at = payload.get("checked_at")
    if features != [_FEATURE] and features != (_FEATURE,):
        raise AuthStateError(
            "Etkin kimlik doğrulama durumu özellikleri tam olarak ['local_research'] olmalıdır"
        )
    if not isinstance(checked_at, str) or not checked_at:
        raise AuthStateError("Etkin kimlik doğrulama durumu için checked_at gereklidir")
    generation = _generation_from_json(payload)
    session_generation = _generation_from_json(
        payload, key="session_generation", default=generation
    )
    if session_generation > generation:
        raise AuthStateError("session_generation, generation değerini aşamaz")
    return LocalAuthState(
        status,
        plan=plan,
        features=(_FEATURE,),
        checked_at=checked_at,
        generation=generation,
        session_generation=session_generation,
    )


def _generation_from_json(
    payload: dict[str, Any], *, key: str = "generation", default: int = 0
) -> int:
    generation = payload.get(key, default)
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
    ):
        raise AuthStateError(
            f"Kimlik doğrulama durumu için {key} negatif olmayan bir tam sayı olmalıdır"
        )
    return generation


def _atomic_replace(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
