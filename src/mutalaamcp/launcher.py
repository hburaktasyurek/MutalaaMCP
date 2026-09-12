"""Sürüme göre seçilen MCP sunumu için kullanıcıya ait kararlı başlatıcı oluşturma."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path


class LauncherError(RuntimeError):
    """Kararlı başlatıcı bu platformda güvenle oluşturulamaz."""


_LAUNCHER_NAME = "mutalaamcp"
_WINDOWS_SELECTOR_NAME = f"{_LAUNCHER_NAME}-selector.py"
_WINDOWS_WRAPPER_NAME = f"{_LAUNCHER_NAME}.cmd"


def ensure_stable_launcher(data_dir: Path, current_launcher: Path) -> Path:
    """Ana makine platformu için kararlı sürüm seçiciyi oluşturur.

    Oluşturulan dosyalar, hiçbir aday ortama değil, kullanıcının veri dizinine
    ait olur; böylece istemci yapılandırmasının sürümlü bir sanal ortamı
    belirtmesi gerekmez. Windows, tırnak içindeki ``.cmd`` sarmalayıcısının
    arkasında standart kitaplıktaki Python seçicisini kullanır; POSIX ise
    çalıştırılabilir betiğini korur.
    """

    launcher = _absolute_executable(current_launcher, "geçerli başlatıcı")
    root = data_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    interpreter = _absolute_executable(
        Path(sys.executable), "kararlı başlatıcı yorumlayıcısı"
    )
    if os.name == "posix":
        target = root / _LAUNCHER_NAME
        if target == launcher:
            raise LauncherError("kararlı başlatıcı geçerli başlatıcının yerini alamaz")
        _atomic_launcher_write(
            target, _launcher_source(root, interpreter), executable=True
        )
        return target
    if os.name == "nt":
        selector = root / _WINDOWS_SELECTOR_NAME
        wrapper = root / _WINDOWS_WRAPPER_NAME
        if wrapper == launcher:
            raise LauncherError("kararlı başlatıcı geçerli başlatıcının yerini alamaz")
        _atomic_launcher_write(
            selector, _launcher_source(root, interpreter), executable=False
        )
        _atomic_launcher_write(
            wrapper,
            _windows_wrapper_source(interpreter, selector),
            executable=False,
        )
        return wrapper
    raise LauncherError("kararlı başlatıcı oluşturma bu platformda desteklenmiyor")


def _absolute_executable(value: Path, label: str) -> Path:
    try:
        path = value.expanduser().resolve(strict=True)
    except OSError as exc:
        raise LauncherError(f"{label} mevcut değil") from exc
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise LauncherError(f"{label} çalıştırılabilir bir mutlak dosya olmalıdır")
    return path


def _atomic_launcher_write(path: Path, source: str, *, executable: bool) -> None:
    descriptor, raw = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(source)
            output.flush()
            os.fsync(output.fileno())
        if os.name == "posix":
            mode = stat.S_IRUSR | stat.S_IWUSR
            if executable:
                mode |= stat.S_IXUSR
            temporary.chmod(mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _windows_wrapper_source(interpreter: Path, selector: Path) -> str:
    """İki sabit yolu tırnak içine alır; yalnızca iletilen istemci bağımsız değişkenleri dinamiktir."""

    return "\n".join(
        (
            "@echo off",
            "setlocal DisableDelayedExpansion",
            f"{_cmd_quote(interpreter)} {_cmd_quote(selector)} %*",
            "exit /b %ERRORLEVEL%",
            "",
        )
    )


def _cmd_quote(path: Path) -> str:
    raw = str(path)
    if "\x00" in raw or '"' in raw:
        raise LauncherError(
            "Windows başlatıcı yolları desteklenmeyen karakterler içeriyor"
        )
    # Yüzde işaretleri çift tırnak içinde bile toplu iş dosyasına uygun biçimde kaçırılmalıdır.
    return f'"{raw.replace("%", "%%")}"'


def _launcher_source(data_dir: Path, interpreter: Path) -> str:
    return f"""#!{interpreter}
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!]*$")
_RUNTIME_READY_FILE_ENV = "MUTALAAMCP_RUNTIME_READY_FILE"
_RUNTIME_READY_TIMEOUT_SECONDS = 5.0
_READY_MARKER = b"ready\\n"

