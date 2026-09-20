"""Install the bundled discovery skill alongside MCP setup, without network access."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import zipfile
from importlib import resources
from pathlib import Path

from mutalaamcp.client_templates import SUPPORTED_CLIENTS

SKILL_NAME = "mutalaa-turk-hukuku"
SKILL_SOURCE_URL = (
    f"https://github.com/hburaktasyurek/MutalaaMCP/tree/main/skills/{SKILL_NAME}"
)
SKILL_GUIDE_URL = (
    "https://github.com/hburaktasyurek/MutalaaMCP/blob/main/docs/mutalaa-skill.md"
)


def bundled_skill() -> bytes:
    """Read the single source shipped in wheels, or the development checkout."""
    package = resources.files("mutalaamcp")
    relative = f"skills/{SKILL_NAME}/SKILL.md"
    resource = package.joinpath(relative)
    if not resource.is_file():
        resource = package.joinpath("..", "..", relative)
    return resource.read_bytes()


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _local_destination(client: str) -> Path:
    """Avoid introducing a shared copy that conflicts with either client."""
    home = Path.home()
    shared = home / ".agents" / "skills" / SKILL_NAME / "SKILL.md"
    existing = {}
    for name in ("codex", "cursor"):
        path = home / f".{name}" / "skills" / SKILL_NAME / "SKILL.md"
        if path.exists() or path.is_symlink() or path.parent.is_symlink():
            existing[name] = path
    if shared.exists() or shared.is_symlink() or shared.parent.is_symlink():
        if existing:
            paths = ", ".join(str(path) for path in (shared, *existing.values()))
            raise OSError(
                f"Birden fazla Mütalaa becerisi var; tek kopya bırakın: {paths}"
            )
        destination = shared
    elif client in existing:
        destination = existing[client]
    elif existing:
        paths = ", ".join(str(path.parent) for path in existing.values())
        raise OSError(
            f"Diğer istemcide Mütalaa becerisi var: {paths}. "
            f"İki istemci için önce beceriyi ortak klasöre taşıyın: {shared.parent}"
        )
    else:
        destination = shared
    if destination.is_symlink() or destination.parent.is_symlink():
        raise OSError(
            f"Beceri sembolik bağlantısı korunuyor; elle güncelleyin: {destination}"
        )
    return destination


def setup_companion_skill(client: str, data_dir: Path) -> str:
    """Return setup status; preserve existing content when updating a local skill."""
    if client not in SUPPORTED_CLIENTS:
        raise ValueError(f"Desteklenmeyen MCP istemcisi: {client!r}")
    content = bundled_skill()
    # Both clients discover this user-level directory. Share one copy if both
    # are configured, instead of creating conflicting per-client duplicates.
    if client in {"codex", "cursor"}:
        destination = _local_destination(client)
        backup: Path | None = None
        if destination.exists():
            previous = destination.read_bytes()
            if previous == content:
                return f"Mütalaa becerisi güncel: {destination}"
            digest = hashlib.sha256(previous).hexdigest()
            backup = destination.with_name(f"SKILL.md.{digest}.bak")
            if backup.is_symlink() or (
                backup.exists() and backup.read_bytes() != previous
            ):
                raise OSError(
                    f"Beceri yedeği doğrulanamadı; mevcut dosya korundu: {backup}"
                )
            if not backup.exists():
                _write_atomic(backup, previous)
        _write_atomic(destination, content)
        message = f"Mütalaa becerisi kuruldu: {destination}. Yeni sohbet açın."
        if backup is not None:
            message += f" Önceki içerik korundu: {backup}"
        return message

    # Desktop/cloud import is controlled by the app, not a local MCP server.
    # Offer the same bundled skill without claiming that import has happened.
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr(f"{SKILL_NAME}/SKILL.md", content)
    destination = data_dir / "skills" / f"{SKILL_NAME}.zip"
    _write_atomic(destination, archive.getvalue())
    if client == "claude-desktop":
        next_step = "Claude'da Customize > Skills üzerinden ZIP dosyasını içe aktarın ve etkinleştirin."
    else:
        next_step = (
            "Uygulamanız skill destekliyorsa kendi içe aktarma yöntemiyle ekleyin."
        )
    return (
        f"Mütalaa beceri paketi hazır: {destination}. {next_step} "
        f"Beceri henüz uygulamaya kurulmuş sayılmaz. Rehber: {SKILL_GUIDE_URL}"
    )
