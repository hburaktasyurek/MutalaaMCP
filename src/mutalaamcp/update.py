"""Crash-safe candidate-version installation and activation.

The active launcher record is the only mutable selector. Candidate environments
are installed and exercised independently before that record changes. A durable
cache snapshot remains in the rollback record until the stable launcher proves
the first real candidate start healthy.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from mutalaamcp.domain.errors import ErrorCode
from mutalaamcp.launcher import LauncherError, ensure_stable_launcher
from mutalaamcp.server import V1_TOOL_NAMES
from mutalaamcp.settings import Settings

_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!]*$")

_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_CANDIDATE_HEALTH_TIMEOUT_SECONDS = 30.0


class UpdateError(RuntimeError):
    """A candidate version could not be installed, verified, or activated."""


@dataclass(frozen=True, slots=True)
class ActiveVersion:
    """The atomic launcher selection persisted for the next client invocation."""

    version: str
    launcher: Path

    def as_dict(self) -> dict[str, str]:
        return {"version": self.version, "launcher": str(self.launcher)}


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """Successful candidate promotion result."""

    previous: ActiveVersion
    active: ActiveVersion


@dataclass(frozen=True, slots=True)
class PackageIntegrity:
    """One exact distribution permitted by a verified update manifest."""

    name: str
    version: str
    hashes: tuple[str, ...]
    url: str | None = None
    marker: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedUpdateManifest:
    """Trusted, complete, hash-bound input for one candidate installation."""

    index_url: str
    package: PackageIntegrity
    dependencies: tuple[PackageIntegrity, ...]


@dataclass(frozen=True, slots=True)
class UpdateOffer:
    """A release-channel document validated into installable form."""

    version: str
    manifest: VerifiedUpdateManifest


HealthCheck = Callable[[Path, Settings, Path | None], None]

_MANIFEST_MAX_BYTES = 262_144
_UPDATE_CHECK_INTERVAL_SECONDS = 6 * 60 * 60.0
_UPDATE_FIRST_CHECK_SECONDS = 60.0
_RELEASE_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")
_PRERELEASE_ORDER = {"a": 0, "b": 1, "rc": 2}


def resolve_uv(settings: Settings) -> Path:
    """Resolve an executable *absolute* uv binary; never invoke a shell lookup."""

    configured = settings.uv_executable
    raw: object = configured if configured is not None else shutil.which("uv")
    if raw is None:
        raise UpdateError(
            "Güncelleme için mutlak bir uv çalıştırılabilir dosyası gerekli."
        )
    if not isinstance(raw, (str, Path)):
        raise UpdateError(
            "Yapılandırılmış uv çalıştırılabilir dosyası bir dosya sistemi yolu değildir."
        )
    try:
        executable = Path(raw).expanduser().resolve(strict=True)
    except OSError as exc:
        raise UpdateError(
            "Yapılandırılmış uv çalıştırılabilir dosyası bulunamadı."
        ) from exc
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise UpdateError(
            "Yapılandırılmış uv çalıştırılabilir dosyası çalıştırılabilir değildir."
        )
    return executable


def fetch_update_offer(client: httpx.Client, url: str) -> UpdateOffer:
    """Fetch and strictly validate the hash-bound update manifest over HTTPS."""

    manifest_url = _validated_https_url(url, "güncelleme bildirim adresi")
    try:
        with client.stream(
            "GET", manifest_url, follow_redirects=True
        ) as response:
            if response.status_code != 200:
                raise UpdateError(
                    "Güncelleme bildirimi beklenmeyen bir durum döndürdü "
                    f"(HTTP {response.status_code})."
                )
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > _MANIFEST_MAX_BYTES:
                    raise UpdateError(
                        "Güncelleme bildirimi izin verilen boyutu aşıyor."
                    )
    except httpx.HTTPError as exc:
        raise UpdateError("Güncelleme bildirimine erişilemiyor.") from exc
    try:
        payload = json.loads(bytes(body))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("Güncelleme bildirimi geçerli JSON değil.") from exc
    return _parse_update_offer(payload)


def _parse_update_offer(payload: object) -> UpdateOffer:
    if not isinstance(payload, Mapping):
        raise UpdateError("Güncelleme bildirimi bir JSON nesnesi olmalıdır.")
    if payload.get("manifest_version") != 1:
        raise UpdateError("Güncelleme bildirimi sürümü desteklenmiyor.")
    version = payload.get("version")
    _validate_target_version(version)
    index_url = payload.get("index_url")
    if not isinstance(index_url, str):
        raise UpdateError("Güncelleme bildirimi bir HTTPS paket dizini içermelidir.")
    package = _parse_package_integrity(payload.get("package"), "güncelleme paketi")
    raw_dependencies = payload.get("dependencies")
    if not isinstance(raw_dependencies, list):
        raise UpdateError("Güncelleme bildirimi bağımlılıkları bir liste olmalıdır.")
    dependencies = tuple(
        _parse_package_integrity(item, "güncelleme bağımlılığı")
        for item in raw_dependencies
    )
    manifest = _validate_update_manifest(
        version,
        VerifiedUpdateManifest(
            index_url=index_url, package=package, dependencies=dependencies
        ),
    )
    return UpdateOffer(version=version, manifest=manifest)


def _parse_package_integrity(value: object, label: str) -> PackageIntegrity:
    if not isinstance(value, Mapping):
        raise UpdateError(f"Güncelleme bildirimindeki {label} bozuk.")
    url = value.get("url")
    if url is not None:
        url = _validated_https_url(url, label)
    marker = value.get("marker")
    if marker is not None and not isinstance(marker, str):
        raise UpdateError(f"Güncelleme bildirimindeki {label} bozuk.")
    raw_hashes = value.get("hashes")
    if not isinstance(raw_hashes, list) or any(
        not isinstance(item, str) for item in raw_hashes
    ):
        raise UpdateError(f"Güncelleme bildirimindeki {label} bozuk.")
    name = value.get("name")
    package_version = value.get("version")
    if not isinstance(name, str) or not isinstance(package_version, str):
        raise UpdateError(f"Güncelleme bildirimindeki {label} bozuk.")
    return PackageIntegrity(
        name=name,
        version=package_version,
        hashes=tuple(raw_hashes),
        url=url,
        marker=marker,
    )


def _validated_https_url(value: object, label: str) -> str:
    """Require an HTTPS URL without credentials, query, or fragment."""

    if not isinstance(value, str) or not value or any(
        character.isspace() for character in value
    ):
        raise UpdateError(f"{label} güvenilir bir HTTPS adresi gerektirir.")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise UpdateError(f"{label} geçersiz.") from exc
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise UpdateError(f"{label} güvenilir bir HTTPS adresi gerektirir.")
    if port is not None and not 1 <= port <= 65535:
        raise UpdateError(f"{label} geçersiz.")
    return value


def _version_key(version: str) -> tuple[int, int, int, int, int] | None:
    match = _RELEASE_VERSION.fullmatch(version)
    if match is None:
        return None
    major, minor, patch, tag, number = match.groups()
    order = 3 if tag is None else _PRERELEASE_ORDER[tag]
    return (int(major), int(minor), int(patch), order, int(number or 0))


def is_newer_version(candidate: str, current: str) -> bool:
    """True when the channel version is strictly newer than the running one."""

    candidate_key = _version_key(candidate)
    current_key = _version_key(current)
    return (
        candidate_key is not None
        and current_key is not None
        and candidate_key > current_key
    )


async def auto_update_loop(
    *,
    is_managed: Callable[[], bool],
    check_and_apply: Callable[[], bool],
    on_updated: Callable[[], None],
    interval_seconds: float = _UPDATE_CHECK_INTERVAL_SECONDS,
    first_delay_seconds: float = _UPDATE_FIRST_CHECK_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Poll the release channel and apply a verified update while managed."""

    await sleep(first_delay_seconds)
    while True:
        try:
            if is_managed() and await asyncio.to_thread(check_and_apply):
                on_updated()
                return
        except Exception:  # noqa: BLE001 -- update checks must never break serving
            pass
        await sleep(interval_seconds)


