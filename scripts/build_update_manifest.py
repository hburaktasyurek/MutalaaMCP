"""Build the hash-bound update manifest for a release; never installs anything.

Usage:
    uv run scripts/build_update_manifest.py --wheel dist/mutalaamcp-X.Y.Z-py3-none-any.whl \
        --output dist/update-manifest-v1.json --tag vX.Y.Z

The manifest is the update channel: clients fetch it from the release assets and
require every listed artifact by SHA-256. Dependency pins come from ``uv.lock``
via ``uv export`` so the installed set is exactly the locked set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_INDEX_URL = "https://pypi.org/simple"
_RELEASE_BASE = "https://github.com/hburaktasyurek/MutalaaMCP/releases/download"
_HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})")
_REQUIREMENT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)(?:\s*;\s*(.+))?")


def _export_dependencies() -> list[dict[str, object]]:
    result = subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"uv export başarısız: {result.stderr.strip()}")
    logical_lines: list[str] = []
    buffer = ""
    for raw in result.stdout.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        buffer += line
        logical_lines.append(buffer)
        buffer = ""
    if buffer.strip():
        raise SystemExit("uv export çıktısı tamamlanmamış bir devam satırı içeriyor.")
    dependencies: list[dict[str, object]] = []
    for line in logical_lines:
        hashes = _HASH.findall(line)
        if not hashes:
            raise SystemExit(f"uv export satırında SHA-256 özeti yok: {line!r}")
        requirement = _HASH.sub("", line).strip()
        match = _REQUIREMENT.fullmatch(requirement)
        if match is None:
            raise SystemExit(f"uv export satırı çözümlenemedi: {line!r}")
        name, version, marker = match.groups()
        if name.lower() == "mutalaamcp":
            raise SystemExit(
                "uv export proje paketini de üretti; manifest paketi wheel'den alır."
            )
        entry: dict[str, object] = {
            "name": name,
            "version": version,
            "hashes": hashes,
        }
        if marker:
            entry["marker"] = marker.strip()
        dependencies.append(entry)
    if not dependencies:
        raise SystemExit("uv export hiç bağımlılık üretmedi.")
    return dependencies


def build(wheel: Path, output: Path, tag: str) -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", version):
        raise SystemExit("Desteklenmeyen sürüm biçimi")
    if tag != f"v{version}" or wheel.name != (
        f"mutalaamcp-{version}-py3-none-any.whl"
    ):
        raise SystemExit("Etiket, paket sürümü ve wheel adı eşleşmelidir")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest = {
        "manifest_version": 1,
        "version": version,
        "index_url": _INDEX_URL,
        "package": {
            "name": "mutalaamcp",
            "version": version,
            "url": f"{_RELEASE_BASE}/{tag}/{wheel.name}",
            "hashes": [digest],
        },
        "dependencies": _export_dependencies(),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    build(args.wheel, args.output, args.tag)
