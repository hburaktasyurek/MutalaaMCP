"""Build version-bound release installers; never install on the build machine."""

from __future__ import annotations

import argparse
import hashlib
import re
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(wheel: Path, output: Path, tag: str) -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", version):
        raise ValueError("Unsupported release version")
    if tag != f"v{version}" or wheel.name != f"mutalaamcp-{version}-py3-none-any.whl":
        raise ValueError("Tag, package version and wheel must match")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    values = {
        "VERSION": version,
        "TAG": tag,
        "WHEEL": wheel.name,
        "SHA256": digest,
        "URL": f"https://github.com/hburaktasyurek/MutalaaMCP/releases/download/{tag}/{wheel.name}",
    }

    def render(name: str) -> str:
        text = (ROOT / "installers" / name).read_text()
        for key, value in values.items():
            text = text.replace(f"@{key}@", value)
        return text

    output.mkdir(parents=True, exist_ok=True)
    command = render("macos.command.in")
    # Finder needs the executable bit. A ZIP preserves it; a raw browser download doesn't.
    with zipfile.ZipFile(
        output / "Mutalaa-macOS.zip", "w", zipfile.ZIP_DEFLATED
    ) as archive:
        info = zipfile.ZipInfo("Mutalaa-Kur.command")
        info.create_system = 3
        info.external_attr = 0o100755 << 16
        archive.writestr(info, command.encode())
    wrapper = """@echo off
setlocal
set "MUTALAA_INSTALLER_FILE=%~f0"
powershell.exe -NoProfile -Command "$s=[IO.File]::ReadAllText($env:MUTALAA_INSTALLER_FILE); & ([scriptblock]::Create(($s -split '# BEGIN_POWERSHELL',2)[1]))"
exit /b %errorlevel%
# BEGIN_POWERSHELL
"""
    # Split marker in the wrapper command so the first occurrence is the payload boundary.
    wrapper = wrapper.replace(
        "-split '# BEGIN_POWERSHELL'", "-split ('# BEGIN_'+'POWERSHELL')"
    )
    (output / "Mutalaa-Kur.cmd").write_bytes(
        (wrapper + render("windows.ps1.in")).replace("\n", "\r\n").encode("utf-8")
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    build(args.wheel, args.output, args.tag)