def read_active_version(
    path: str | Path,
    *,
    fallback_launcher: str | Path | None = None,
    fallback_version: str | None = None,
) -> ActiveVersion:
    """Read a validated active-state record, or construct a non-persisted fallback."""

    state_path = Path(path)
    raw = _read_json_object(state_path)
    if raw is None:
        if fallback_launcher is None:
            raise UpdateError("Etkin bir MutalaaMCP sürümü yapılandırılmamış.")
        launcher = _absolute_executable(fallback_launcher, "geçerli başlatıcı")
        return ActiveVersion(
            version=fallback_version or _installed_version(), launcher=launcher
        )
    version = raw.get("version")
    raw_launcher = raw.get("launcher")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise UpdateError("Etkin sürüm durumu geçersiz bir sürüm içeriyor.")
    if not isinstance(raw_launcher, str):
        raise UpdateError("Etkin sürüm durumu geçersiz bir başlatıcı içeriyor.")
    return ActiveVersion(
        version=version, launcher=_absolute_executable(raw_launcher, "etkin başlatıcı")
    )


def write_active_version(
    path: str | Path,
    state: ActiveVersion,
    *,
    rollback_to: ActiveVersion | None = None,
    rollback_cache_path: Path | None = None,
    rollback_cache_backup: Path | None = None,
) -> None:
    """Atomically select a candidate and retain its rollback cache snapshot."""

    if rollback_to is None and (
        rollback_cache_path is not None or rollback_cache_backup is not None
    ):
        raise UpdateError(
            "Geri alma önbellek durumu bir geri alma başlatıcısı gerektirir."
        )
    value: dict[str, object] = dict(state.as_dict())
    if rollback_to is not None:
        rollback: dict[str, object] = dict(rollback_to.as_dict())
        if rollback_cache_path is not None:
            try:
                cache_path = rollback_cache_path.expanduser().resolve()
            except OSError as exc:
                raise UpdateError("Geri alma önbelleği yoluna erişilemiyor.") from exc
            if not cache_path.is_absolute():
                raise UpdateError("Geri alma önbelleği yolu mutlak olmalıdır.")
            rollback["cache_path"] = str(cache_path)
            if rollback_cache_backup is None:
                rollback["cache_backup"] = None
            else:
                backup = _absolute_regular_file(
                    rollback_cache_backup, "geri alma önbellek yedeği"
                )
                rollback["cache_backup"] = str(backup)
        elif rollback_cache_backup is not None:
            raise UpdateError("Geri alma önbellek yedeği bir önbellek yolu gerektirir.")
        value["rollback"] = rollback
    _atomic_json_write(Path(path), value)


