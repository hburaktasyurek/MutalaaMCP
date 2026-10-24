"""One-time removal of credentials left by the retired account sign-in."""

from __future__ import annotations

from pathlib import Path

import keyring
import pytest
from keyring.errors import PasswordDeleteError

from mutalaamcp import legacy_auth

_LEGACY_FILES = (
    "auth_state.json",
    ".auth_state.json.lock",
    "native-oauth.sqlite3",
    "announcement-delivery.sqlite3",
)


@pytest.fixture
def passwords(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    stored: dict[tuple[str, str], str] = {}

    def delete_password(service: str, user: str) -> None:
        if stored.pop((service, user), None) is None:
            raise PasswordDeleteError("missing")

    monkeypatch.setattr(
        keyring, "get_password", lambda service, user: stored.get((service, user))
    )
    monkeypatch.setattr(keyring, "delete_password", delete_password)
    return stored


def test_removes_legacy_files_and_refresh_token_once(
    tmp_path: Path, passwords: dict[tuple[str, str], str]
) -> None:
    for name in _LEGACY_FILES:
        (tmp_path / name).write_text("legacy", encoding="utf-8")
    (tmp_path / "cache.sqlite3").write_text("keep", encoding="utf-8")
    passwords[("MutalaaMCP", legacy_auth._KEYRING_USERNAME)] = "refresh"

    legacy_auth.remove_legacy_auth_state(tmp_path)

    assert not any((tmp_path / name).exists() for name in _LEGACY_FILES)
    assert (tmp_path / "cache.sqlite3").exists()
    assert passwords == {}
    assert (tmp_path / "legacy-auth-removed").exists()

    (tmp_path / "auth_state.json").write_text("new", encoding="utf-8")
    legacy_auth.remove_legacy_auth_state(tmp_path)
    assert (tmp_path / "auth_state.json").exists()


def test_missing_token_still_completes_cleanup(
    tmp_path: Path, passwords: dict[tuple[str, str], str]
) -> None:
    legacy_auth.remove_legacy_auth_state(tmp_path / "fresh")

    assert (tmp_path / "fresh" / "legacy-auth-removed").exists()


def test_keyring_failure_is_reported_and_retried_later(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unavailable(_service: str, _user: str) -> None:
        raise RuntimeError("no keyring backend")

    monkeypatch.setattr(keyring, "delete_password", unavailable)

    legacy_auth.remove_legacy_auth_state(tmp_path)

    assert not (tmp_path / "legacy-auth-removed").exists()
    assert "eski oturum temizliği tamamlanamadı" in capsys.readouterr().err
