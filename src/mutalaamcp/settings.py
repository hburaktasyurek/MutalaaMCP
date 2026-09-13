"""Environment-backed settings. No secrets, no I/O on import."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from platformdirs import user_cache_dir, user_data_dir
from pydantic import Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

APP_NAME = "MutalaaMCP"
_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024
_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_OCR_ALLOWED_HOSTS = frozenset(
    {"models.mutalaa.tr", "paddle-model-ecology.bj.bcebos.com"}
)
_OCR_MANIFEST_SCHEMA_VERSIONS = frozenset({1, 2})

_DEFAULT_AUTH_BASE_URL = "https://mutalaa.tr"
_DEFAULT_TERMS_BASE_URL = "https://mutalaa.tr/terms"
_DEFAULT_UPDATE_MANIFEST_URL = (
    "https://github.com/hburaktasyurek/MutalaaMCP"
    "/releases/latest/download/update-manifest-v1.json"
)


def normalize_auth_origin(value: str, *, field: str) -> str:
    """Normalize a secure auth origin, allowing HTTP only for local test servers."""
    parsed = urlsplit(value.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"{field} http veya https olmalıdır")
    if parsed.username or parsed.password:
        raise ValueError(f"{field} kimlik bilgileri içermemelidir")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"{field} bir ana bilgisayar içermelidir")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field} sorgu veya parça içermemelidir")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} geçersiz bir bağlantı noktası içeriyor") from exc
    host = hostname.rstrip(".").lower()
    if scheme != "https" and not _is_explicit_loopback(host):
        raise ValueError(
            f"{field}, açıkça belirtilmiş bir geri döngü test kökeni olmadıkça https olmalıdır"
        )
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    netloc = host if port is None or port == default_port else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _is_explicit_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _default_cache_dir() -> Path:
    return Path(user_cache_dir(APP_NAME, appauthor=False))


def _default_data_dir() -> Path:
    return Path(user_data_dir(APP_NAME, appauthor=False))


def _default_model_dir() -> Path:
    return _default_data_dir() / "models"


class Settings(BaseSettings):
    """Public runtime configuration loaded from ``MUTALAAMCP_`` environment variables.

    Authentication is brokered by Mütalaa. Refresh tokens, access tokens, and
    API keys must not be stored on this object.
    """

    model_config = SettingsConfigDict(
        env_prefix="MUTALAAMCP_",
        extra="forbid",
        frozen=True,
        env_file=None,
        secrets_dir=None,
        env_ignore_empty=True,
        validate_default=True,
        enable_decoding=False,
        case_sensitive=False,
    )

    auth_base_url: str = Field(default=_DEFAULT_AUTH_BASE_URL, min_length=1)
    terms_base_url: str = Field(default=_DEFAULT_TERMS_BASE_URL, min_length=1)
    update_manifest_url: str = Field(default=_DEFAULT_UPDATE_MANIFEST_URL, min_length=1)
    cache_dir: Path = Field(default_factory=_default_cache_dir)
    data_dir: Path = Field(default_factory=_default_data_dir)
    model_dir: Path = Field(default_factory=_default_model_dir)
    http_timeout_seconds: float = Field(default=30.0, gt=0)
    http_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    ocr_timeout_seconds: float = Field(default=120.0, gt=0)
    activation_timeout_seconds: float = Field(default=30.0, gt=0)
    http_port: int = Field(default=18769, ge=1024, le=65535)
    cache_max_bytes: int = Field(default=_CACHE_MAX_BYTES, gt=0)
    max_download_bytes: int = Field(default=_DOWNLOAD_MAX_BYTES, gt=0)
    ocr_manifest_path: Path | None = None
    ocr_allowed_hosts: frozenset[str] = Field(
        default_factory=lambda: _DEFAULT_OCR_ALLOWED_HOSTS
    )
    ocr_supported_manifest_versions: frozenset[int] = Field(
        default_factory=lambda: _OCR_MANIFEST_SCHEMA_VERSIONS
    )
    uv_executable: Path | None = None
    max_pdf_bytes: int = Field(default=_DOWNLOAD_MAX_BYTES, gt=0)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del settings_cls, dotenv_settings, file_secret_settings
        return (init_settings, env_settings)

    @field_validator("auth_base_url")
    @classmethod
    def _auth_base_url(cls, value: str) -> str:
        return normalize_auth_origin(
            value, field="Mütalaa kimlik doğrulama temel URL'si"
        )

    @field_validator("terms_base_url")
    @classmethod
    def _terms_base_url(cls, value: str) -> str:
        return normalize_auth_origin(value, field="koşullar temel URL'si")

    @field_validator(
        "cache_dir", "data_dir", "model_dir", "ocr_manifest_path", "uv_executable"
    )
    @classmethod
    def _absolute_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("yol mutlak olmalıdır")
        return path

    @field_validator("ocr_allowed_hosts", mode="before")
    @classmethod
    def _ocr_allowed_hosts(cls, value: object) -> frozenset[str]:
        raw_values = value.split(",") if isinstance(value, str) else value
        if not isinstance(raw_values, (set, frozenset, list, tuple)):
            raise TypeError(
                "OCR için izin verilen ana bilgisayarlar, virgülle ayrılmış bir ana bilgisayar listesi olmalıdır"
            )
        hosts: set[str] = set()
        for raw in raw_values:
            if not isinstance(raw, str):
                raise TypeError(
                    "OCR için izin verilen ana bilgisayarlar yalnızca dizgeler içermelidir"
                )
            host = raw.strip().lower()
            if not host or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
                raise ValueError(
                    "OCR için izin verilen ana bilgisayarlar DNS ana bilgisayar adları içermelidir"
                )
            hosts.add(host)
        if not hosts:
            raise ValueError("OCR için izin verilen ana bilgisayarlar boş olmamalıdır")
        return frozenset(hosts)

    @field_validator("ocr_supported_manifest_versions", mode="before")
    @classmethod
    def _ocr_supported_manifest_versions(cls, value: object) -> frozenset[int]:
        raw_values = value.split(",") if isinstance(value, str) else value
        if not isinstance(raw_values, (set, frozenset, list, tuple)):
            raise TypeError(
                "OCR için desteklenen manifest sürümleri, virgülle ayrılmış bir tam sayı listesi olmalıdır"
            )
        versions: set[int] = set()
        for raw in raw_values:
            try:
                version = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "OCR için desteklenen manifest sürümleri tam sayılar içermelidir"
                ) from exc
            if version < 1:
                raise ValueError("OCR manifest sürümleri pozitif olmalıdır")
            versions.add(version)
        if not versions:
            raise ValueError("OCR için desteklenen manifest sürümleri boş olmamalıdır")
        return frozenset(versions)

    @property
    def serve_lock_path(self) -> Path:
        return self.data_dir / "serve.lock"

    @property
    def cache_db_path(self) -> Path:
        return self.cache_dir / "cache.sqlite3"

    @property
    def version_root(self) -> Path:
        return self.data_dir / "versions"

    @property
    def active_version_state_path(self) -> Path:
        return self.data_dir / "active-version.json"
