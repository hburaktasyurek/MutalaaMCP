"""Deterministic OCR and updater lifecycle tests; no user paths or network."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import plistlib
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from mutalaamcp import native_service
from mutalaamcp import ocr as ocr_module
from mutalaamcp.launcher import ensure_stable_launcher
from mutalaamcp.ocr import (
    OcrArtifact,
    OcrDownload,
    OcrDownloadError,
    OcrError,
    OcrManifest,
    current_platform_tag,
    install_ocr,
)
from mutalaamcp.settings import Settings
from mutalaamcp.update import (
    ActiveVersion,
    PackageIntegrity,
    UpdateError,
    UpdateOffer,
    VerifiedUpdateManifest,
    auto_update_loop,
    backup_cache,
    fetch_update_offer,
    install_candidate,
    is_newer_version,
    update_to_version,
)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "cache_dir": tmp_path / "cache",
        "data_dir": tmp_path / "data",
        "model_dir": tmp_path / "models",
        "ocr_allowed_hosts": frozenset({"models.test"}),
    }
    values.update(overrides)
    return Settings(**values)


def _update_manifest(version: str = "2.0.0") -> VerifiedUpdateManifest:
    """Trusted local fixture: every installed distribution is hash-bound."""

    return VerifiedUpdateManifest(
        index_url="https://packages.test/simple",
        package=PackageIntegrity(
            name="mutalaamcp",
            version=version,
            hashes=("0" * 64,),
        ),
        dependencies=(
            PackageIntegrity(
                name="fastmcp",
                version="3.0.0",
                hashes=("1" * 64,),
            ),
        ),
    )


def _manifest(*, url: str, payload: bytes) -> OcrManifest:
    return OcrManifest(
        schema_version=1,
        model_id="paddle-test",
        runtime="paddleocr",
        runtime_version="test",
        runtime_options={},
        artifacts={
            current_platform_tag(): OcrArtifact(
                platform=current_platform_tag(),
                downloads=(
                    OcrDownload(
                        name="model",
                        url=url,
                        sha256=hashlib.sha256(payload).hexdigest(),
                        size_bytes=len(payload),
                        archive="file",
                    ),
                ),
                files=("model.bin",),
            )
        },
    )


def _tar_payload(path: str, data: bytes) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo(path)
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def test_ocr_install_requires_consent_before_any_model_write(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manifest = _manifest(url="https://models.test/model.bin", payload=b"model")

    with pytest.raises(OcrError, match="açık OCR kurulum onayı gereklidir"):
        install_ocr(settings, consent=False, manifest=manifest)

    assert not settings.model_dir.exists()


def test_ocr_install_rejects_non_allowlisted_or_non_https_source(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manifest = _manifest(url="http://models.test/model.bin", payload=b"model")

    with pytest.raises(
        OcrDownloadError,
        match="OCR yapıtı URL'si izin listesindeki bir HTTPS ana makinesini kullanmalıdır",
    ):
        install_ocr(settings, consent=True, manifest=manifest)

    assert not settings.model_dir.exists()


def test_ocr_checksum_failure_never_activates_partial_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    manifest = _manifest(url="https://models.test/model.bin", payload=b"expected")

    def wrong_download(*args: object, **_kwargs: object) -> None:
        target = args[1]
        assert isinstance(target, Path)
        target.write_bytes(b"wrong")

    monkeypatch.setattr("mutalaamcp.ocr._download_artifact", wrong_download)
    with pytest.raises(
        OcrDownloadError, match="OCR yapıtı boyutu manifestiyle eşleşmiyor"
    ):
        install_ocr(settings, consent=True, manifest=manifest)

    assert not (settings.model_dir / "active.json").exists()
    assert not (settings.model_dir / "installs").exists()


def test_ocr_install_combines_pinned_tar_downloads_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = {
        "https://models.test/detection.tar": _tar_payload(
            "detection/inference.json", b"detection"
        ),
        "https://models.test/recognition.tar": _tar_payload(
            "recognition/inference.json", b"recognition"
        ),
    }
    artifact = OcrArtifact(
        platform=current_platform_tag(),
        downloads=tuple(
            OcrDownload(
                name=name,
                url=url,
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                archive="tar",
            )
            for name, (url, payload) in zip(
                ("detection", "recognition"), payloads.items(), strict=True
            )
        ),
        files=("detection/inference.json", "recognition/inference.json"),
    )
    manifest = OcrManifest(
        schema_version=2,
        model_id="paddle-test",
        runtime="paddleocr",
        runtime_version="test",
        runtime_options={},
        artifacts={artifact.platform: artifact},
    )

    def download(url: str, target: Path, **_kwargs: object) -> None:
        target.write_bytes(payloads[url])

    monkeypatch.setattr("mutalaamcp.ocr._download_artifact", download)

    class PaddleRuntime:
        __version__ = "test"

        class PaddleOCR:
            pass

    monkeypatch.setattr(
        "mutalaamcp.ocr.importlib.import_module", lambda _name: PaddleRuntime
    )

    status = install_ocr(_settings(tmp_path), consent=True, manifest=manifest)

    assert status.status == "ready"
    assert status.install_dir is not None
    assert (
        status.install_dir / "detection/inference.json"
    ).read_bytes() == b"detection"
    assert (status.install_dir / "recognition/inference.json").read_bytes() == (
        b"recognition"
    )


def test_local_ocr_resolves_model_dirs_and_uses_supported_image_suffix(
    tmp_path: Path,
) -> None:
    model_dir = tmp_path / "detection"
    model_dir.mkdir()
    observed: dict[str, object] = {}

    class Engine:
        def predict(self, path: str) -> list[dict[str, list[str]]]:
            observed["suffix"] = Path(path).suffix
            return [{"rec_texts": ["TÜRKİYE", "MADDE 42"]}]

    def factory(**options: object) -> Engine:
        observed["model_dir"] = options["text_detection_model_dir"]
        return Engine()

    adapter = ocr_module._LocalPaddleOcr(
        factory,
        tmp_path,
        {"runtime_options": {"text_detection_model_dir": "detection"}},
    )

    assert (
        adapter(b"\x89PNG\r\n\x1a\npayload", "memory://legal-scan")
        == "TÜRKİYE\nMADDE 42"
    )
    assert observed == {
        "suffix": ".png",
        "model_dir": str(model_dir),
    }


def test_install_candidate_uses_fake_absolute_uv(tmp_path: Path) -> None:
    calls = tmp_path / "calls.jsonl"
    candidate_root = tmp_path / "candidate"
    manifest = _update_manifest()
    uv = _fake_uv(tmp_path / "bin" / "uv", calls)
    candidate = install_candidate(
        uv,
        "2.0.0",
        candidate_root,
        manifest=manifest,
    )

    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(recorded) == 2
    expected_python = candidate_root / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    assert uv.is_absolute()
    assert recorded[0] == [
        "venv",
        "--python",
        getattr(sys, "_base_executable", sys.executable),
        str(candidate_root),
    ]
    assert recorded[1][:11] == [
        "pip",
        "install",
        "--python",
        str(expected_python),
        "--require-hashes",
        "--no-deps",
        "--only-binary",
        ":all:",
        "--no-cache",
        "--index-url",
        manifest.index_url,
    ]
    assert recorded[1][-2] == "-r"
    requirements = Path(recorded[1][-1])
    assert requirements.parent == candidate_root
    assert requirements.name.startswith(".mutalaamcp-requirements-")
    assert candidate.version == "2.0.0"
    assert candidate.launcher.is_absolute()
    assert (
        candidate.launcher
        == (
            expected_python.parent
            / ("mutalaamcp.exe" if os.name == "nt" else "mutalaamcp")
        ).resolve()
    )


def test_install_candidate_fails_closed_without_complete_hash_manifest(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "candidate"
    uv = _fake_executable(tmp_path / "uv")

    with pytest.raises(
        UpdateError, match="Doğrulanmış bir güncelleme bütünlük bildirimi gerekli"
    ):
        install_candidate(uv, "2.0.0", candidate_root)

    unhashed = VerifiedUpdateManifest(
        index_url="https://packages.test/simple",
        package=PackageIntegrity(name="mutalaamcp", version="2.0.0", hashes=()),
        dependencies=(),
    )
    with pytest.raises(
        UpdateError,
        match=r"^Bütünlük bildirimindeki güncelleme paketi SHA-256 özetleri gerektirir\.$",
    ):
        install_candidate(uv, "2.0.0", candidate_root, manifest=unhashed)

    assert not candidate_root.exists()


def test_cache_backup_is_owner_only_on_posix(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX backup permissions do not apply")
    settings = _settings(tmp_path)
    _create_cache(settings.cache_db_path, user_version=1)

    backup = backup_cache(settings)

    assert backup is not None
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_update_health_failure_does_not_switch_active_version_or_touch_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    old_launcher = _fake_executable(tmp_path / "old" / "mutalaamcp")
    settings.active_version_state_path.parent.mkdir(parents=True)
    settings.active_version_state_path.write_text(
        json.dumps({"version": "1.0.0", "launcher": str(old_launcher)}),
        encoding="utf-8",
    )
    original_state = settings.active_version_state_path.read_bytes()
    _create_cache(settings.cache_db_path, user_version=1)
    original_cache_version = _cache_version(settings.cache_db_path)
    candidate_roots: list[Path] = []

    def install(
        _uv: Path,
        _version: str,
        root: Path,
        *,
        manifest: VerifiedUpdateManifest,
    ) -> ActiveVersion:
        assert manifest == _update_manifest()
        candidate_roots.append(root)
        return ActiveVersion("2.0.0", _fake_executable(root / "bin" / "mutalaamcp"))

    monkeypatch.setattr("mutalaamcp.update.install_candidate", install)

    def reject_health(
        _launcher: Path, checked_settings: Settings, backup: Path | None
    ) -> None:
        assert checked_settings is settings
        assert backup is not None
        assert _cache_version(backup) == original_cache_version
        assert settings.active_version_state_path.read_bytes() == original_state
        raise UpdateError("candidate failed health")

    with pytest.raises(UpdateError, match="candidate failed health"):
        update_to_version(
            settings,
            "2.0.0",
            current_launcher=old_launcher,
            health_check=reject_health,
            uv_executable=_fake_executable(tmp_path / "uv"),
            integrity_manifest=_update_manifest(),
        )

    assert settings.active_version_state_path.read_bytes() == original_state
    assert _cache_version(settings.cache_db_path) == original_cache_version
    assert candidate_roots and not candidate_roots[0].exists()


def test_update_post_switch_failure_restores_active_state_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    old_launcher = _fake_executable(tmp_path / "old" / "mutalaamcp")
    settings.active_version_state_path.parent.mkdir(parents=True)
    settings.active_version_state_path.write_text(
        json.dumps({"version": "1.0.0", "launcher": str(old_launcher)}),
        encoding="utf-8",
    )
    original_state = settings.active_version_state_path.read_bytes()
    _create_cache(settings.cache_db_path, user_version=1)
    candidate_launcher = _fake_executable(tmp_path / "candidate" / "mutalaamcp")

    monkeypatch.setattr(
        "mutalaamcp.update.install_candidate",
        lambda *_args, **_kwargs: ActiveVersion("2.0.0", candidate_launcher),
    )

    def reject_post(_launcher: Path, _settings: Settings, _backup: Path | None) -> None:
        selected = json.loads(settings.active_version_state_path.read_text())
        assert selected["launcher"] == str(candidate_launcher)
        rollback = selected["rollback"]
        assert rollback["version"] == "1.0.0"
        assert rollback["launcher"] == str(old_launcher)
        assert rollback["cache_path"] == str(settings.cache_db_path)
        assert isinstance(rollback["cache_backup"], str)
        assert Path(rollback["cache_backup"]).is_file()
        raise UpdateError("first active launch failed")

    with pytest.raises(UpdateError, match="first active launch failed"):
        update_to_version(
            settings,
            "2.0.0",
            current_launcher=old_launcher,
            health_check=lambda _launcher, _settings, _backup: None,
            post_switch_check=reject_post,
            uv_executable=_fake_executable(tmp_path / "uv"),
            integrity_manifest=_update_manifest(),
        )

    assert settings.active_version_state_path.read_bytes() == original_state
    assert _cache_version(settings.cache_db_path) == 1


def test_update_persists_cache_backup_until_first_stable_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    old_launcher = _fake_executable(tmp_path / "old" / "mutalaamcp")
    settings.active_version_state_path.parent.mkdir(parents=True)
    settings.active_version_state_path.write_text(
        json.dumps({"version": "1.0.0", "launcher": str(old_launcher)}),
        encoding="utf-8",
    )
    _create_cache(settings.cache_db_path, user_version=1)
    candidate_launcher = _fake_executable(tmp_path / "candidate" / "mutalaamcp")
    monkeypatch.setattr(
        "mutalaamcp.update.install_candidate",
        lambda *_args, **_kwargs: ActiveVersion("2.0.0", candidate_launcher),
    )

    update_to_version(
        settings,
        "2.0.0",
        current_launcher=old_launcher,
        health_check=lambda _launcher, _settings, _backup: None,
        uv_executable=_fake_executable(tmp_path / "uv"),
        integrity_manifest=_update_manifest(),
    )

    rollback = json.loads(settings.active_version_state_path.read_text())["rollback"]
    assert rollback["cache_path"] == str(settings.cache_db_path)
    assert isinstance(rollback["cache_backup"], str)
    assert Path(rollback["cache_backup"]).is_file()


def test_stable_launcher_commits_after_runtime_ready_signal(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    old_launcher = _fake_executable(tmp_path / "old" / "mutalaamcp")
    _create_cache(settings.cache_db_path, user_version=1)
    backup = backup_cache(settings)
    assert backup is not None
    candidate_root = settings.version_root / ".candidate-2.0.0-ready"
    candidate_path = candidate_root / "bin" / "mutalaamcp"
    readiness_record = tmp_path / "readiness-path"
    candidate = _fake_python_executable(
        candidate_path,
        "\n".join(
            (
                f"#!{sys.executable}",
                "import os",
                "import sqlite3",
                "import stat",
                "import tempfile",
                "from pathlib import Path",
                f"marker_record = Path({str(readiness_record)!r})",
                "marker = Path(os.environ['MUTALAAMCP_RUNTIME_READY_FILE'])",
                "marker_record.write_text(",
                "    str(marker) + '\\n' + oct(stat.S_IMODE(marker.stat().st_mode)),",
                "    encoding='utf-8',",
                ")",
                f"with sqlite3.connect({str(settings.cache_db_path)!r}) as connection:",
                "    connection.execute('PRAGMA user_version = 2')",
                "descriptor, temporary = tempfile.mkstemp(",
                "    prefix='.runtime-ready-', suffix='.tmp', dir=marker.parent",
                ")",
                "with os.fdopen(descriptor, 'wb') as output:",
                "    if os.name == 'posix':",
                "        os.fchmod(output.fileno(), 0o600)",
                "    output.write(b'ready\\n')",
                "    output.flush()",
                "    os.fsync(output.fileno())",
                "os.replace(temporary, marker)",
                "",
            )
        ),
    )
    settings.active_version_state_path.parent.mkdir(parents=True, exist_ok=True)
    settings.active_version_state_path.write_text(
        json.dumps(
            {
                "version": "2.0.0",
                "launcher": str(candidate),
                "rollback": {
                    "version": "1.0.0",
                    "launcher": str(old_launcher),
                    "cache_path": str(settings.cache_db_path),
                    "cache_backup": str(backup),
                },
            }
        ),
        encoding="utf-8",
    )

    stable = ensure_stable_launcher(settings.data_dir, old_launcher)
    first = subprocess.run([str(stable), "serve"], check=False)

    assert first.returncode == 0
    assert json.loads(settings.active_version_state_path.read_text()) == {
        "version": "2.0.0",
        "launcher": str(candidate),
    }
    assert _cache_version(settings.cache_db_path) == 2
    assert not backup.exists()
    assert candidate_root.exists()
    readiness_path, mode = readiness_record.read_text(encoding="utf-8").splitlines()
    marker = Path(readiness_path)
    assert marker.parent == settings.data_dir.resolve()
    assert marker.name.startswith(".mutalaamcp-runtime-ready-")
    assert marker.suffix == ".marker"
    if os.name == "posix":
        assert mode == "0o600"
    assert not marker.exists()


def test_stable_launcher_rolls_back_failed_first_candidate_serve(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    old_launcher = _fake_executable(tmp_path / "old" / "mutalaamcp")
    _create_cache(settings.cache_db_path, user_version=1)
    backup = backup_cache(settings)
    assert backup is not None
    candidate_root = settings.version_root / ".candidate-2.0.0-test"
    candidate_path = candidate_root / "bin" / "mutalaamcp"
    candidate = _fake_python_executable(
        candidate_path,
        "\n".join(
            (
                f"#!{sys.executable}",
                "import sqlite3",
                f"with sqlite3.connect({str(settings.cache_db_path)!r}) as connection:",
                "    connection.execute('PRAGMA user_version = 2')",
                "raise SystemExit(23)",
                "",
            )
        ),
    )
    settings.active_version_state_path.parent.mkdir(parents=True, exist_ok=True)
    settings.active_version_state_path.write_text(
        json.dumps(
            {
                "version": "2.0.0",
                "launcher": str(candidate),
                "rollback": {
                    "version": "1.0.0",
                    "launcher": str(old_launcher),
                    "cache_path": str(settings.cache_db_path),
                    "cache_backup": str(backup),
                },
            }
        ),
        encoding="utf-8",
    )

    stable = ensure_stable_launcher(settings.data_dir, old_launcher)
    first = subprocess.run([str(stable), "serve"], check=False)

    assert first.returncode == 23
    assert json.loads(settings.active_version_state_path.read_text()) == {
        "version": "1.0.0",
        "launcher": str(old_launcher),
    }
    assert _cache_version(settings.cache_db_path) == 1
    assert not backup.exists()
    assert not candidate_root.exists()
    assert not list(settings.data_dir.glob(".mutalaamcp-runtime-ready-*.marker"))
    assert subprocess.run([str(stable), "serve"], check=False).returncode == 0


def test_stable_launcher_times_out_live_unavailable_runtime_and_rolls_back(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    old_launcher = _fake_executable(tmp_path / "old" / "mutalaamcp")
    _create_cache(settings.cache_db_path, user_version=1)
    backup = backup_cache(settings)
    assert backup is not None
    candidate_root = settings.version_root / ".candidate-2.0.0-unavailable"
    candidate_path = candidate_root / "bin" / "mutalaamcp"
    pid_path = tmp_path / "candidate.pid"
    readiness_record = tmp_path / "readiness-path"
    candidate = _fake_python_executable(
        candidate_path,
        "\n".join(
            (
                f"#!{sys.executable}",
                "import os",
                "import sqlite3",
                "import time",
                "from pathlib import Path",
                f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')",
                f"Path({str(readiness_record)!r}).write_text(",
                "    os.environ['MUTALAAMCP_RUNTIME_READY_FILE'], encoding='utf-8'",
                ")",
                f"connection = sqlite3.connect({str(settings.cache_db_path)!r})",
                "try:",
                "    connection.execute('PRAGMA user_version = 2')",
                "finally:",
                "    connection.close()",
                "time.sleep(60)",
                "",
            )
        ),
    )
    settings.active_version_state_path.parent.mkdir(parents=True, exist_ok=True)
    settings.active_version_state_path.write_text(
        json.dumps(
            {
                "version": "2.0.0",
                "launcher": str(candidate),
                "rollback": {
                    "version": "1.0.0",
                    "launcher": str(old_launcher),
                    "cache_path": str(settings.cache_db_path),
                    "cache_backup": str(backup),
                },
            }
        ),
        encoding="utf-8",
    )

    stable = ensure_stable_launcher(settings.data_dir, old_launcher)
    selector = stable.with_name("mutalaamcp-selector.py") if os.name == "nt" else stable
    source = selector.read_text(encoding="utf-8")
    selector.write_text(
        source.replace(
            "_RUNTIME_READY_TIMEOUT_SECONDS = 5.0",
            "_RUNTIME_READY_TIMEOUT_SECONDS = 1.0",
        ),
        encoding="utf-8",
    )
    try:
        first = subprocess.run([str(stable), "serve"], check=False, timeout=5)
    finally:
        _terminate_process(pid_path)

    assert first.returncode != 0
    assert json.loads(settings.active_version_state_path.read_text()) == {
        "version": "1.0.0",
        "launcher": str(old_launcher),
    }
    assert _cache_version(settings.cache_db_path) == 1
    assert not backup.exists()
    assert not candidate_root.exists()
    marker = Path(readiness_record.read_text(encoding="utf-8"))
    assert marker.parent == settings.data_dir.resolve()
    assert marker.name.startswith(".mutalaamcp-runtime-ready-")
    assert not marker.exists()
    assert not list(settings.data_dir.glob(".mutalaamcp-runtime-ready-*.marker"))
    if os.name == "posix":
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_path.read_text(encoding="utf-8")), 0)


def test_update_retains_cache_backup_when_rollback_restore_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    old_launcher = _fake_executable(tmp_path / "old" / "mutalaamcp")
    settings.active_version_state_path.parent.mkdir(parents=True)
    settings.active_version_state_path.write_text(
        json.dumps({"version": "1.0.0", "launcher": str(old_launcher)}),
        encoding="utf-8",
    )
    _create_cache(settings.cache_db_path, user_version=1)
    candidate_launcher = _fake_executable(tmp_path / "candidate" / "mutalaamcp")
    monkeypatch.setattr(
        "mutalaamcp.update.install_candidate",
        lambda *_args, **_kwargs: ActiveVersion("2.0.0", candidate_launcher),
    )
    retained: list[Path] = []

    def fail_restore(_settings: Settings, backup: str | Path) -> None:
        retained.append(Path(backup))
        raise OSError("restore device failed")

    def fail_after_switch(
        _launcher: Path, _settings: Settings, _backup: Path | None
    ) -> None:
        raise UpdateError("candidate exited unsuccessfully")

    monkeypatch.setattr("mutalaamcp.update.restore_cache", fail_restore)
    with pytest.raises(UpdateError, match="yedek şu konumda korundu") as error:
        update_to_version(
            settings,
            "2.0.0",
            current_launcher=old_launcher,
            health_check=lambda _launcher, _settings, _backup: None,
            post_switch_check=fail_after_switch,
            uv_executable=_fake_executable(tmp_path / "uv"),
            integrity_manifest=_update_manifest(),
        )

    assert retained and retained[0].is_file()
    assert str(retained[0]) in str(error.value)


def _fake_uv(path: Path, calls: Path) -> Path:
    return _fake_python_executable(
        path,
        "\n".join(
            (
                f"#!{sys.executable}",
                "import json",
                "import os",
                "import sys",
                "from pathlib import Path",
                f"log = Path({str(calls)!r})",
                "with log.open('a', encoding='utf-8') as output:",
                "    output.write(json.dumps(sys.argv[1:]) + '\\n')",
                "if sys.argv[1] == 'venv':",
                "    root = Path(sys.argv[-1])",
                "    scripts = root / ('Scripts' if os.name == 'nt' else 'bin')",
                "    scripts.mkdir(parents=True)",
                "    (scripts / ('python.exe' if os.name == 'nt' else 'python')).touch()",
                "else:",
                "    python = Path(sys.argv[4])",
                "    launcher = python.parent / (",
                "        'mutalaamcp.exe' if os.name == 'nt' else 'mutalaamcp'",
                "    )",
                "    launcher.touch()",
                "    if os.name == 'posix':",
                "        launcher.chmod(0o755)",
                "",
            )
        ),
    )


def _fake_executable(path: Path) -> Path:
    return _fake_python_executable(path, f"#!{sys.executable}\n")


def _fake_python_executable(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        script = path.with_suffix(".py")
        script.write_text(source, encoding="utf-8")
        executable = path.with_suffix(".cmd")
        executable.write_text(
            "\n".join(
                (
                    "@echo off",
                    f'"{sys.executable}" "{script}" %*',
                    "exit /b %ERRORLEVEL%",
                    "",
                )
            ),
            encoding="utf-8",
        )
    else:
        executable = path
        executable.write_text(source, encoding="utf-8")
        executable.chmod(0o755)
    return executable.resolve()


def _terminate_process(pid_path: Path) -> None:
    try:
        pid = int(pid_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _create_cache(path: Path, *, user_version: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {user_version}")


def _cache_version(path: Path) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _offer_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "manifest_version": 1,
        "version": "2.0.0",
        "index_url": "https://pypi.org/simple",
        "package": {
            "name": "mutalaamcp",
            "version": "2.0.0",
            "url": "https://releases.test/mutalaamcp-2.0.0-py3-none-any.whl",
            "hashes": ["0" * 64],
        },
        "dependencies": [
            {
                "name": "fastmcp",
                "version": "3.0.0",
                "hashes": ["1" * 64],
                "marker": "sys_platform == 'win32'",
            }
        ],
    }
    payload.update(overrides)
    return payload


def _offer_client(body: object, status: int = 200) -> httpx.Client:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(status, content=content)
    )
    return httpx.Client(transport=transport)


def test_fetch_update_offer_binds_version_urls_hashes_and_markers() -> None:
    offer = fetch_update_offer(
        _offer_client(_offer_payload()), "https://releases.test/update.json"
    )

    assert offer.version == "2.0.0"
    assert offer.manifest.index_url == "https://pypi.org/simple"
    assert offer.manifest.package.url == (
        "https://releases.test/mutalaamcp-2.0.0-py3-none-any.whl"
    )
    assert offer.manifest.package.hashes == ("0" * 64,)
    dependency = offer.manifest.dependencies[0]
    assert dependency.marker == "sys_platform == 'win32'"


def test_fetch_update_offer_requires_https_channel() -> None:
    with pytest.raises(UpdateError, match="HTTPS"):
        fetch_update_offer(_offer_client(_offer_payload()), "http://releases.test/x")


def test_fetch_update_offer_rejects_non_ok_and_invalid_json() -> None:
    with pytest.raises(UpdateError, match="HTTP 404"):
        fetch_update_offer(_offer_client(b"{}", status=404), "https://releases.test/x")
    with pytest.raises(UpdateError, match="geçerli JSON"):
        fetch_update_offer(_offer_client(b"{"), "https://releases.test/x")


@pytest.mark.parametrize(
    "package",
    [
        {
            "name": "mutalaamcp",
            "version": "2.0.0",
            "url": "http://releases.test/mutalaamcp-2.0.0-py3-none-any.whl",
            "hashes": ["0" * 64],
        },
        {
            "name": "mutalaamcp",
            "version": "2.0.0",
            "url": "https://user:pw@releases.test/mutalaamcp-2.0.0-py3-none-any.whl",
            "hashes": ["0" * 64],
        },
        {
            "name": "mutalaamcp",
            "version": "2.0.0",
            "url": "https://releases.test/wheel.whl?token=abc",
            "hashes": ["0" * 64],
        },
        {"name": "mutalaamcp", "version": "2.0.0", "hashes": []},
        {
            "name": "mutalaamcp",
            "version": "2.0.0",
            "hashes": ["not-a-sha"],
        },
        {"name": "mutalaamcp", "version": "9.9.9", "hashes": ["0" * 64]},
        {"name": "other", "version": "2.0.0", "hashes": ["0" * 64]},
    ],
)
def test_fetch_update_offer_rejects_unbound_or_insecure_package(
    package: dict[str, object],
) -> None:
    with pytest.raises(UpdateError):
        fetch_update_offer(
            _offer_client(_offer_payload(package=package)),
            "https://releases.test/x",
        )


def test_fetch_update_offer_rejects_unknown_manifest_version_and_duplicates() -> None:
    with pytest.raises(UpdateError, match="sürümü desteklenmiyor"):
        fetch_update_offer(
            _offer_client(_offer_payload(manifest_version=2)),
            "https://releases.test/x",
        )
    duplicate = {"name": "fastmcp", "version": "3.0.1", "hashes": ["2" * 64]}
    dependencies = [*_offer_payload()["dependencies"], duplicate]
    with pytest.raises(UpdateError, match="yinelenen"):
        fetch_update_offer(
            _offer_client(_offer_payload(dependencies=dependencies)),
            "https://releases.test/x",
        )


def test_fetch_update_offer_bounds_manifest_size() -> None:
    oversized = b" " * (262_144 + 1)
    with pytest.raises(UpdateError, match="boyutu"):
        fetch_update_offer(_offer_client(oversized), "https://releases.test/x")


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("2.0.0", "1.0.0", True),
        ("1.0.0", "2.0.0", False),
        ("2.0.0", "2.0.0", False),
        ("1.0.1", "1.0.0", True),
        ("2.0.0rc1", "2.0.0b2", True),
        ("2.0.0", "2.0.0rc3", True),
        ("2.0.0a2", "2.0.0a1", True),
        ("nightly", "1.0.0", False),
        ("2.0.0", "installed", False),
    ],
)
def test_is_newer_version_orders_release_tracks(
    candidate: str, current: str, expected: bool
) -> None:
    assert is_newer_version(candidate, current) is expected


def test_auto_update_loop_applies_verified_offer_and_stops() -> None:
    delays: list[float] = []
    applied: list[str] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    attempts = iter((False, True))

    async def run() -> None:
        await auto_update_loop(
            is_managed=lambda: True,
            check_and_apply=lambda: next(attempts),
            on_updated=lambda: applied.append("restarted"),
            interval_seconds=10.0,
            first_delay_seconds=1.0,
            sleep=sleep,
        )

    asyncio.run(run())

    assert delays == [1.0, 10.0]
    assert applied == ["restarted"]


def test_auto_update_loop_skips_checks_while_unmanaged() -> None:
    checks: list[str] = []
    managed = iter((False, False, True))

    async def run() -> None:
        await auto_update_loop(
            is_managed=lambda: next(managed),
            check_and_apply=lambda: checks.append("checked") or True,
            on_updated=lambda: None,
            interval_seconds=10.0,
            first_delay_seconds=1.0,
            sleep=lambda _seconds: asyncio.sleep(0),
        )

    asyncio.run(run())

    assert checks == ["checked"]


def test_auto_update_loop_survives_channel_failures() -> None:
    outcomes = iter((RuntimeError("offline"), True))
    applied: list[str] = []

    def check() -> bool:
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def run() -> None:
        await auto_update_loop(
            is_managed=lambda: True,
            check_and_apply=check,
            on_updated=lambda: applied.append("restarted"),
            interval_seconds=10.0,
            first_delay_seconds=1.0,
            sleep=lambda _seconds: asyncio.sleep(0),
        )

    asyncio.run(run())

    assert applied == ["restarted"]


def test_stable_launcher_forwards_serve_http_to_selected_version(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    argv_record = tmp_path / "argv.json"
    child = _fake_python_executable(
        tmp_path / "current" / "mutalaamcp",
        "\n".join(
            (
                f"#!{sys.executable}",
                "import json",
                "import sys",
                "from pathlib import Path",
                f"Path({str(argv_record)!r}).write_text(",
                "    json.dumps(sys.argv[1:]), encoding='utf-8'",
                ")",
                "",
            )
        ),
    )
    settings.active_version_state_path.parent.mkdir(parents=True)
    settings.active_version_state_path.write_text(
        json.dumps({"version": "1.0.0", "launcher": str(child)}),
        encoding="utf-8",
    )

    stable = ensure_stable_launcher(settings.data_dir, child)
    served = subprocess.run([str(stable), "serve-http"], check=False)

    assert served.returncode == 0
    assert json.loads(argv_record.read_text(encoding="utf-8")) == ["serve-http"]

    argv_record.unlink()
    rejected = subprocess.run([str(stable), "bogus"], check=False)
    assert rejected.returncode == 1
    assert not argv_record.exists()


def test_stable_launcher_forwards_serve_http_through_candidate_rollback(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    old_launcher = _fake_executable(tmp_path / "old" / "mutalaamcp")
    _create_cache(settings.cache_db_path, user_version=1)
    backup = backup_cache(settings)
    assert backup is not None
    argv_record = tmp_path / "candidate-argv.json"
    candidate_path = (
        settings.version_root / ".candidate-2.0.0-http" / "bin" / "mutalaamcp"
    )
    candidate = _fake_python_executable(
        candidate_path,
        "\n".join(
            (
                f"#!{sys.executable}",
                "import json",
                "import os",
                "import sys",
                "from pathlib import Path",
                f"Path({str(argv_record)!r}).write_text(",
                "    json.dumps(sys.argv[1:]), encoding='utf-8'",
                ")",
                "marker = Path(os.environ['MUTALAAMCP_RUNTIME_READY_FILE'])",
                "marker.write_bytes(b'ready\\n')",
                "",
            )
        ),
    )
    settings.active_version_state_path.parent.mkdir(parents=True, exist_ok=True)
    settings.active_version_state_path.write_text(
        json.dumps(
            {
                "version": "2.0.0",
                "launcher": str(candidate),
                "rollback": {
                    "version": "1.0.0",
                    "launcher": str(old_launcher),
                    "cache_path": str(settings.cache_db_path),
                    "cache_backup": str(backup),
                },
            }
        ),
        encoding="utf-8",
    )

    stable = ensure_stable_launcher(settings.data_dir, old_launcher)
    served = subprocess.run([str(stable), "serve-http"], check=False)

    assert served.returncode == 0
    assert json.loads(argv_record.read_text(encoding="utf-8")) == ["serve-http"]
    assert json.loads(settings.active_version_state_path.read_text()) == {
        "version": "2.0.0",
        "launcher": str(candidate),
    }


def test_service_targets_selector_only_when_registration_uses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    settings = _settings(tmp_path)
    selector = settings.data_dir / "mutalaamcp"
    selector.parent.mkdir(parents=True)
    selector.write_text("#selector\n", encoding="utf-8")
    plist_dir = home / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True)
    plist = plist_dir / "tr.mutalaa.mcp.plist"

    assert native_service.service_targets_selector(settings) is False

    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "tr.mutalaa.mcp",
                "ProgramArguments": [str(selector), "serve-http"],
            }
        )
    )
    assert native_service.service_targets_selector(settings) is True

    other = _fake_executable(tmp_path / "other" / "mutalaamcp")
    plist.write_bytes(
        plistlib.dumps(
            {"Label": "tr.mutalaa.mcp", "ProgramArguments": [str(other), "serve-http"]}
        )
    )
    assert native_service.service_targets_selector(settings) is False


def test_managed_update_check_applies_only_strictly_newer_offers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    offer = UpdateOffer(version="2.0.0", manifest=_update_manifest())
    monkeypatch.setattr(
        "mutalaamcp.update.fetch_update_offer", lambda _client, _url: offer
    )
    installs: list[dict[str, object]] = []
    monkeypatch.setattr(
        "mutalaamcp.update.update_to_version",
        lambda _settings, version, **kwargs: (
            installs.append({"version": version, **kwargs}) or None
        ),
    )
    monkeypatch.setattr(native_service, "__version__", "1.0.0")

    assert native_service._check_and_apply_update(settings) is True
    assert installs and installs[0]["version"] == "2.0.0"
    assert installs[0]["integrity_manifest"] == offer.manifest

    installs.clear()
    monkeypatch.setattr(native_service, "__version__", "2.0.0")
    assert native_service._check_and_apply_update(settings) is False
    assert installs == []


def test_fetch_update_offer_rejects_redirects_off_https() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "http":
            return httpx.Response(200, content=json.dumps(_offer_payload()).encode())
        return httpx.Response(
            302, headers={"location": "http://mirror.test/update.json"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(UpdateError, match="HTTPS olmayan"):
        fetch_update_offer(client, "https://releases.test/update.json")


def test_native_service_ready_accepts_a_newer_service_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "service": "MutalaaMCP",
            "version": "99.0.0",
            "transport": "http",
        },
    )
    monkeypatch.setattr(
        native_service, "httpx", SimpleNamespace(get=lambda *_a, **_k: response)
    )
    assert native_service.native_service_ready(settings) is True


def test_native_service_install_registers_selector_and_pins_absolute_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    launched: list[list[str]] = []
    monkeypatch.setattr(
        native_service,
        "subprocess",
        SimpleNamespace(
            run=lambda args, **_kwargs: (
                launched.append(list(args)) or SimpleNamespace(returncode=0)
            )
        ),
    )
    monkeypatch.setattr(native_service, "native_service_ready", lambda _settings: True)
    uv = _fake_executable(tmp_path / "uv-bin" / "uv")
    settings = _settings(tmp_path, uv_executable=uv)
    selector = _fake_executable(settings.data_dir / "mutalaamcp")

    native_service.install_native_service(settings, str(selector))

    plist_path = home / "Library" / "LaunchAgents" / "tr.mutalaa.mcp.plist"
    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["ProgramArguments"] == [str(selector), "serve-http"]
    environment = payload["EnvironmentVariables"]
    assert environment["MUTALAAMCP_UV_EXECUTABLE"] == str(uv)
    assert environment["MUTALAAMCP_UPDATE_MANIFEST_URL"] == (
        settings.update_manifest_url
    )
    assert payload["KeepAlive"] is True
    assert launched and any("bootstrap" in call for call in launched)

    # A PATH entry is pinned unresolved: a resolved versioned path such as a
    # brew Cellar binary would die on the next tool upgrade.
    link_dir = tmp_path / "pathbin"
    link_dir.mkdir()
    link = link_dir / "uv"
    link.symlink_to(uv)
    monkeypatch.setattr(
        native_service, "shutil", SimpleNamespace(which=lambda _name: str(link))
    )
    native_service.install_native_service(_settings(tmp_path / "other"), str(selector))
    environment = plistlib.loads(plist_path.read_bytes())["EnvironmentVariables"]
    assert environment["MUTALAAMCP_UV_EXECUTABLE"] == str(link)
