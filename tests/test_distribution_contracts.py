"""Release manifests and supported client configuration contracts."""

from __future__ import annotations

import json
import tarfile
import tomllib
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest

from mutalaamcp import __version__
from mutalaamcp.cache.store import SCHEMA_VERSION
from mutalaamcp.client_templates import SUPPORTED_CLIENTS, render_client_config

ROOT = Path(__file__).parents[1]
RELEASE_DIR = ROOT / "release"
CLIENT_FIXTURES_DIR = RELEASE_DIR / "clients"
FIXTURE_LAUNCHER = "/opt/mutalaamcp/bin/mutalaamcp"
EXPECTED_FIXTURES = {
    "claude-desktop-v1.json",
    "codex-v1.toml",
    "cursor-v1.json",
    "google-antigravity-v1.json",
    "zcode-v1.json",
    "opencode-desktop-v1.json",
    "witsy-v1.json",
    "hermes-agent-v1.json",
    "cherry-studio-v1.json",
}
_SECRET_MARKERS = ("token", "secret", "authorization", "password", "refresh", "env")

_DEVELOPER_ARTIFACT_PARTS = frozenset(
    {
        ".hypothesis",
        ".omp",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".venv",
        ".idea",
        ".vscode",
        "dist",
        "build",
        "htmlcov",
        "__pycache__",
    }
)
_DEVELOPER_ARTIFACT_NAMES = frozenset({".coverage", "coverage.xml", ".DS_Store"})


def _fixture_data(path: Path) -> dict[str, Any]:
    if path.suffix == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
    elif path.suffix == ".toml":
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    else:
        raise AssertionError(f"Unsupported fixture format: {path}")
    assert isinstance(value, dict)
    return value


def _stdio_pair(value: dict[str, Any]) -> tuple[str, list[str]]:
    if "mcpServers" in value:
        config = value["mcpServers"]["mutalaamcp"]
        return config["command"], config["args"]
    if "mcp_servers" in value:
        config = value["mcp_servers"]["mutalaamcp"]
        return config["command"], config["args"]
    if "mcp" in value:
        config = value["mcp"]["mutalaamcp"]
        assert config["type"] == "local"
        command = config["command"]
        return command[0], command[1:]
    return value["command"], value["arguments"]


def _rendered_stdio_pair(client: str, rendered: str) -> tuple[str, list[str]]:
    if client == "codex":
        return _stdio_pair(tomllib.loads(rendered))
    if client == "opencode-desktop":
        return _stdio_pair(json.loads(rendered))
    if client == "hermes-agent":
        lines = rendered.splitlines()
        assert lines == [
            "mcpServers:",
            "  mutalaamcp:",
            f'    command: "{FIXTURE_LAUNCHER}"',
            "    args:",
            "      - serve",
        ]
        return FIXTURE_LAUNCHER, ["serve"]
    if client in {"witsy", "cherry-studio"}:
        lines = rendered.splitlines()
        assert lines == [f"Komut: {FIXTURE_LAUNCHER}", "Bağımsız değişkenler: serve"]
        return FIXTURE_LAUNCHER, ["serve"]
    return _stdio_pair(json.loads(rendered))


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)


def _is_absolute(path: str) -> bool:
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def test_supported_clients_are_exact_v1_matrix() -> None:
    assert SUPPORTED_CLIENTS == (
        "claude-desktop",
        "codex",
        "cursor",
        "google-antigravity",
        "zcode",
        "opencode-desktop",
        "witsy",
        "hermes-agent",
        "cherry-studio",
    )


@pytest.mark.parametrize("client", SUPPORTED_CLIENTS)
def test_rendered_client_configs_are_deterministic_and_secret_free(client: str) -> None:
    first = render_client_config(client, FIXTURE_LAUNCHER)
    assert first == render_client_config(client, FIXTURE_LAUNCHER)

    launcher, arguments = _rendered_stdio_pair(client, first)
    assert launcher == FIXTURE_LAUNCHER
    assert _is_absolute(launcher)
    assert arguments == ["serve"]
    lowered = first.lower()
    assert all(marker not in lowered for marker in _SECRET_MARKERS)

    launcher = r"C:\Users\example\AppData\Local\mutalaamcp\mutalaamcp.exe"
    rendered = render_client_config("claude-desktop", launcher)
    assert json.loads(rendered)["mcpServers"]["mutalaamcp"]["command"] == launcher


@pytest.mark.parametrize("launcher", ("mutalaamcp", "./mutalaamcp", "", "~/mutalaamcp"))
def test_render_client_config_rejects_nonabsolute_launcher(launcher: str) -> None:
    with pytest.raises(ValueError, match="mutlak bir yol"):
        render_client_config("claude-desktop", launcher)


