"""ParameterConfig -> JSON Schema mapping for MCP tool input schemas.

Phase 1 covers the mechanical mapping documented in docs/mcp-integration.md
section 2.4. CommandConfig.is_dangerous maps to MCP ToolAnnotations
(handled in tools/execute.py); streamable / requires_sweep go into the
tool description so an agent doesn't try to call them via execute_command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from ..profile_schema import CommandConfig, ParameterConfig


_TYPE_TO_JSON: Dict[str, str] = {
    "float": "number",
    "int": "integer",
    "string": "string",
    "bool": "boolean",
    "enum": "string",
}


def parameter_to_json_schema(param: "ParameterConfig") -> Dict[str, Any]:
    """Project a single ParameterConfig into a JSON Schema fragment."""
    param.validate()

    schema: Dict[str, Any] = {}

    if param.type == "enum":
        schema["type"] = "string"
        schema["enum"] = list(param.options or [])
    else:
        json_type = _TYPE_TO_JSON.get(param.type)
        if json_type is None:
            raise ValueError(f"Unsupported parameter type: {param.type}")
        schema["type"] = json_type

    if param.map:
        # Forward-map only: agents see human-friendly labels; the daemon
        # substitutes the wire value at SCPI-format time.
        schema["enum"] = list(param.map.keys())
        schema["type"] = "string"

    if param.min is not None and param.type in ("float", "int"):
        schema["minimum"] = param.min
    if param.max is not None and param.type in ("float", "int"):
        schema["maximum"] = param.max

    if param.default is not None:
        schema["default"] = param.default

    description_parts: List[str] = []
    if param.description:
        description_parts.append(param.description)
    if param.unit:
        description_parts.append(f"(unit: {param.unit})")
    if description_parts:
        schema["description"] = " ".join(description_parts)

    return schema


def command_to_input_schema(cmd: "CommandConfig") -> Dict[str, Any]:
    """Build a JSON Schema object for a CommandConfig's parameters."""
    properties: Dict[str, Any] = {}
    required: List[str] = []

    if cmd.params:
        for name, pc in cmd.params.items():
            properties[name] = parameter_to_json_schema(pc)
            if pc.default is None:
                required.append(name)

    schema: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema
