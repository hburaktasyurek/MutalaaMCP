"""OCR release manifest loading and unavailable-artifact behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mutalaamcp.ocr import current_platform_tag, inspect_ocr
from mutalaamcp.settings import Settings


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "cache_dir": tmp_path / "cache",
        "data_dir": tmp_path / "data",
        "model_dir": tmp_path / "models",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    ("platform_tag", "reason"),
    [
        ("darwin-arm64", "no_active_model"),
        ("windows-x64", "no_active_model"),
        ("linux-x64", "model_unavailable_for_platform"),
    ],
)
def test_bundled_manifest_declares_current_platform_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform_tag: str, reason: str
) -> None:
    monkeypatch.setattr("mutalaamcp.ocr.current_platform_tag", lambda: platform_tag)
    settings = _settings(tmp_path)

    assert inspect_ocr(settings).as_dict() == {
        "status": "not_configured",
        "reason": reason,
    }
    assert not settings.model_dir.exists()


def test_explicit_manifest_path_overrides_bundled_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "ocr-models.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model_id": "paddleocr-test",
                "runtime": "paddleocr",
                "runtime_version": "9.9.9",
                "runtime_options": {"lang": "tr"},
                "allowed_hosts": ["models.test"],
                "artifacts": {
                    current_platform_tag(): {
                        "url": "https://models.test/paddleocr-test.bin",
                        "sha256": "0" * 64,
                        "size_bytes": 1,
                        "archive": "file",
                        "files": ["model.bin"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    settings = _settings(
        tmp_path,
        ocr_manifest_path=manifest_path,
        ocr_allowed_hosts=frozenset({"models.test"}),
    )

    assert inspect_ocr(settings).as_dict() == {
        "status": "not_configured",
        "reason": "no_active_model",
    }
