"""Release binding and installer failure/success behavior without real installation."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from mutalaamcp import __version__

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "build_installers", ROOT / "scripts/build_installers.py"
)
assert spec and spec.loader
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.fixture
def generated(tmp_path: Path) -> tuple[Path, Path]:
    wheel = tmp_path / f"mutalaamcp-{__version__}-py3-none-any.whl"
    wheel.write_bytes(b"test-wheel")
    out = tmp_path / "release"
    builder.build(wheel, out, f"v{__version__}")
    return wheel, out


def test_release_is_bound_to_wheel_and_preserves_mac_permissions(generated):
    wheel, out = generated
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with zipfile.ZipFile(out / "Mutalaa-macOS.zip") as archive:
        info = archive.getinfo("Mutalaa-Kur.command")
        assert (info.external_attr >> 16) & 0o111 == 0o111
        command = archive.read(info).decode()
    windows = (out / "Mutalaa-Kur.cmd").read_text()
    for text in (command, windows):
        assert digest in text
        assert f"/releases/download/v{__version__}/{wheel.name}" in text
        assert "main.zip" not in text
        assert "@VERSION@" not in text
    assert windows.count("# BEGIN_POWERSHELL") == 1
    with pytest.raises(ValueError):
        builder.build(wheel, out, "v9.9.9")


@pytest.mark.skipif(os.name == "nt", reason="macOS shell script")
@pytest.mark.parametrize(
    "fail_at", ["none", "download", "checksum", "install", "setup"]
)
def test_macos_installer_stops_on_failure(generated, tmp_path, fail_at):
    wheel, out = generated
    with zipfile.ZipFile(out / "Mutalaa-macOS.zip") as archive:
        archive.extractall(out)
    fake = tmp_path / "fake bin"
    fake.mkdir()
    log = tmp_path / "calls"
    tool = tmp_path / "tool path" / "mutalaamcp/bin"
    tool.mkdir(parents=True)
    env = {**os.environ, "PATH": f"{fake}:/usr/bin:/bin", "TEST_LOG": str(log)}

    def executable(path, body):
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    executable(fake / "uname", "echo Darwin\n")
    executable(
        fake / "curl",
        'echo download >> "$TEST_LOG"\n'
        + (
            "exit 22\n"
            if fail_at == "download"
            else 'while [ "$1" != "-o" ]; do shift; done\nshift\nprintf test-wheel > "$1"\n'
        ),
    )
    executable(
        fake / "shasum",
        f'echo "{hashlib.sha256(wheel.read_bytes()).hexdigest() if fail_at != "checksum" else "bad"}  file"\n',
    )
    executable(
        fake / "uv",
        f'if [ "$2" = dir ]; then echo "{tool.parent.parent}"; exit; fi\necho install >> "$TEST_LOG"\nexit {1 if fail_at == "install" else 0}\n',
    )
    executable(
        tool / "mutalaamcp",
        'if [ "$1" = service ]; then echo status=stopped; exit; fi\necho setup >> "$TEST_LOG"\n'
        + f"exit {1 if fail_at == 'setup' else 0}\n",
    )
    executable(fake / "pbcopy", 'cat >/dev/null\necho clipboard >> "$TEST_LOG"\n')
    run = subprocess.run(
        ["/bin/bash", str(out / "Mutalaa-Kur.command")],
        input="\n\n",
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert (run.returncode == 0) == (fail_at == "none"), run.stdout + run.stderr
    calls = log.read_text()
    if fail_at in {"download", "checksum"}:
        assert "install" not in calls
    if fail_at != "none":
        assert "clipboard" not in calls
        assert "Hazir!" not in run.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell parser")
def test_windows_payload_parses(generated):
    _, out = generated
    executable = shutil.which("powershell.exe")
    assert executable
    script = "$s=[IO.File]::ReadAllText($env:TEST_INSTALLER);$body=($s -split '# BEGIN_POWERSHELL',2)[1];$tokens=$null;$errors=$null;[System.Management.Automation.Language.Parser]::ParseInput($body,[ref]$tokens,[ref]$errors)|Out-Null;if($errors.Count){$errors|Out-String|Write-Error;exit 1}"
    subprocess.run(
        [executable, "-NoProfile", "-Command", script],
        env={**os.environ, "TEST_INSTALLER": str(out / "Mutalaa-Kur.cmd")},
        check=True,
    )