def test_render_client_config_rejects_unknown_client() -> None:
    with pytest.raises(ValueError, match="Desteklenmeyen MCP istemcisi"):
        render_client_config("unknown-client", FIXTURE_LAUNCHER)


def test_client_fixtures_parse_and_match_renderer_stdio_contract() -> None:
    fixtures = {
        path.name: path for path in CLIENT_FIXTURES_DIR.iterdir() if path.is_file()
    }
    assert fixtures.keys() == EXPECTED_FIXTURES

    fixture_clients = {
        "claude-desktop-v1.json": "claude-desktop",
        "codex-v1.toml": "codex",
        "cursor-v1.json": "cursor",
        "google-antigravity-v1.json": "google-antigravity",
        "zcode-v1.json": "zcode",
        "opencode-desktop-v1.json": "opencode-desktop",
        "witsy-v1.json": "witsy",
        "hermes-agent-v1.json": "hermes-agent",
        "cherry-studio-v1.json": "cherry-studio",
    }
    for name, client in fixture_clients.items():
        fixture = _fixture_data(fixtures[name])
        fixture_launcher, fixture_arguments = _stdio_pair(fixture)
        rendered_launcher, rendered_arguments = _rendered_stdio_pair(
            client, render_client_config(client, FIXTURE_LAUNCHER)
        )
        assert (fixture_launcher, fixture_arguments) == (
            rendered_launcher,
            rendered_arguments,
        )
        assert _is_absolute(fixture_launcher)
        assert fixture_arguments == ["serve"]
        assert all(
            marker not in string.lower()
            for string in _strings(fixture)
            for marker in _SECRET_MARKERS
        )


def test_release_manifest_has_grounded_versions_and_explicit_unknowns() -> None:
    manifest = json.loads((RELEASE_DIR / "release-manifest-v1.json").read_text("utf-8"))
    assert manifest["manifest_version"] == 1
    assert manifest["package"] == {"name": "mutalaamcp", "version": __version__}
    assert manifest["cache_schema_version"] == SCHEMA_VERSION
    assert manifest["ocr_model_manifest_version"] == 1
    assert manifest["minimum_runtime"] == {
        "python": {"minimum_version": "3.12", "verification": "repository-verified"},
        "uv": {"minimum_version": None, "verification": "unverified"},
    }
    assert {
        (target["os"], target["architecture"]) for target in manifest["targets"]
    } == {
        ("macos", "arm64"),
        ("windows", "x86_64"),
    }
    assert all(
        target["os_version"]["verification"] == "unverified"
        for target in manifest["targets"]
    )
    assert tuple(manifest["clients"]) == SUPPORTED_CLIENTS
    assert all(
        client == {"verification": "unverified"}
        for client in manifest["clients"].values()
    )