def install_candidate(
    uv: Path,
    version: str,
    candidate_root: Path,
    *,
    manifest: VerifiedUpdateManifest | None = None,
) -> ActiveVersion:
    """Create an isolated venv from an exact, complete, hash-bound manifest."""

    verified_manifest = _validate_update_manifest(version, manifest)
    if not uv.is_absolute():
        raise UpdateError("uv çalıştırılabilir dosyasının yolu mutlak olmalıdır.")
    candidate_root.parent.mkdir(parents=True, exist_ok=True)
    _run_uv(uv, ["venv", "--python", sys.executable, str(candidate_root)])
    python = _candidate_python(candidate_root)
    requirements = _write_hashed_requirements(candidate_root, verified_manifest)
    try:
        _run_uv(
            uv,
            [
                "pip",
                "install",
                "--python",
                str(python),
                "--require-hashes",
                "--no-deps",
                "--only-binary",
                ":all:",
                "--no-cache",
                "--index-url",
                verified_manifest.index_url,
                "-r",
                str(requirements),
            ],
            env=_isolated_uv_environment(),
        )
    finally:
        requirements.unlink(missing_ok=True)
    launcher = _candidate_launcher(candidate_root)
    return ActiveVersion(version=version, launcher=launcher)


def _validate_update_manifest(
    version: str, manifest: VerifiedUpdateManifest | None
) -> VerifiedUpdateManifest:
    """Reject every source that cannot bind the complete installation set."""

    _validate_target_version(version)
    if not isinstance(manifest, VerifiedUpdateManifest):
        raise UpdateError("Doğrulanmış bir güncelleme bütünlük bildirimi gerekli.")
    _validate_https_index(manifest.index_url)
    _validate_package_integrity(manifest.package, "güncelleme paketi")
    if (
        _normalized_package_name(manifest.package.name) != "mutalaamcp"
        or manifest.package.version != version
    ):
        raise UpdateError("Bütünlük bildirimi istenen mutalaamcp sürümünü bağlamıyor.")
    if not isinstance(manifest.dependencies, tuple):
        raise UpdateError(
            "Bütünlük bildirimi bağımlılıkları eksiksiz bir demet olmalıdır."
        )
    names = {_normalized_package_name(manifest.package.name)}
    for dependency in manifest.dependencies:
        _validate_package_integrity(dependency, "güncelleme bağımlılığı")
        normalized = _normalized_package_name(dependency.name)
        if normalized in names:
            raise UpdateError("Bütünlük bildirimi yinelenen paket bağları içeriyor.")
        names.add(normalized)
    return manifest