_DATA_DIR = Path({str(data_dir)!r})
_STATE_PATH = _DATA_DIR / "active-version.json"
_VERSION_ROOT = _DATA_DIR / "versions"


def _fail(message):
    print(f"mutalaamcp başlatıcısı: {{message}}", file=sys.stderr)
    return 1


def _load_state():
    try:
        value = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("active-version.json kullanılamıyor veya hatalı biçimlendirilmiş") from exc
    if not isinstance(value, dict):
        raise RuntimeError("active-version.json bir nesne içermelidir")
    return value


def _launcher_from(record, label):
    if not isinstance(record, dict):
        raise RuntimeError(f"{{label}} eksik")
    version = record.get("version")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise RuntimeError(f"{{label}} sürümü geçersiz")
    raw = record.get("launcher")
    if not isinstance(raw, str):
        raise RuntimeError(f"{{label}} başlatıcısı eksik")
    try:
        launcher = Path(raw).expanduser().resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{{label}} başlatıcısı kullanılamıyor") from exc
    if not launcher.is_absolute() or not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise RuntimeError(f"{{label}} başlatıcısı çalıştırılabilir değil")
    return launcher


def _atomic_state_write(value):
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(
        prefix=f".{{_STATE_PATH.name}}.", suffix=".tmp", dir=_STATE_PATH.parent
    )
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.write("\\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, _STATE_PATH)
        _fsync_directory(_STATE_PATH.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path):
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_private_readiness_file():
    descriptor, raw = tempfile.mkstemp(
        prefix=".mutalaamcp-runtime-ready-", suffix=".marker", dir=_DATA_DIR
    )
    path = Path(raw)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        os.close(descriptor)
    return path


def _readiness_is_signaled(marker):
    try:
        status = marker.lstat()
        if not stat.S_ISREG(status.st_mode):
            return False
        if os.name == "posix" and stat.S_IMODE(status.st_mode) != 0o600:
            return False
        return marker.read_bytes() == _READY_MARKER
    except OSError:
        return False


def _wait_for_runtime_ready(process, marker):
    deadline = time.monotonic() + _RUNTIME_READY_TIMEOUT_SECONDS
    while True:
        if _readiness_is_signaled(marker):
            return True
        if process.poll() is not None:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _terminate_candidate(process):
    if process.poll() is not None:
        return process.returncode
    process.terminate()
    try:
        return process.wait(timeout=_RUNTIME_READY_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait()


def _remove_readiness_marker(marker):
    try:
        marker.unlink(missing_ok=True)
    except OSError as exc:
        print(
            f"mutalaamcp başlatıcısı: çalışma zamanı hazır olma işaretleyicisi kaldırılamadı: {{exc}}",
            file=sys.stderr,
        )


def _candidate_root(launcher):
    try:
        root = launcher.parent.parent.resolve(strict=True)
        version_root = _VERSION_ROOT.resolve(strict=True)
    except OSError:
        return None
    if root.parent == version_root and root.name.startswith(".candidate-"):
        return root
    return None


def _discard_candidate(launcher):
    root = _candidate_root(launcher)
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)


def _rollback_plan(state):
    rollback = state.get("rollback")
    if not isinstance(rollback, dict):
        raise RuntimeError("geri alma durumu eksik")
    previous_launcher = _launcher_from(rollback, "geri alma")
    raw_cache_path = rollback.get("cache_path")
    if not isinstance(raw_cache_path, str):
        raise RuntimeError("geri alma önbellek yolu eksik")
    try:
        cache_path = Path(raw_cache_path).expanduser().resolve()
    except OSError as exc:
        raise RuntimeError("geri alma önbellek yolu kullanılamıyor") from exc
    if not cache_path.is_absolute():
        raise RuntimeError("geri alma önbellek yolu geçersiz")
    raw_backup = rollback.get("cache_backup")
    if raw_backup is None:
        backup = None
    elif isinstance(raw_backup, str):
        try:
            backup = Path(raw_backup).expanduser().resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("geri alma önbellek yedeği kullanılamıyor") from exc
        if not backup.is_absolute() or not backup.is_file():
            raise RuntimeError("geri alma önbellek yedeği kullanılamıyor")
    else:
        raise RuntimeError("geri alma önbellek yedeği geçersiz")
    previous = {{"version": rollback["version"], "launcher": str(previous_launcher)}}
    return previous, cache_path, backup


def _restore_cache(cache_path, backup):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if backup is None:
        cache_path.unlink(missing_ok=True)
    else:
        descriptor, raw = tempfile.mkstemp(
            prefix=f".{{cache_path.name}}.restore-", suffix=".tmp", dir=cache_path.parent
        )
        temporary = Path(raw)
        try:
            with os.fdopen(descriptor, "wb") as output, backup.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, cache_path)
        finally:
            temporary.unlink(missing_ok=True)
    Path(f"{{cache_path}}-wal").unlink(missing_ok=True)
    Path(f"{{cache_path}}-shm").unlink(missing_ok=True)
    _fsync_directory(cache_path.parent)


