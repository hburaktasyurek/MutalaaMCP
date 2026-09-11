"""Desteklenen MCP istemcileri için gizli bilgi içermeyen stdio yapılandırma oluşturucuları."""

from __future__ import annotations

import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Final

SUPPORTED_CLIENTS: Final[tuple[str, ...]] = (
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

_SERVER_NAME: Final = "mutalaamcp"
_SERVE_ARGUMENT: Final = "serve"


def render_client_config(client: str, launcher: str) -> str:
    """``client`` için belirlenimci, gizli bilgi içermeyen bir stdio yapılandırması oluşturur.

    Başlatıcının, yerel kurulum akışı tarafından önceden seçilmiş mutlak bir yol
    olması gerekir. Başlatıcı, desteklenen iki V1 platformundan herhangi birinde
    mutlak olduğunda kabul edilir; böylece donanımlar ve sürüm araçları
    platformdan bağımsız kalır.
    """
    if client not in SUPPORTED_CLIENTS:
        supported = ", ".join(SUPPORTED_CLIENTS)
        raise ValueError(
            f"Desteklenmeyen MCP istemcisi {client!r}; desteklenen istemciler: {supported}"
        )
    if not isinstance(launcher, str) or not _is_absolute_launcher(launcher):
        raise ValueError("MCP başlatıcısı mutlak bir yol olmalıdır")

    if client == "codex":
        return _render_codex_toml(launcher)
    if client == "opencode-desktop":
        return _render_opencode_json(launcher)
    if client == "hermes-agent":
        return _render_hermes_yaml(launcher)
    if client in {"witsy", "cherry-studio"}:
        return _render_stdio_fields(launcher)
    return _render_standard_json(launcher)


def _is_absolute_launcher(launcher: str) -> bool:
    if "\x00" in launcher or "\r" in launcher or "\n" in launcher:
        return False
    return (
        PurePosixPath(launcher).is_absolute() or PureWindowsPath(launcher).is_absolute()
    )


def _render_standard_json(launcher: str) -> str:
    return _json(
        {
            "mcpServers": {
                _SERVER_NAME: {
                    "command": launcher,
                    "args": [_SERVE_ARGUMENT],
                }
            }
        }
    )


def _render_opencode_json(launcher: str) -> str:
    return _json(
        {
            "mcp": {
                _SERVER_NAME: {
                    "type": "local",
                    "command": [launcher, _SERVE_ARGUMENT],
                }
            }
        }
    )


def _render_codex_toml(launcher: str) -> str:
    quoted_launcher = json.dumps(launcher, ensure_ascii=False)
    return (
        f"[mcp_servers.{_SERVER_NAME}]\n"
        f"command = {quoted_launcher}\n"
        f'args = ["{_SERVE_ARGUMENT}"]\n'
    )


def _render_hermes_yaml(launcher: str) -> str:
    quoted_launcher = json.dumps(launcher, ensure_ascii=False)
    return (
        "mcpServers:\n"
        f"  {_SERVER_NAME}:\n"
        f"    command: {quoted_launcher}\n"
        "    args:\n"
        f"      - {_SERVE_ARGUMENT}\n"
    )


def _render_stdio_fields(launcher: str) -> str:
    return f"Komut: {launcher}\nBağımsız değişkenler: {_SERVE_ARGUMENT}\n"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
