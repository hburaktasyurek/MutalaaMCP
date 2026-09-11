"""Deterministic security boundaries for local persistence and OCR downloads."""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mutalaamcp import ocr
from mutalaamcp.cache.store import CacheError, CacheStore
from mutalaamcp.ocr import (
    OcrArtifact,
    OcrDownload,
    OcrDownloadError,
    OcrManifest,
    install_ocr,
)
from mutalaamcp.settings import Settings


class _Response:
    def __init__(
        self, status: int, *, body: bytes = b"", location: str | None = None
    ) -> None:
        self._body = body
        self._status = status
        self.headers: dict[str, str] = {}
        if location is not None:
            self.headers["Location"] = location
        if status == 200:
            self.headers["Content-Length"] = str(len(body))

    def close(self) -> None:
        pass

    def getcode(self) -> int:
        return self._status

    def read(self, _size: int) -> bytes:
        body, self._body = self._body, b""
        return body


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
        model_dir=tmp_path / "models",
        ocr_allowed_hosts=frozenset({"models.test"}),
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_ocr_redirect_chain_rejects_private_target_before_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter(
        (
            _Response(302, location="/intermediate"),
            _Response(302, location="https://127.0.0.1/internal"),
        )
    )
    requested: list[str] = []

    def open_request(request: Any, *, timeout: float) -> _Response:
        assert timeout == 30
        requested.append(request.full_url)
        return next(responses)

    monkeypatch.setattr(ocr, "_open_ocr_request", open_request)
    target = tmp_path / "artifact"

    with pytest.raises(
        OcrDownloadError,
        match="OCR yapıtı URL'si izin listesindeki bir HTTPS ana makinesini kullanmalıdır",
    ):
        ocr._download_artifact(
            "https://models.test/model.bin",
            target,
            allowed_hosts={"models.test"},
            max_bytes=1024,
        )

    assert requested == [
        "https://models.test/model.bin",
        "https://models.test/intermediate",
    ]
    assert not target.exists()


def test_ocr_redirect_chain_has_a_bounded_hop_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[str] = []

    def open_request(request: Any, *, timeout: float) -> _Response:
        assert timeout == 30
        requested.append(request.full_url)
        return _Response(302, location=f"/hop-{len(requested)}")

    monkeypatch.setattr(ocr, "_open_ocr_request", open_request)

    with pytest.raises(OcrDownloadError, match="OCR yapıtı yönlendirme sınırı aşıldı"):
        ocr._download_artifact(
            "https://models.test/model.bin",
            tmp_path / "artifact",
            allowed_hosts={"models.test"},
            max_bytes=1024,
        )

    assert len(requested) == 4


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode enforcement")
def test_cache_files_override_a_permissive_umask(tmp_path: Path) -> None:
    database = tmp_path / "cache" / "legal-content.sqlite"
    previous_umask = os.umask(0)
    try:
        with CacheStore(database) as store:
            moment = datetime(2026, 1, 1, tzinfo=UTC)
            store.put(
                namespace="legislation",
                key="law-1",
                source_url="https://mevzuat.test/law-1",
                content="private legal content",
                fetched_at=moment,
                validated_at=moment,
                expires_at=None,
                now=moment,
            )
            assert _mode(database.parent) == 0o700
            for path in (
                database,
                Path(f"{database}-wal"),
                Path(f"{database}-shm"),
            ):
                assert path.is_file()
                assert _mode(path) == 0o600
    finally:
        os.umask(previous_umask)


def test_cache_rejects_a_symbolic_link_database(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    target.write_bytes(b"not a database")
    link = tmp_path / "cache.sqlite"
    link.symlink_to(target)

    with pytest.raises(CacheError, match="normal bir dosya"):
        CacheStore(link)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode enforcement")
def test_ocr_installation_files_override_a_permissive_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"model payload"
    settings = _settings(tmp_path)
    artifact = OcrArtifact(
        platform=ocr.current_platform_tag(),
        downloads=(
            OcrDownload(
                name="model",
                url="https://models.test/model.bin",
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                archive="file",
            ),
        ),
        files=("model.bin",),
    )
    manifest = OcrManifest(
        schema_version=1,
        model_id="test-model",
        runtime="paddleocr",
        runtime_version=None,
        runtime_options={},
        artifacts={artifact.platform: artifact},
    )

    def write_download(_url: str, target: Path, **_kwargs: object) -> None:
        target.write_bytes(payload)

    monkeypatch.setattr(ocr, "_download_artifact", write_download)
    previous_umask = os.umask(0)
    try:
        install_ocr(settings, consent=True, manifest=manifest)
    finally:
        os.umask(previous_umask)

    install_dir = settings.model_dir / "installs" / f"test-model-{artifact.sha256[:16]}"
    for directory in (
        settings.model_dir,
        settings.model_dir / "installs",
        install_dir,
    ):
        assert _mode(directory) == 0o700
    for path in (
        settings.model_dir / "active.json",
        install_dir / "installation.json",
        install_dir / "model.bin",
    ):
        assert _mode(path) == 0o600
