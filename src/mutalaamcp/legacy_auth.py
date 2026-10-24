"""One-time removal of credentials left by the retired Mütalaa account sign-in."""

from __future__ import annotations

import sys
from hashlib import sha256
from pathlib import Path

_KEYRING_SERVICE = "MutalaaMCP"
_KEYRING_USERNAME = "refresh_token:v1:" + sha256(b"https://mutalaa.tr").hexdigest()
_LEGACY_FILES = (
    "auth_state.json",
    ".auth_state.json.lock",
    "native-oauth.sqlite3",
    "announcement-delivery.sqlite3",
)
_DONE_MARKER = "legacy-auth-removed"


def remove_legacy_auth_state(data_dir: Path) -> None:
    """Delete the stored refresh token and auth files once; never block startup."""
    marker = data_dir / _DONE_MARKER
    if marker.exists():
        return
    try:
        for name in _LEGACY_FILES:
            (data_dir / name).unlink(missing_ok=True)
        _delete_refresh_token()
        data_dir.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except Exception as exc:  # noqa: BLE001 -- retried on the next start
        print(
            f"mutalaamcp eski oturum temizliği tamamlanamadı: {type(exc).__name__}",
            file=sys.stderr,
        )


def _delete_refresh_token() -> None:
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    except PasswordDeleteError:
        if keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME) is not None:
            raise