def test_ocr_manifest_pins_official_cross_platform_model_artifacts() -> None:
    manifest = json.loads((RELEASE_DIR / "ocr-models-v1.json").read_text("utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["model_id"] == "pp-ocrv6-small"
    assert manifest["runtime"] == "paddleocr"
    assert manifest["runtime_version"] == "3.7.0"
    assert manifest["allowed_hosts"] == ["paddle-model-ecology.bj.bcebos.com"]
    assert manifest["runtime_options"] == {
        "device": "cpu",
        "text_detection_model_name": "PP-OCRv6_small_det",
        "text_detection_model_dir": "PP-OCRv6_small_det_infer",
        "text_recognition_model_name": "PP-OCRv6_small_rec",
        "text_recognition_model_dir": "PP-OCRv6_small_rec_infer",
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
    }
    artifacts = manifest["artifacts"]
    assert set(artifacts) == {"darwin-arm64", "windows-x64"}
    assert artifacts["darwin-arm64"] == artifacts["windows-x64"]
    assert artifacts["darwin-arm64"]["downloads"] == [
        {
            "name": "text-detection",
            "url": (
                "https://paddle-model-ecology.bj.bcebos.com/paddlex/"
                "official_inference_model/paddle3.0.0/"
                "PP-OCRv6_small_det_infer.tar"
            ),
            "sha256": (
                "bfb7c1e59f0faa6b540ebdca93aea3f4b1f2477805b389fbee117820d68fe9f5"
            ),
            "size_bytes": 10055680,
            "archive": "tar",
        },
        {
            "name": "text-recognition",
            "url": (
                "https://paddle-model-ecology.bj.bcebos.com/paddlex/"
                "official_inference_model/paddle3.0.0/"
                "PP-OCRv6_small_rec_infer.tar"
            ),
            "sha256": (
                "da460f968ce9f88325ac3a34fa302077d6e9b0dcefb16ba3137cd7796f879d06"
            ),
            "size_bytes": 21442560,
            "archive": "tar",
        },
    ]
    assert artifacts["darwin-arm64"]["files"] == [
        "PP-OCRv6_small_det_infer/inference.json",
        "PP-OCRv6_small_det_infer/inference.pdiparams",
        "PP-OCRv6_small_det_infer/inference.yml",
        "PP-OCRv6_small_rec_infer/inference.json",
        "PP-OCRv6_small_rec_infer/inference.pdiparams",
        "PP-OCRv6_small_rec_infer/inference.yml",
    ]


def test_ocr_runtime_lock_contains_supported_macos_and_windows_wheels() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {package["name"]: package for package in lock["package"]}
    assert packages["paddleocr"]["version"] == "3.7.0"
    assert {
        PurePosixPath(wheel["url"]).name for wheel in packages["paddleocr"]["wheels"]
    } == {"paddleocr-3.7.0-py3-none-any.whl"}
    assert packages["paddlepaddle"]["version"] == "3.3.0"
    paddle_wheels = {
        PurePosixPath(wheel["url"]).name for wheel in packages["paddlepaddle"]["wheels"]
    }
    assert {
        "paddlepaddle-3.3.0-cp312-cp312-macosx_11_0_arm64.whl",
        "paddlepaddle-3.3.0-cp313-cp313-macosx_11_0_arm64.whl",
        "paddlepaddle-3.3.0-cp312-cp312-win_amd64.whl",
        "paddlepaddle-3.3.0-cp313-cp313-win_amd64.whl",
    } <= paddle_wheels


def test_release_resources_are_declared_for_wheel_packaging() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = config["tool"]["hatch"]["build"]["targets"]["wheel"][
        "force-include"
    ]
    assert force_include["release"] == "mutalaamcp/release"
    build_config = config["tool"]["hatch"]["build"]
    assert {
        "/.hypothesis/**",
        "/.omp/**",
        "/.cache/**",
        "/.pytest_cache/**",
        "/.mypy_cache/**",
        "/.ruff_cache/**",
        "/dist/**",
        "/build/**",
        "/**/__pycache__/**",
    } <= set(build_config["exclude"])
    assert {
        "/contracts",
        "/release",
        "/LICENSE",
        "/README.md",
        "/README.en.md",
        "/SECURITY.md",
        "/SECURITY.en.md",
        "/SUPPORT.md",
        "/SUPPORT.en.md",
        "/TRADEMARK_POLICY",
        "/TRADEMARK_POLICY.en.md",
        "/THIRD_PARTY_NOTICES",
        "/THIRD_PARTY_NOTICES.en.md",
    } <= set(build_config["targets"]["sdist"]["include"])


def test_built_artifacts_exclude_developer_state_and_retain_release_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hatchling_build = pytest.importorskip("hatchling.build")
    monkeypatch.chdir(ROOT)

    sdist_path = tmp_path / hatchling_build.build_sdist(str(tmp_path))
    wheel_path = tmp_path / hatchling_build.build_wheel(str(tmp_path))
    with tarfile.open(sdist_path) as archive:
        source_members = {
            PurePosixPath(*PurePosixPath(member.name).parts[1:]).as_posix()
            for member in archive.getmembers()
            if member.isfile()
        }
    with zipfile.ZipFile(wheel_path) as archive:
        wheel_members = {
            member.filename for member in archive.infolist() if not member.is_dir()
        }

    assert {
        "LICENSE",
        "README.md",
        "README.en.md",
        "SECURITY.md",
        "SECURITY.en.md",
        "SUPPORT.md",
        "SUPPORT.en.md",
        "TRADEMARK_POLICY",
        "TRADEMARK_POLICY.en.md",
        "THIRD_PARTY_NOTICES",
        "THIRD_PARTY_NOTICES.en.md",
        "contracts/tool-surface-v1.json",
        "release/release-manifest-v1.json",
        "release/clients/claude-desktop-v1.json",
    } <= source_members
    assert {
        "mutalaamcp/contracts/tool-surface-v1.json",
        "mutalaamcp/release/release-manifest-v1.json",
        "mutalaamcp/release/clients/claude-desktop-v1.json",
        f"mutalaamcp-{__version__}.dist-info/licenses/LICENSE",
    } <= wheel_members

    for member in source_members | wheel_members:
        path = PurePosixPath(member)
        assert not _DEVELOPER_ARTIFACT_PARTS.intersection(path.parts), member
        assert path.name not in _DEVELOPER_ARTIFACT_NAMES, member
        assert not path.name.endswith((".pyc", ".pyo")), member
        assert not any(part.endswith(".egg-info") for part in path.parts), member