def _validate_https_index(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise UpdateError("Bütünlük bildirimi güvenilir bir HTTPS dizini gerektirir.")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise UpdateError("Bütünlük bildirimi dizini geçersiz.") from exc
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.hostname is None
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in value)
    ):
        raise UpdateError("Bütünlük bildirimi güvenilir bir HTTPS dizini gerektirir.")
    if port is not None and not 1 <= port <= 65535:
        raise UpdateError("Bütünlük bildirimi dizini geçersiz.")


def _validate_package_integrity(value: object, label: str) -> None:
    if not isinstance(value, PackageIntegrity):
        raise UpdateError(f"Bütünlük bildirimindeki {label} bozuk.")
    if not isinstance(value.name, str) or not _PACKAGE_NAME.fullmatch(value.name):
        raise UpdateError(f"Bütünlük bildirimindeki {label} geçersiz bir ada sahip.")
    if not isinstance(value.version, str) or not _VERSION.fullmatch(value.version):
        raise UpdateError(f"Bütünlük bildirimindeki {label} geçersiz bir sürüme sahip.")
    if not isinstance(value.hashes, tuple) or not value.hashes:
        raise UpdateError(
            f"Bütünlük bildirimindeki {label} SHA-256 özetleri gerektirir."
        )
    for value_hash in value.hashes:
        if not isinstance(value_hash, str) or not _SHA256.fullmatch(
            _hash_digest(value_hash)
        ):
            raise UpdateError(
                f"Bütünlük bildirimindeki {label} geçersiz bir SHA-256 özetine sahip."
            )
    if value.url is not None:
        _validated_https_url(value.url, label)
    if value.marker is not None and (
        not value.marker.strip()
        or len(value.marker) > 300
        or any(character in value.marker for character in "\r\n")
    ):
        raise UpdateError(f"Bütünlük bildirimindeki {label} geçersiz bir işaretçi içeriyor.")


def _normalized_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _hash_digest(value: str) -> str:
    return value.removeprefix("sha256:")


def _write_hashed_requirements(
    candidate_root: Path, manifest: VerifiedUpdateManifest
) -> Path:
    """Write only manifest-bound requirements for uv's hash-checking mode."""

    descriptor, raw = tempfile.mkstemp(
        prefix=".mutalaamcp-requirements-", suffix=".txt", dir=candidate_root
    )
    requirements = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            for requirement in (manifest.package, *manifest.dependencies):
                if requirement.url is not None:
                    specifier = f"{requirement.name} @ {requirement.url}"
                else:
                    specifier = f"{requirement.name}=={requirement.version}"
                if requirement.marker is not None:
                    specifier += f" ; {requirement.marker}"
                hashes = " ".join(
                    f"--hash=sha256:{_hash_digest(value_hash)}"
                    for value_hash in requirement.hashes
                )
                output.write(f"{specifier} {hashes}\n")
            output.flush()
            os.fsync(output.fileno())
        return requirements
    except Exception:
        requirements.unlink(missing_ok=True)
        raise


