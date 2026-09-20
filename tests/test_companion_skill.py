"""Companion skill installation in isolated client homes."""

import hashlib
import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from mutalaamcp.companion_skill import SKILL_NAME, bundled_skill, setup_companion_skill


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "home"
    directory.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: directory))
    return directory


def test_codex_and_cursor_share_one_idempotent_install(
    home: Path, tmp_path: Path
) -> None:
    destination = home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    setup_companion_skill("codex", tmp_path / "data")
    assert destination.read_bytes() == bundled_skill()
    modified = destination.stat().st_mtime_ns
    setup_companion_skill("cursor", tmp_path / "data")
    assert destination.stat().st_mtime_ns == modified
    assert list(destination.parent.iterdir()) == [destination]


def test_update_keeps_previous_user_content(home: Path, tmp_path: Path) -> None:
    destination = home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("Kullanıcının önceki becerisi", encoding="utf-8")
    previous = destination.read_bytes()
    setup_companion_skill("codex", tmp_path / "data")
    assert destination.read_bytes() == bundled_skill()
    backups = list(destination.parent.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == previous
    setup_companion_skill("codex", tmp_path / "data")
    assert list(destination.parent.glob("*.bak")) == backups


def test_incomplete_backup_cannot_allow_loss_of_previous_skill(
    home: Path, tmp_path: Path
) -> None:
    destination = home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    destination.parent.mkdir(parents=True)
    previous = b"user customizations"
    destination.write_bytes(previous)
    digest = hashlib.sha256(previous).hexdigest()
    backup = destination.with_name(f"SKILL.md.{digest}.bak")
    backup.write_bytes(b"interrupted backup")
    with pytest.raises(OSError, match="doğrulanamadı"):
        setup_companion_skill("codex", tmp_path / "data")
    assert destination.read_bytes() == previous
    assert backup.read_bytes() == b"interrupted backup"


@pytest.mark.parametrize("first,second", [("codex", "cursor"), ("cursor", "codex")])
def test_other_clients_existing_skill_prevents_conflicting_shared_install(
    first: str, second: str, home: Path, tmp_path: Path
) -> None:
    existing = home / f".{first}" / "skills" / SKILL_NAME / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing skill")
    with pytest.raises(OSError, match="ortak"):
        setup_companion_skill(second, tmp_path / "data")
    assert not (home / ".agents").exists()
    assert existing.read_bytes() == b"existing skill"


@pytest.mark.parametrize("client", ["claude-desktop", "witsy"])
def test_import_clients_receive_same_skill_zip_not_false_installed_status(
    client: str, home: Path, tmp_path: Path
) -> None:
    message = setup_companion_skill(client, tmp_path / "data")
    with ZipFile(tmp_path / "data" / "skills" / f"{SKILL_NAME}.zip") as archive:
        assert archive.namelist() == [f"{SKILL_NAME}/SKILL.md"]
        assert archive.read(archive.namelist()[0]) == bundled_skill()
    assert "henüz uygulamaya kurulmuş sayılmaz" in message
    assert not (home / ".agents").exists()


@pytest.mark.parametrize("client", ["codex", "cursor"])
def test_existing_client_skill_is_updated_without_duplicate(
    client: str, home: Path, tmp_path: Path
) -> None:
    destination = home / f".{client}" / "skills" / SKILL_NAME / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("previous", encoding="utf-8")
    setup_companion_skill(client, tmp_path / "data")
    assert destination.read_bytes() == bundled_skill()
    assert not (home / ".agents").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink privilege")
def test_existing_skill_symlink_is_preserved(home: Path, tmp_path: Path) -> None:
    destination = home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    destination.parent.mkdir(parents=True)
    original = tmp_path / "original.md"
    original.write_text("custom", encoding="utf-8")
    destination.symlink_to(original)
    with pytest.raises(OSError, match="sembolik bağlantısı"):
        setup_companion_skill("codex", tmp_path / "data")
    assert destination.is_symlink()
    assert original.read_text() == "custom"


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink privilege")
def test_dangling_legacy_directory_is_not_bypassed(home: Path, tmp_path: Path) -> None:
    directory = home / ".codex" / "skills" / SKILL_NAME
    directory.parent.mkdir(parents=True)
    directory.symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(OSError, match="sembolik bağlantısı"):
        setup_companion_skill("codex", tmp_path / "data")
    assert directory.is_symlink()
    assert not (home / ".agents").exists()


def test_failed_atomic_replace_keeps_original_and_removes_temporary_file(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"previous")
    replace = os.replace

    def fail_install(source: Path, target: Path) -> None:
        if target == destination:
            raise OSError("simulated disk failure")
        replace(source, target)

    monkeypatch.setattr("mutalaamcp.companion_skill.os.replace", fail_install)
    with pytest.raises(OSError, match="disk failure"):
        setup_companion_skill("codex", tmp_path / "data")
    assert destination.read_bytes() == b"previous"
    backups = list(destination.parent.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"previous"
    assert set(destination.parent.iterdir()) == {destination, backups[0]}
