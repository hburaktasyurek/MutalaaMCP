"""İsteğe bağlı, açıkça kurulan yerel PaddleOCR model yaşam döngüsü.

Bu modül, paket içe aktarma sırasında asla model indirmez veya PaddleOCR'yi içe
aktarmaya çalışmaz. Model, indirilmiş yapıtı doğrulandıktan, atomik olarak
kurulduktan ve etkin uyumlu kurulum olarak kaydedildikten sonra çalışma zamanına
görünür hâle gelir.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.resources
import ipaddress
import json
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from http.client import HTTPMessage, HTTPResponse
from pathlib import Path
from typing import IO, Any, Literal, Protocol
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from mutalaamcp.settings import Settings

MANIFEST_SCHEMA_VERSION = 2
_INSTALL_STATE_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_MAX_OCR_REDIRECTS = 3


class _NoRedirectHandler(HTTPRedirectHandler):
    """Yönlendirme yanıtlarını izlemek yerine çağırana döndürür."""

    def http_error_301(
        self,
        request: Request,
        response: IO[bytes],
        code: int,
        message: str,
        headers: HTTPMessage,
    ) -> IO[bytes]:
        del request, code, message, headers
        return response

    def http_error_302(
        self,
        request: Request,
        response: IO[bytes],
        code: int,
        message: str,
        headers: HTTPMessage,
    ) -> IO[bytes]:
        del request, code, message, headers
        return response

    def http_error_303(
        self,
        request: Request,
        response: IO[bytes],
        code: int,
        message: str,
        headers: HTTPMessage,
    ) -> IO[bytes]:
        del request, code, message, headers
        return response

    def http_error_307(
        self,
        request: Request,
        response: IO[bytes],
        code: int,
        message: str,
        headers: HTTPMessage,
    ) -> IO[bytes]:
        del request, code, message, headers
        return response

    def http_error_308(
        self,
        request: Request,
        response: IO[bytes],
        code: int,
        message: str,
        headers: HTTPMessage,
    ) -> IO[bytes]:
        del request, code, message, headers
        return response


class OcrError(RuntimeError):
    """İsteğe bağlı OCR alt sistemi güvenle yapılandırılamaz veya kullanılamaz."""


class OcrManifestError(OcrError):
    """Yerel olarak sağlanan OCR manifesti hatalı biçimlendirilmiş ya da desteklenmiyor."""


class OcrDownloadError(OcrError):
    """Bir model yapıtı kaynak, boyut veya sağlama toplamı doğrulamasını geçemedi."""


class OcrRuntimeUnavailable(OcrError):
    """Doğrulanmış bir model var, ancak isteğe bağlı PaddleOCR çalışma zamanı kullanılamıyor."""


@dataclass(frozen=True, slots=True)
class OcrDownload:
    """Sağlama toplamıyla sabitlenmiş tek resmî model arşivi."""

    name: str
    url: str
    sha256: str
    size_bytes: int
    archive: Literal["zip", "tar", "tar.gz", "file"]


@dataclass(frozen=True, slots=True)
class OcrArtifact:
    """Bir platform için birlikte kurulan model arşivleri."""

    platform: str
    downloads: tuple[OcrDownload, ...]
    files: tuple[str, ...]

    @property
    def sha256(self) -> str:
        digest = hashlib.sha256()
        for download in self.downloads:
            digest.update(download.name.encode())
            digest.update(b"\0")
            digest.update(bytes.fromhex(download.sha256))
        return digest.hexdigest()

    @property
    def size_bytes(self) -> int:
        return sum(download.size_bytes for download in self.downloads)


@dataclass(frozen=True, slots=True)
class OcrManifest:
    """Tek bir yerel OCR model ailesinin sürümle sağlanan açıklaması."""

    schema_version: int
    model_id: str
    runtime: str
    runtime_version: str | None
    runtime_options: dict[str, object]
    artifacts: dict[str, OcrArtifact]
    allowed_hosts: frozenset[str] = frozenset()

    @classmethod
    def from_mapping(cls, value: object) -> OcrManifest:
        if not isinstance(value, Mapping):
            raise OcrManifestError("OCR manifesti bir JSON nesnesi olmalıdır")
        schema_version = _positive_int(value.get("schema_version"), "schema_version")
        model_id = _safe_component(value.get("model_id"), "model_id")
        runtime = _nonempty_string(value.get("runtime"), "runtime").lower()
        runtime_version_raw = value.get("runtime_version")
        if runtime_version_raw is not None:
            runtime_version = _nonempty_string(runtime_version_raw, "runtime_version")
        else:
            runtime_version = None
        runtime_options_raw = value.get("runtime_options", {})
        if not isinstance(runtime_options_raw, Mapping):
            raise OcrManifestError("runtime_options bir nesne olmalıdır")
        allowed_hosts = _manifest_allowed_hosts(value.get("allowed_hosts"))
        artifacts_raw = value.get("artifacts")
        if not isinstance(artifacts_raw, Mapping):
            raise OcrManifestError("artifacts bir nesne olmalıdır")
        if artifacts_raw and not allowed_hosts:
            raise OcrManifestError(
                "artifacts tanımlandığında allowed_hosts boş olmamalıdır"
            )
        artifacts: dict[str, OcrArtifact] = {}
        for platform_name, raw_artifact in artifacts_raw.items():
            name = _safe_component(platform_name, "artifact platform")
            if name in artifacts:
                raise OcrManifestError(f"yinelenen OCR yapıtı platformu: {name}")
            artifacts[name] = _artifact_from_mapping(name, raw_artifact, allowed_hosts)
        return cls(
            schema_version=schema_version,
            model_id=model_id,
            runtime=runtime,
            runtime_version=runtime_version,
            runtime_options=dict(runtime_options_raw),
            artifacts=artifacts,
            allowed_hosts=allowed_hosts,
        )

    @classmethod
    def load(cls, path: str | Path) -> OcrManifest:
        source = Path(path)
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise OcrManifestError(
                f"OCR manifesti yapılandırılmamış: {source}"
            ) from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OcrManifestError(f"OCR manifesti okunamadı: {source}") from exc
        return cls.from_mapping(raw)

    def artifact_for_current_platform(self) -> OcrArtifact:
        tag = current_platform_tag()
        artifact = self.artifacts.get(tag)
        if artifact is None:
            raise OcrManifestError(f"OCR modeli {tag} platformunda kullanılamıyor")
        return artifact


@dataclass(frozen=True, slots=True)
class OcrStatus:
    """Etkin OCR kurulumunun gizli bilgi içermeyen açıklaması."""

    status: Literal["ready", "not_configured"]
    reason: str | None = None
    model_id: str | None = None
    runtime: str | None = None
    runtime_version: str | None = None
    install_dir: Path | None = None

    def as_dict(self) -> dict[str, str]:
        result: dict[str, str] = {"status": self.status}
        if self.reason is not None:
            result["reason"] = self.reason
        if self.model_id is not None:
            result["model_id"] = self.model_id
        if self.runtime is not None:
            result["runtime"] = self.runtime
        if self.runtime_version is not None:
            result["runtime_version"] = self.runtime_version
        return result


class _PaddleOcrType(Protocol):
    def __call__(self, data: bytes, source_url: str) -> str: ...


def current_platform_tag() -> str:
    """Çalışan yorumlayıcı için sürüm manifesti platform etiketini döndürür."""

    system = platform.system().lower()
    machine = platform.machine().lower()
    aliases = {"aarch64": "arm64", "amd64": "x64", "x86_64": "x64"}
    return f"{system}-{aliases.get(machine, machine)}"


def install_ocr(
    settings: Settings,
    *,
    consent: bool,
    manifest: OcrManifest | None = None,
) -> OcrStatus:
    """Yerel bir OCR modelini indirir, doğrular ve atomik olarak etkinleştirir.

    Çağıranlar süreç genelindeki değişiklik kilidini edinmelidir. Onay, hiçbir
    kod yolunun durum denetimini yanlışlıkla indirmeye dönüştürememesi için açık
    bir bağımsız değişkendir.
    """

    if consent is not True:
        raise OcrError("açık OCR kurulum onayı gereklidir")
    selected = manifest if manifest is not None else _configured_manifest(settings)
    _require_supported_manifest(settings, selected)
    artifact, unavailable_reason = _current_artifact(selected)
    if artifact is None:
        return OcrStatus(status="not_configured", reason=unavailable_reason)
    allowed_hosts = _artifact_allowed_hosts(settings, selected)
    for download in artifact.downloads:
        _validate_artifact_source(download.url, allowed_hosts)
    if artifact.size_bytes > settings.max_download_bytes:
        raise OcrDownloadError("OCR yapıtları yapılandırılan boyut sınırını aşıyor")

    root = settings.model_dir
    _ensure_private_directory(root)
    installs = root / "installs"
    _ensure_private_directory(installs)
    install_name = f"{selected.model_id}-{artifact.sha256[:16]}"
    destination = installs / install_name
    if destination.exists():
        if destination.is_dir() and not destination.is_symlink():
            _secure_model_tree(destination)
            if _matching_installation(destination, selected, artifact):
                _activate(root, install_name, selected, artifact)
                return _inspect_ocr(settings, selected, artifact)
        # A partial or incompatible same-hash path must never be activated.
        raise OcrError("mevcut OCR kurulumu manifestiyle eşleşmiyor")
    staging = Path(tempfile.mkdtemp(prefix=".ocr-staging-", dir=root))
    installed = False
    try:
        payload = staging / "payload"
        _ensure_private_directory(payload)
        for index, download in enumerate(artifact.downloads):
            downloaded = staging / f"artifact-{index}"
            _download_artifact(
                download.url,
                downloaded,
                allowed_hosts=allowed_hosts,
                max_bytes=min(settings.max_download_bytes, download.size_bytes),
            )
            _verify_artifact(downloaded, download)
            _extract_artifact(
                downloaded, payload, download, settings.max_download_bytes
            )
        _verify_expected_files(payload, artifact.files)
        installation = {
            "state_schema_version": _INSTALL_STATE_SCHEMA_VERSION,
            "manifest_schema_version": selected.schema_version,
            "model_id": selected.model_id,
            "runtime": selected.runtime,
            "runtime_version": selected.runtime_version,
            "runtime_options": selected.runtime_options,
            "platform": artifact.platform,
            "artifact_sha256": artifact.sha256,
            "artifact_size_bytes": artifact.size_bytes,
            "files": list(artifact.files),
        }
        _atomic_json_write(payload / "installation.json", installation)
        _secure_model_tree(payload)
        installs.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(payload, destination)
        except FileExistsError as exc:
            raise OcrError("OCR kurulum hedefi zaten mevcut") from exc
        _activate(root, install_name, selected, artifact)
        installed = True
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if not installed:
            _remove_empty_directory(installs)
    return _inspect_ocr(settings, selected, artifact)


def remove_ocr(settings: Settings) -> OcrStatus:
    """Yalnızca etkin seçili yerel modeli devre dışı bırakır ve kaldırır."""

    state_path = _active_state_path(settings.model_dir)
    active = _read_json_object(state_path)
    if active is None:
        return OcrStatus(status="not_configured", reason="no_active_model")
    install_name = active.get("install")
    if not isinstance(install_name, str) or not _SAFE_COMPONENT.fullmatch(install_name):
        raise OcrError("etkin OCR durumu hatalı biçimlendirilmiş")
    _atomic_unlink(state_path)
    target = settings.model_dir / "installs" / install_name
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    return OcrStatus(status="not_configured", reason="no_active_model")


def inspect_ocr(settings: Settings) -> OcrStatus:
    """İndirme yapmadan yerel kurulumu ve çalışma zamanı uyumluluğunu denetler."""

    selected = _configured_manifest(settings)
    _require_supported_manifest(settings, selected)
    artifact, unavailable_reason = _current_artifact(selected)
    if artifact is None:
        return OcrStatus(status="not_configured", reason=unavailable_reason)
    return _inspect_ocr(settings, selected, artifact)


def _inspect_ocr(
    settings: Settings, manifest: OcrManifest, artifact: OcrArtifact
) -> OcrStatus:
    active = _read_json_object(_active_state_path(settings.model_dir))
    if active is None:
        return OcrStatus(status="not_configured", reason="no_active_model")
    install_name = active.get("install")
    if not isinstance(install_name, str) or not _SAFE_COMPONENT.fullmatch(install_name):
        return OcrStatus(status="not_configured", reason="invalid_active_state")
    install_dir = settings.model_dir / "installs" / install_name
    if install_dir.is_symlink():
        return OcrStatus(status="not_configured", reason="invalid_active_state")
    return _status_from_install(
        settings, install_dir, manifest=manifest, artifact=artifact, active=active
    )


def resolve_local_ocr(settings: Settings) -> _PaddleOcrType | None:
    """Yalnızca doğrulanmış hazır kurulum için gerçekten yerel bir OCR çağrılabiliri döndürür."""

    status = inspect_ocr(settings)
    if status.status != "ready" or status.install_dir is None:
        return None
    installation = _read_json_object(status.install_dir / "installation.json")
    if installation is None:
        return None
    try:
        paddleocr = importlib.import_module("paddleocr")
    except Exception:  # noqa: BLE001 -- optional PaddleOCR import may be unavailable
        return None
    factory = getattr(paddleocr, "PaddleOCR", None)
    if not callable(factory):
        return None
    return _LocalPaddleOcr(factory, status.install_dir, installation)


class _LocalPaddleOcr:
    """Gerçek PaddleOCR kurulumu etrafında, uyumluluk taklidi olmayan tembel bağdaştırıcı."""

    def __init__(
        self, factory: Any, install_dir: Path, installation: Mapping[str, Any]
    ) -> None:
        self._factory = factory
        self._install_dir = install_dir
        options = installation.get("runtime_options", {})
        self._options = dict(options) if isinstance(options, Mapping) else {}
        self._engine: Any | None = None

    def __call__(self, data: bytes, source_url: str) -> str:
        if self._engine is None:
            options = dict(self._options)
            for key, value in tuple(options.items()):
                if key.endswith("_model_dir") and isinstance(value, str):
                    relative = _safe_archive_path(value)
                    model_dir = self._install_dir / relative
                    if not model_dir.is_dir() or model_dir.is_symlink():
                        raise OcrRuntimeUnavailable(
                            f"PaddleOCR model dizini kullanılamıyor: {value}"
                        )
                    options[key] = str(model_dir)
            try:
                self._engine = self._factory(**options)
            except Exception as exc:
                raise OcrRuntimeUnavailable(
                    "PaddleOCR yerel olarak başlatılamadı"
                ) from exc
        suffix = _ocr_input_suffix(data, source_url)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix="mutalaamcp-ocr-", suffix=suffix
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
            if callable(getattr(self._engine, "predict", None)):
                result = self._engine.predict(temporary_path)
            elif callable(getattr(self._engine, "ocr", None)):
                result = self._engine.ocr(temporary_path, cls=True)
            else:
                raise OcrRuntimeUnavailable(
                    "PaddleOCR çalışma zamanında OCR giriş noktası yok"
                )
            text = _ocr_text(result)
            if not text:
                raise OcrRuntimeUnavailable("PaddleOCR tanınmış metin döndürmedi")
            return text
        finally:
            Path(temporary_path).unlink(missing_ok=True)


def _ocr_input_suffix(data: bytes, source_url: str) -> str:
    if data.startswith(b"%PDF-"):
        return ".pdf"
    signatures = (
        (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"\xff\xd8\xff", ".jpg"),
        (b"BM", ".bmp"),
        (b"II*\x00", ".tiff"),
        (b"MM\x00*", ".tiff"),
    )
    for signature, suffix in signatures:
        if data.startswith(signature):
            return suffix
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if len(data) > 2 and data[:2] in {b"P1", b"P2", b"P3", b"P4", b"P5", b"P6"}:
        return ".pnm"
    suffix = Path(urlsplit(source_url).path).suffix.lower()
    if suffix in {
        ".bmp",
        ".dib",
        ".jpeg",
        ".jpg",
        ".png",
        ".webp",
        ".pbm",
        ".pgm",
        ".ppm",
        ".pnm",
        ".sr",
        ".ras",
        ".tiff",
        ".tif",
        ".pdf",
    }:
        return suffix
    raise OcrRuntimeUnavailable("PaddleOCR giriş biçimi desteklenmiyor")


def _configured_manifest(settings: Settings) -> OcrManifest:
    if settings.ocr_manifest_path is not None:
        return OcrManifest.load(settings.ocr_manifest_path)
    try:
        resource = importlib.resources.files("mutalaamcp").joinpath(
            "release", "ocr-models-v1.json"
        )
        raw = json.loads(resource.read_text(encoding="utf-8"))
    except FileNotFoundError:
        source_checkout_manifest = (
            Path(__file__).parents[2] / "release" / "ocr-models-v1.json"
        )
        if source_checkout_manifest.is_file():
            return OcrManifest.load(source_checkout_manifest)
        raise OcrManifestError("paketlenmiş OCR manifesti kullanılamıyor") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OcrManifestError("paketlenmiş OCR manifesti okunamadı") from exc
    return OcrManifest.from_mapping(raw)


def _current_artifact(manifest: OcrManifest) -> tuple[OcrArtifact | None, str]:
    if not manifest.artifacts:
        return None, "model_artifacts_unavailable"
    artifact = manifest.artifacts.get(current_platform_tag())
    if artifact is None:
        return None, "model_unavailable_for_platform"
    return artifact, ""


def _artifact_allowed_hosts(
    settings: Settings, manifest: OcrManifest
) -> frozenset[str]:
    if not manifest.allowed_hosts:
        return settings.ocr_allowed_hosts
    return settings.ocr_allowed_hosts & manifest.allowed_hosts


def _require_supported_manifest(settings: Settings, manifest: OcrManifest) -> None:
    if manifest.schema_version not in settings.ocr_supported_manifest_versions:
        raise OcrManifestError(
            f"OCR manifest şeması {manifest.schema_version} bu sürüm tarafından desteklenmiyor"
        )
    if manifest.runtime != "paddleocr":
        raise OcrManifestError(
            f"OCR çalışma zamanı {manifest.runtime!r} bu sürüm tarafından desteklenmiyor"
        )


def _artifact_from_mapping(
    platform_name: str, value: object, allowed_hosts: frozenset[str]
) -> OcrArtifact:
    if not isinstance(value, Mapping):
        raise OcrManifestError(f"artifact {platform_name} bir nesne olmalıdır")
    files_raw = value.get("files", ())
    if not isinstance(files_raw, (list, tuple)) or any(
        not isinstance(item, str) for item in files_raw
    ):
        raise OcrManifestError(
            f"artifact {platform_name}.files bir dize listesi olmalıdır"
        )
    files = tuple(files_raw)
    for name in files:
        _safe_archive_path(name)

    downloads_raw = value.get("downloads")
    if downloads_raw is None:
        downloads_raw = (value,)
    if not isinstance(downloads_raw, (list, tuple)) or not downloads_raw:
        raise OcrManifestError(
            f"artifact {platform_name}.downloads boş olmayan bir liste olmalıdır"
        )
    downloads: list[OcrDownload] = []
    seen_names: set[str] = set()
    for index, raw_download in enumerate(downloads_raw):
        download = _download_from_mapping(
            platform_name, index, raw_download, allowed_hosts
        )
        if download.name in seen_names:
            raise OcrManifestError(
                f"artifact {platform_name}.downloads yinelenen ad içeriyor: {download.name}"
            )
        seen_names.add(download.name)
        downloads.append(download)
    return OcrArtifact(
        platform=platform_name,
        downloads=tuple(downloads),
        files=files,
    )


def _download_from_mapping(
    platform_name: str,
    index: int,
    value: object,
    allowed_hosts: frozenset[str],
) -> OcrDownload:
    if not isinstance(value, Mapping):
        raise OcrManifestError(
            f"artifact {platform_name}.downloads[{index}] bir nesne olmalıdır"
        )
    default_name = "model" if index == 0 else f"model-{index + 1}"
    name = _safe_component(value.get("name", default_name), "download name")
    label = f"artifact {platform_name}.downloads[{index}]"
    url = _nonempty_string(value.get("url"), f"{label}.url")
    _validate_manifest_artifact_source(label, url, allowed_hosts)
    sha256 = _nonempty_string(value.get("sha256"), f"{label}.sha256").lower()
    if not _SHA256.fullmatch(sha256):
        raise OcrManifestError(f"{label}.sha256 bir SHA-256 onaltılık özeti olmalıdır")
    size_bytes = _positive_int(value.get("size_bytes"), f"{label}.size_bytes")
    archive = value.get("archive", "zip")
    if archive not in {"zip", "tar", "tar.gz", "file"}:
        raise OcrManifestError(f"{label}.archive desteklenmiyor")
    return OcrDownload(
        name=name,
        url=url,
        sha256=sha256,
        size_bytes=size_bytes,
        archive=archive,
    )


def _manifest_allowed_hosts(value: object) -> frozenset[str]:
    if not isinstance(value, (list, tuple)):
        raise OcrManifestError("allowed_hosts bir dize listesi olmalıdır")
    hosts: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise OcrManifestError(
                "allowed_hosts boş olmayan bir dize listesi olmalıdır"
            )
        host = raw.strip().lower()
        if not _is_safe_artifact_hostname(host):
            raise OcrManifestError(
                "allowed_hosts genel DNS ana makine adları içermelidir"
            )
        hosts.add(host)
    return frozenset(hosts)


def _is_safe_artifact_hostname(host: str | None) -> bool:
    if host is None or _HOSTNAME.fullmatch(host) is None:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def _validate_manifest_artifact_source(
    label: str, url: str, allowed_hosts: frozenset[str]
) -> None:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise OcrManifestError(
            f"{label}.url geçersiz bir bağlantı noktası içeriyor"
        ) from exc
    host = parsed.hostname.lower() if parsed.hostname else None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not _is_safe_artifact_hostname(host)
        or host not in allowed_hosts
        or parsed.fragment
    ):
        raise OcrManifestError(
            f"{label}.url izin listesindeki bir HTTPS ana makinesini kullanmalıdır"
        )


def _open_ocr_request(request: Request, *, timeout: float) -> HTTPResponse:
    response = build_opener(_NoRedirectHandler()).open(request, timeout=timeout)
    if not isinstance(response, HTTPResponse):
        response.close()
        raise OcrDownloadError("OCR yapıtı indirmesi geçersiz bir yanıt döndürdü")
    return response


def _download_artifact(
    url: str,
    target: Path,
    *,
    allowed_hosts: Iterable[str],
    max_bytes: int,
) -> None:
    _validate_artifact_source(url, allowed_hosts)
    current_url = url
    seen_urls: set[str] = set()
    try:
        for redirect_count in range(_MAX_OCR_REDIRECTS + 1):
            _validate_artifact_source(current_url, allowed_hosts)
            if current_url in seen_urls:
                raise OcrDownloadError("OCR yapıtı yönlendirme döngüsü algılandı")
            seen_urls.add(current_url)
            request = Request(
                current_url, headers={"User-Agent": "MutalaaMCP OCR installer"}
            )
            response = _open_ocr_request(request, timeout=30)
            try:
                status = response.getcode()
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not isinstance(location, str) or not location:
                        raise OcrDownloadError(
                            "OCR yapıtı yönlendirmesinde Location yok"
                        )
                    next_url = urljoin(current_url, location)
                    _validate_artifact_source(next_url, allowed_hosts)
                    if next_url in seen_urls:
                        raise OcrDownloadError(
                            "OCR yapıtı yönlendirme döngüsü algılandı"
                        )
                    if redirect_count >= _MAX_OCR_REDIRECTS:
                        raise OcrDownloadError("OCR yapıtı yönlendirme sınırı aşıldı")
                    current_url = next_url
                    continue
                if status is None or not 200 <= status < 300:
                    raise OcrDownloadError("OCR yapıtı indirmesi hata durumuyla döndü")
                declared_size = response.headers.get("Content-Length")
                if declared_size is not None:
                    try:
                        content_length = int(declared_size)
                    except ValueError as exc:
                        raise OcrDownloadError(
                            "OCR yapıtı geçersiz bir Content-Length değerine sahip"
                        ) from exc
                    if content_length < 0 or content_length > max_bytes:
                        raise OcrDownloadError(
                            "OCR yapıtı yapılandırılan boyut sınırını aşıyor"
                        )
                total = 0
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(target, flags, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    while chunk := response.read(64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise OcrDownloadError(
                                "OCR yapıtı yapılandırılan boyut sınırını aşıyor"
                            )
                        output.write(chunk)
                return
            finally:
                response.close()
    except OcrError:
        target.unlink(missing_ok=True)
        raise
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise OcrDownloadError("OCR yapıtı indirilemedi") from exc


def _validate_artifact_source(url: str, allowed_hosts: Iterable[str]) -> None:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise OcrDownloadError(
            "OCR yapıtı URL'si geçersiz bir bağlantı noktası içeriyor"
        ) from exc
    host = parsed.hostname.lower() if parsed.hostname else None
    allowlist = {item.lower() for item in allowed_hosts}
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not _is_safe_artifact_hostname(host)
        or host not in allowlist
        or parsed.fragment
    ):
        raise OcrDownloadError(
            "OCR yapıtı URL'si izin listesindeki bir HTTPS ana makinesini kullanmalıdır"
        )


def _verify_artifact(path: Path, artifact: OcrDownload) -> None:
    if not _is_safe_regular_file(path):
        raise OcrDownloadError("OCR yapıtı normal bir dosya değil")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise OcrDownloadError("OCR yapıtı doğrulama öncesinde kayboldu") from exc
    if size != artifact.size_bytes:
        raise OcrDownloadError("OCR yapıtı boyutu manifestiyle eşleşmiyor")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != artifact.sha256:
        raise OcrDownloadError("OCR yapıtının sağlama toplamı manifestiyle eşleşmiyor")


def _extract_artifact(
    source: Path, destination: Path, artifact: OcrDownload, max_bytes: int
) -> None:
    try:
        if artifact.archive == "file":
            shutil.copyfile(source, destination / "model.bin")
            return
        if artifact.archive == "zip":
            with zipfile.ZipFile(source) as archive:
                zip_members: list[zipfile.ZipInfo] = archive.infolist()
                total = sum(member.file_size for member in zip_members)
                if total > max_bytes:
                    raise OcrDownloadError(
                        "OCR arşivi açıldığında yapılandırılan boyut sınırını aşıyor"
                    )
                for zip_member in zip_members:
                    _safe_archive_path(zip_member.filename)
                    if stat.S_ISLNK(zip_member.external_attr >> 16):
                        raise OcrDownloadError(
                            "OCR arşivi sembolik bağlantılar içermemelidir"
                        )
                archive.extractall(destination)
            return
        mode: Literal["r:", "r:gz"] = "r:" if artifact.archive == "tar" else "r:gz"
        with tarfile.open(source, mode=mode) as archive:
            tar_members: list[tarfile.TarInfo] = archive.getmembers()
            total = sum(member.size for member in tar_members if member.isfile())
            if total > max_bytes:
                raise OcrDownloadError(
                    "OCR arşivi açıldığında yapılandırılan boyut sınırını aşıyor"
                )
            for tar_member in tar_members:
                _safe_archive_path(tar_member.name)
                if tar_member.issym() or tar_member.islnk() or tar_member.isdev():
                    raise OcrDownloadError(
                        "OCR arşivi bağlantı veya aygıt dosyaları içermemelidir"
                    )
            archive.extractall(destination, filter="data")
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise OcrDownloadError("doğrulanmış OCR yapıtı ayıklanamadı") from exc


def _verify_expected_files(root: Path, expected: Iterable[str]) -> None:
    for relative in expected:
        safe = _safe_archive_path(relative)
        candidate = root / safe
        if not _is_safe_regular_file(candidate):
            raise OcrDownloadError(
                f"OCR arşivinde gerekli model dosyası eksik: {relative}"
            )


def _safe_archive_path(value: str) -> Path:
    candidate = Path(value)
    if (
        not value
        or candidate.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise OcrDownloadError("OCR arşivi güvenli olmayan bir yol içeriyor")
    return candidate


def _is_safe_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise OcrError(f"OCR yolu bir dizin olmalıdır: {path}")
        if os.name == "posix":
            os.chmod(path, 0o700)
    except OcrError:
        raise
    except OSError as exc:
        raise OcrError(f"OCR dizini güvenli hâle getirilemedi: {path}") from exc


def _remove_empty_directory(path: Path) -> None:
    """Bir yaşam döngüsü dizinini yalnızca boşsa kaldırır."""

    try:
        path.rmdir()
    except OSError:
        pass


def _secure_model_tree(root: Path) -> None:
    _ensure_private_directory(root)
    try:
        for raw_root, directories, files in os.walk(root, followlinks=False):
            current = Path(raw_root)
            for name in directories:
                path = current / name
                if not stat.S_ISDIR(path.lstat().st_mode):
                    raise OcrDownloadError(
                        "OCR modeli sembolik bağlantılar içermemelidir"
                    )
                if os.name == "posix":
                    os.chmod(path, 0o700)
            for name in files:
                path = current / name
                if not _is_safe_regular_file(path):
                    raise OcrDownloadError(
                        "OCR modeli yalnızca normal dosyalar içermelidir"
                    )
                if os.name == "posix":
                    os.chmod(path, 0o600)
    except OcrDownloadError:
        raise
    except OSError as exc:
        raise OcrDownloadError("OCR model dosyaları güvenli hâle getirilemedi") from exc


def _activate(
    root: Path, install_name: str, manifest: OcrManifest, artifact: OcrArtifact
) -> None:
    _atomic_json_write(
        _active_state_path(root),
        {
            "state_schema_version": _INSTALL_STATE_SCHEMA_VERSION,
            "install": install_name,
            "manifest_schema_version": manifest.schema_version,
            "model_id": manifest.model_id,
            "runtime": manifest.runtime,
            "runtime_version": manifest.runtime_version,
            "platform": artifact.platform,
            "artifact_sha256": artifact.sha256,
        },
    )


def _matching_installation(
    install_dir: Path, manifest: OcrManifest, artifact: OcrArtifact
) -> bool:
    installation = _read_json_object(install_dir / "installation.json")
    if installation is None:
        return False
    files = installation.get("files")
    return (
        installation.get("state_schema_version") == _INSTALL_STATE_SCHEMA_VERSION
        and installation.get("manifest_schema_version") == manifest.schema_version
        and installation.get("model_id") == manifest.model_id
        and installation.get("runtime") == manifest.runtime
        and installation.get("runtime_version") == manifest.runtime_version
        and installation.get("runtime_options") == manifest.runtime_options
        and installation.get("platform") == artifact.platform
        and installation.get("artifact_sha256") == artifact.sha256
        and installation.get("artifact_size_bytes") == artifact.size_bytes
        and isinstance(files, list)
        and tuple(files) == artifact.files
    )


def _status_from_install(
    settings: Settings,
    install_dir: Path,
    *,
    manifest: OcrManifest,
    artifact: OcrArtifact,
    active: Mapping[str, object] | None = None,
) -> OcrStatus:
    installation = _read_json_object(install_dir / "installation.json")
    if installation is None:
        return OcrStatus(
            status="not_configured", reason="missing_installation_manifest"
        )
    if installation.get("state_schema_version") != _INSTALL_STATE_SCHEMA_VERSION:
        return OcrStatus(
            status="not_configured", reason="incompatible_installation_state"
        )
    schema_version = installation.get("manifest_schema_version")
    if (
        not isinstance(schema_version, int)
        or schema_version not in settings.ocr_supported_manifest_versions
    ):
        return OcrStatus(status="not_configured", reason="incompatible_manifest_schema")
    model_id = installation.get("model_id")
    runtime = installation.get("runtime")
    runtime_version = installation.get("runtime_version")
    artifact_sha256 = installation.get("artifact_sha256")
    installed_platform = installation.get("platform")
    files = installation.get("files")
    if (
        not isinstance(model_id, str)
        or not _SAFE_COMPONENT.fullmatch(model_id)
        or runtime != "paddleocr"
        or (runtime_version is not None and not isinstance(runtime_version, str))
        or not isinstance(artifact_sha256, str)
        or not _SHA256.fullmatch(artifact_sha256)
        or installed_platform != current_platform_tag()
        or not isinstance(files, list)
        or any(not isinstance(name, str) for name in files)
    ):
        return OcrStatus(
            status="not_configured", reason="invalid_installation_manifest"
        )
    if not _matching_installation(install_dir, manifest, artifact):
        return OcrStatus(
            status="not_configured",
            reason="installed_model_incompatible_with_manifest",
        )
    try:
        _verify_expected_files(install_dir, files)
    except OcrDownloadError:
        return OcrStatus(status="not_configured", reason="missing_model_files")
    if active is not None:
        for key in (
            "model_id",
            "runtime",
            "runtime_version",
            "artifact_sha256",
            "manifest_schema_version",
            "platform",
        ):
            if active.get(key) != installation.get(key):
                return OcrStatus(
                    status="not_configured", reason="active_state_mismatch"
                )
    try:
        paddleocr = importlib.import_module("paddleocr")
    except Exception:  # noqa: BLE001 -- optional PaddleOCR import may be unavailable
        return OcrStatus(
            status="not_configured",
            reason="paddleocr_runtime_unavailable",
            model_id=model_id,
            runtime=runtime,
            runtime_version=runtime_version
            if isinstance(runtime_version, str)
            else None,
            install_dir=install_dir,
        )
    if not callable(getattr(paddleocr, "PaddleOCR", None)):
        return OcrStatus(
            status="not_configured",
            reason="paddleocr_runtime_invalid",
            model_id=model_id,
            runtime=runtime,
            runtime_version=runtime_version
            if isinstance(runtime_version, str)
            else None,
            install_dir=install_dir,
        )
    installed_runtime_version = getattr(paddleocr, "__version__", None)
    if (
        isinstance(runtime_version, str)
        and runtime_version
        and installed_runtime_version != runtime_version
    ):
        return OcrStatus(
            status="not_configured",
            reason="paddleocr_runtime_incompatible",
            model_id=model_id,
            runtime=runtime,
            runtime_version=runtime_version,
            install_dir=install_dir,
        )
    return OcrStatus(
        status="ready",
        model_id=model_id,
        runtime=runtime,
        runtime_version=runtime_version if isinstance(runtime_version, str) else None,
        install_dir=install_dir,
    )


def _active_state_path(root: Path) -> Path:
    return root / "active.json"


def _read_json_object(path: Path) -> dict[str, object] | None:
    if not _is_safe_regular_file(path):
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    _ensure_private_directory(path.parent)
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
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_unlink(path: Path) -> None:
    if not path.exists():
        return
    tombstone = path.with_name(f".{path.name}.{uuid.uuid4().hex}.removed")
    os.replace(path, tombstone)
    _fsync_directory(path.parent)
    tombstone.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise OcrManifestError(f"{name} pozitif bir tam sayı olmalıdır")
    return value


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OcrManifestError(f"{name} boş olmayan bir dize olmalıdır")
    return value.strip()


def _safe_component(value: object, name: str) -> str:
    token = _nonempty_string(value, name)
    if not _SAFE_COMPONENT.fullmatch(token):
        raise OcrManifestError(f"{name} güvenli bir dosya adı bileşeni olmalıdır")
    return token


def _ocr_text(value: object) -> str:
    """PaddleOCR'nin sürüme bağlı sonuç biçimlerindeki metin yapraklarını ayıklar."""

    leaves: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, str):
            if item.strip():
                leaves.append(item.strip())
        elif isinstance(item, Mapping):
            for key in ("rec_texts", "text", "texts"):
                if key in item:
                    visit(item[key])
        elif isinstance(item, Iterable) and not isinstance(item, (bytes, bytearray)):
            for child in item:
                visit(child)

    visit(value)
    return "\n".join(dict.fromkeys(leaves))