def _isolated_uv_environment() -> dict[str, str]:
    """Drop ambient package-source configuration before hash-checked install."""

    environment = dict(os.environ)
    for name in (
        "CURL_CA_BUNDLE",
        "PIP_CERT",
        "PIP_CONFIG_FILE",
        "PIP_EXTRA_INDEX_URL",
        "PIP_FIND_LINKS",
        "PIP_INDEX_URL",
        "PIP_NO_BINARY",
        "PIP_NO_INDEX",
        "PIP_TRUSTED_HOST",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "UV_CONFIG_FILE",
        "UV_DEFAULT_INDEX",
        "UV_EXTRA_INDEX_URL",
        "UV_FIND_LINKS",
        "UV_INDEX_URL",
        "UV_INSECURE_HOST",
        "UV_NO_INDEX",
    ):
        environment.pop(name, None)
    environment["UV_NO_CONFIG"] = "1"
    return environment


def candidate_mcp_health_check(
    launcher: Path, settings: Settings, cache_backup: Path | None
) -> None:
    """Exercise a candidate's actual stdio runtime on an isolated cache snapshot."""

    try:
        asyncio.run(
            asyncio.wait_for(
                _candidate_mcp_health_check(launcher, settings, cache_backup),
                timeout=_CANDIDATE_HEALTH_TIMEOUT_SECONDS,
            )
        )
    except TimeoutError as exc:
        raise UpdateError("Aday MCP sağlık denetimi zaman aşımına uğradı.") from exc


async def _candidate_mcp_health_check(
    launcher: Path, settings: Settings, cache_backup: Path | None
) -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    launcher = _absolute_executable(launcher, "aday başlatıcı")
    with tempfile.TemporaryDirectory(prefix="mutalaamcp-candidate-health-") as temp:
        root = Path(temp)
        cache_dir = root / "cache"
        data_dir = root / "data"
        _copy_candidate_cache(cache_backup, cache_dir)
        _copy_auth_state(settings.data_dir, data_dir)
        transport = StdioTransport(
            command=str(launcher),
            args=["serve"],
            env=_candidate_environment(settings, cache_dir, data_dir),
        )
        try:
            async with Client(transport) as client:
                tools = await client.list_tools()
                local_result = await client.call_tool("belge_getir", {"id": "invalid"})
        except Exception as exc:  # external candidate process boundary
            raise UpdateError(
                "Aday MCP'nin başlatma/araç-listesi/yerel-araç sağlık denetimi başarısız oldu."
            ) from exc
    names = {tool.name for tool in tools}
    if names != V1_TOOL_NAMES:
        raise UpdateError("Aday MCP'nin araç listesi V1 araç kataloğunu sunmadı.")
    if not _has_invalid_params_error(_structured_payload(local_result)):
        raise UpdateError(
            "Aday MCP'nin yapılandırılmış yerel araç sağlık denetimi yetkilendirilmedi."
        )


def _copy_candidate_cache(backup: Path | None, cache_dir: Path) -> None:
    """Seed the candidate runtime cache from the durable real-cache snapshot."""

    cache_dir.mkdir(parents=True, exist_ok=True)
    if backup is None:
        return
    destination = cache_dir / "cache.sqlite3"
    try:
        shutil.copyfile(backup, destination)
    except OSError as exc:
        raise UpdateError("Aday önbellek anlık görüntüsü hazırlanamadı.") from exc


def _copy_auth_state(source_data_dir: Path, destination_data_dir: Path) -> None:
    source = source_data_dir / "auth_state.json"
    if not source.is_file():
        return
    destination_data_dir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, destination_data_dir / source.name)
    except OSError as exc:
        raise UpdateError("Aday yetkilendirme durumu hazırlanamadı.") from exc


def _candidate_environment(
    settings: Settings, cache_dir: Path, data_dir: Path
) -> dict[str, str]:
    environment = dict(os.environ)
    for field, value in settings.model_dump(mode="json").items():
        key = f"MUTALAAMCP_{field.upper()}"
        if value is None:
            environment.pop(key, None)
        elif isinstance(value, list):
            environment[key] = ",".join(str(item) for item in value)
        elif isinstance(value, bool):
            environment[key] = "true" if value else "false"
        else:
            environment[key] = str(value)
    environment.update(
        {
            "MUTALAAMCP_CACHE_DIR": str(cache_dir),
            "MUTALAAMCP_DATA_DIR": str(data_dir),
            "MUTALAAMCP_MODEL_DIR": str(settings.model_dir),
        }
    )
    return environment