def _delete_backup(backup):
    if backup is None:
        return
    try:
        backup.unlink(missing_ok=True)
    except OSError as exc:
        print(f"mutalaamcp başlatıcısı: önbellek yedeği kaldırılamadı: {{exc}}", file=sys.stderr)


def _rollback_failed_candidate(launcher, previous, cache_path, backup):
    _restore_cache(cache_path, backup)
    _atomic_state_write(previous)
    _discard_candidate(launcher)
    _delete_backup(backup)
    print(
        "mutalaamcp başlatıcısı: aday serve başlatması başarısız oldu; önceki başlatıcı ve önbellek geri yüklendi",
        file=sys.stderr,
    )


def _commit_healthy_start(state, backup):
    committed = dict(state)
    committed.pop("rollback", None)
    _atomic_state_write(committed)
    _delete_backup(backup)


def _run_candidate_with_rollback(launcher, state, previous, cache_path, backup, argv):
    try:
        marker = _create_private_readiness_file()
    except OSError as exc:
        print(
            f"mutalaamcp başlatıcısı: çalışma zamanı hazır olma işaretleyicisi oluşturulamadı: {{exc}}",
            file=sys.stderr,
        )
        _rollback_failed_candidate(launcher, previous, cache_path, backup)
        return 1
    environment = os.environ.copy()
    environment[_RUNTIME_READY_FILE_ENV] = str(marker)
    try:
        try:
            process = subprocess.Popen([str(launcher), *argv], env=environment)
        except OSError as exc:
            print(
                f"mutalaamcp başlatıcısı: aday başlatılamadı: {{exc}}",
                file=sys.stderr,
            )
            _rollback_failed_candidate(launcher, previous, cache_path, backup)
            return 1
        if _wait_for_runtime_ready(process, marker):
            _remove_readiness_marker(marker)
            _commit_healthy_start(state, backup)
            return process.wait()
        returncode = _terminate_candidate(process)
        _rollback_failed_candidate(launcher, previous, cache_path, backup)
        if returncode == 0:
            print(
                "mutalaamcp başlatıcısı: aday, çalışma zamanının hazır olduğunu bildirmeden sonlandı",
                file=sys.stderr,
            )
            return 1
        print(
            "mutalaamcp başlatıcısı: aday, çalışma zamanının hazır olduğunu bildirmedi",
            file=sys.stderr,
        )
        return returncode
    finally:
        _remove_readiness_marker(marker)


def main():
    argv = sys.argv[1:]
    if argv not in (["serve"], ["serve-http"]):
        return _fail("yalnızca serve veya serve-http komutları desteklenir")
    try:
        state = _load_state()
        launcher = _launcher_from(state, "etkin sürüm")
        rollback = (
            _rollback_plan(state) if "rollback" in state else None
        )
    except RuntimeError as exc:
        return _fail(str(exc))
    try:
        if rollback is None:
            environment = os.environ.copy()
            environment.pop(_RUNTIME_READY_FILE_ENV, None)
            return subprocess.run(
                [str(launcher), *argv], check=False, env=environment
            ).returncode
        previous, cache_path, backup = rollback
        return _run_candidate_with_rollback(
            launcher, state, previous, cache_path, backup, argv
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _fail(f"başlatıcı durumu kalıcı hâle getirilemedi: {{exc}}")


if __name__ == "__main__":
    raise SystemExit(main())
"""
