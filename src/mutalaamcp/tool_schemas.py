"""Contract-backed standalone JSON Schema exports for the V1 tools."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

SchemaKind = Literal["input", "output"]
_CONTRACT_RESOURCE = "contracts/tool-surface-v1.json"


@lru_cache(maxsize=1)
def load_tool_surface_contract() -> Mapping[str, Any]:
    """Load the wheel resource, with a source-checkout fallback for development."""
    package = resources.files("mutalaamcp")
    resource = package.joinpath(_CONTRACT_RESOURCE)
    if not resource.is_file():
        resource = package.joinpath("..", "..", "contracts", "tool-surface-v1.json")
    payload = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("Araç yüzeyi sözleşmesi bir JSON nesnesi olmalıdır.")
    return payload


def v1_tool_contract(name: str) -> Mapping[str, Any]:
    """Return the authoritative contract entry for one named V1 tool."""
    contract = load_tool_surface_contract()
    if "tools" not in contract:
        raise ValueError("Araç yüzeyi sözleşmesi bir tools dizisi içermelidir.")
    tools = contract["tools"]
    if not isinstance(tools, list):
        raise TypeError("Araç yüzeyi sözleşmesindeki tools bir JSON dizisi olmalıdır.")
    for tool in tools:
        if isinstance(tool, dict) and tool.get("name") == name:
            return tool
    raise ValueError(f"Bilinmeyen V1 aracı: {name}")


def standalone_tool_schema(name: str, kind: SchemaKind) -> dict[str, Any]:
    """Export one contract schema with precisely its transitive catalog ``$defs``.

    Definition traversal is depth-first in JSON insertion order.  A definition is
    copied only at its first reference, preserving the contract's deterministic
    first-seen ordering while making its original ``#/$defs/<Name>`` references
    resolve inside this standalone document.
    """
    if kind not in {"input", "output"}:
        raise ValueError("Tür, 'input' veya 'output' olmalıdır.")

    contract = load_tool_surface_contract()
    tool = v1_tool_contract(name)
    root = deepcopy(_schema_object(tool, f"{kind}_schema"))
    catalog = _defs_object(contract)
    definitions: dict[str, Any] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "$ref" and isinstance(child, str):
                    definition_name = _catalog_ref_name(child)
                    if (
                        definition_name is not None
                        and definition_name not in definitions
                    ):
                        try:
                            definition = catalog[definition_name]
                        except KeyError as exc:
                            raise ValueError(
                                f"Sözleşme başvurusu bir katalog tanımı belirtmiyor: {child}"
                            ) from exc
                        copied = deepcopy(definition)
                        definitions[definition_name] = copied
                        visit(copied)
                visit(child)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(root)
    export = dict(root)
    if "schema_export" not in contract:
        raise ValueError("Araç yüzeyi sözleşmesinde schema_export.dialect eksik.")
    schema_export = contract["schema_export"]
    if not isinstance(schema_export, dict):
        raise TypeError(
            "Araç yüzeyi sözleşmesindeki schema_export bir JSON nesnesi olmalıdır."
        )
    if "dialect" not in schema_export:
        raise ValueError("Araç yüzeyi sözleşmesinde schema_export.dialect eksik.")
    dialect = schema_export["dialect"]
    if not isinstance(dialect, str):
        raise TypeError(
            "Araç yüzeyi sözleşmesindeki schema_export.dialect bir dize olmalıdır."
        )
    export["$schema"] = dialect
    export["$id"] = f"mutalaamcp:tool-surface:v1:{name}:{kind}"
    export["$defs"] = definitions
    if kind == "output":
        export.setdefault("type", "object")
    return export


def fastmcp_output_schema(name: str) -> dict[str, Any]:
    """Return the standalone output schema accepted by FastMCP."""
    return standalone_tool_schema(name, "output")


def _schema_object(tool: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    if key not in tool:
        raise ValueError(f"Araç sözleşmesinde {key} nesnesi eksik.")
    schema = tool[key]
    if not isinstance(schema, dict):
        raise TypeError(f"Araç sözleşmesindeki {key} bir JSON nesnesi olmalıdır.")
    return schema


def _defs_object(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    if "$defs" not in contract:
        raise ValueError("Araç yüzeyi sözleşmesinde $defs eksik.")
    definitions = contract["$defs"]
    if not isinstance(definitions, dict):
        raise TypeError("Araç yüzeyi sözleşmesindeki $defs bir JSON nesnesi olmalıdır.")
    return definitions


def _catalog_ref_name(value: str) -> str | None:
    prefix = "#/$defs/"
    if not value.startswith(prefix):
        return None
    name = value.removeprefix(prefix)
    return name or None