def _structured_payload(result: object) -> object:
    payload = getattr(result, "structured_content", result)
    model_dump = getattr(payload, "model_dump", None)
    return model_dump(mode="json") if callable(model_dump) else payload


def _has_invalid_params_error(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    error = payload.get("error")
    return (
        isinstance(error, Mapping)
        and error.get("code") == ErrorCode.INVALID_PARAMS.value
    )


def update_to_version(
    settings: Settings,
    version: str,
    *,
    current_launcher: str | Path,
    health_check: HealthCheck = candidate_mcp_health_check,
    post_switch_check: HealthCheck | None = None,
    uv_executable: Path | None = None,
    integrity_manifest: VerifiedUpdateManifest | None = None,
) -> UpdateResult:
    """Install and prove a candidate before atomically selecting it.

    The caller owns ``mutation_lock(settings.serve_lock_path)`` across this whole
    operation. This function deliberately does not acquire it itself so tests and
    higher-level commands can make the transaction boundary explicit.
    """

    _validate_target_version(version)
    try:
        ensure_stable_launcher(settings.data_dir, Path(current_launcher))
    except LauncherError as exc:
        raise UpdateError(f"Kararlı başlatıcı hazırlanamadı: {exc}") from exc
    previous = read_active_version(
        settings.active_version_state_path,
        fallback_launcher=current_launcher,
        fallback_version=_installed_version(),
    )
    if previous.version == version:
        return UpdateResult(previous=previous, active=previous)
    verified_manifest = _validate_update_manifest(version, integrity_manifest)
    uv = uv_executable if uv_executable is not None else resolve_uv(settings)
    uv = _absolute_executable(uv, "uv çalıştırılabilir dosyası")
    if not os.access(uv, os.X_OK):
        raise UpdateError("uv çalıştırılabilir dosyası çalıştırılabilir değildir.")

    candidate_root = settings.version_root / f".candidate-{version}-{uuid.uuid4().hex}"
    backup: Path | None = None
    prior_state = _read_file(settings.active_version_state_path)
    state_switched = False
    state_restored = True
    cache_restored = True
    candidate: ActiveVersion | None = None
    success = False
    try:
        candidate = install_candidate(
            uv, version, candidate_root, manifest=verified_manifest
        )
        backup = backup_cache(settings)
        health_check(candidate.launcher, settings, backup)
        # A state write can fail after ``os.replace`` but before directory fsync.
        # Restore the recorded bytes in either outcome.
        state_switched = True
        write_active_version(
            settings.active_version_state_path,
            candidate,
            rollback_to=previous,
            rollback_cache_path=settings.cache_db_path,
            rollback_cache_backup=backup,
        )
        (post_switch_check or health_check)(candidate.launcher, settings, backup)
        success = True
        return UpdateResult(previous=previous, active=candidate)
    except Exception as exc:
        rollback_errors: list[str] = []
        if state_switched:
            try:
                restore_cache(settings, backup)
            except Exception as restore_exc:  # noqa: BLE001 -- rollback must aggregate all failures while preserving backup evidence
                cache_restored = False
                state_restored = False
                rollback_errors.append(
                    "Önbellek geri yüklenemedi; "
                    f"yedek şu konumda korundu: {backup}: {restore_exc}"
                )
            if cache_restored:
                try:
                    _restore_file(settings.active_version_state_path, prior_state)
                except Exception as restore_exc:  # noqa: BLE001 -- rollback must aggregate all failures while preserving backup evidence
                    state_restored = False
                    rollback_errors.append(
                        f"Etkin sürüm durumu geri yüklenemedi: {restore_exc}"
                    )
        if rollback_errors:
            raise UpdateError(
                "Aday güncellemesi başarısız oldu; " + "; ".join(rollback_errors)
            ) from exc
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(
            "Aday güncellemesi başarısız oldu; önceki sürüm geri yüklendi."
        ) from exc
    finally:
        if not success:
            shutil.rmtree(candidate_root, ignore_errors=True)
        if (
            backup is not None
            and not success
            and ((not state_switched) or (state_restored and cache_restored))
        ):
            backup.unlink(missing_ok=True)


def backup_cache(settings: Settings) -> Path | None:
    """Create a consistent, owner-only SQLite snapshot for rollback."""

    source = settings.cache_db_path
    if not source.is_file():
        return None
    destination = source.with_name(f".{source.name}.update-{uuid.uuid4().hex}.bak")
    try:
        if os.name == "posix":
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IRUSR | stat.S_IWUSR,
            )
            os.close(descriptor)
        source_connection = sqlite3.connect(source)
        try:
            destination_connection = sqlite3.connect(destination)
            try:
                source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
        finally:
            source_connection.close()
        if os.name == "posix":
            destination.chmod(stat.S_IRUSR | stat.S_IWUSR)
        descriptor = os.open(destination, os.O_WRONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(destination.parent)
    except (OSError, sqlite3.Error) as exc:
        destination.unlink(missing_ok=True)
        raise UpdateError("Geçiş öncesi önbellek yedeği oluşturulamadı.") from exc
    return destination


def restore_cache(settings: Settings, backup: str | Path | None) -> None:
    """Atomically restore the pre-candidate snapshot, including original absence."""

    source = settings.cache_db_path
    source.parent.mkdir(parents=True, exist_ok=True)
    if backup is None:
        source.unlink(missing_ok=True)
    else:
        saved = Path(backup)
        if not saved.is_file():
            raise UpdateError("Geri alma için gereken önbellek yedeğine erişilemiyor.")
        descriptor, raw = tempfile.mkstemp(
            prefix=f".{source.name}.restore-", suffix=".tmp", dir=source.parent
        )
        temporary = Path(raw)
        try:
            with os.fdopen(descriptor, "wb") as output, saved.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, source)
        finally:
            temporary.unlink(missing_ok=True)
    Path(f"{source}-wal").unlink(missing_ok=True)
    Path(f"{source}-shm").unlink(missing_ok=True)
    _fsync_directory(source.parent)


def _run_uv(uv: Path, args: list[str], *, env: Mapping[str, str] | None = None) -> None:
    try:
        completed = subprocess.run(
            [str(uv), *args],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            env=env,
        )
    except OSError as exc:
        raise UpdateError("Aday kurulumu için uv çalıştırılamadı.") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise UpdateError(
            f"uv ile aday kurulumu başarısız oldu{': ' + detail if detail else ''}"
        )


def _candidate_python(root: Path) -> Path:
    candidate = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return candidate


def _candidate_launcher(root: Path) -> Path:
    candidate = root / (
        "Scripts/mutalaamcp.exe" if os.name == "nt" else "bin/mutalaamcp"
    )
    return _absolute_executable(candidate, "aday başlatıcı")


def _absolute_executable(value: str | Path, label: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise UpdateError(f"{label} bulunamadı.") from exc
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise UpdateError(f"{label} mutlak ve çalıştırılabilir bir dosya olmalıdır.")
    return path


def _absolute_regular_file(value: str | Path, label: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise UpdateError(f"{label} bulunamadı.") from exc
    if not path.is_absolute() or not path.is_file():
        raise UpdateError(f"{label} mutlak bir normal dosya olmalıdır.")
    return path


def _validate_target_version(version: str) -> None:
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise UpdateError("Sürüm, tam bir paket sürümü belirteci olmalıdır.")


def _installed_version() -> str:
    try:
        return importlib.metadata.version("mutalaamcp")
    except importlib.metadata.PackageNotFoundError:
        return "installed"


def _read_json_object(path: Path) -> dict[str, object] | None:
    raw = _read_file(path)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise UpdateError("Etkin sürüm durumu bozuk.") from None
    if not isinstance(value, Mapping):
        raise UpdateError("Etkin sürüm durumu bozuk.")
    return dict(value)


def _read_file(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UpdateError(f"{path} okunamadı.") from exc


def _restore_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.restore-", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(previous)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
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
