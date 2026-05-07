"""Per-SDK typed MCP tool registration (Phase 3 §4.4).

For each module under ``galois_edge.sdk_wrappers`` that declares a
module-level ``MCP_TOOL_SPECS`` constant we emit one MCP tool per spec
entry. Each emitted tool's name is ``<wrapper_stem>__<spec_name>`` —
e.g. ``dps150_wrapper__set_voltage``. Calling the tool routes through
:meth:`SDKExecutor.call_method` so the agent never sees the opaque
``ProxySDKCall`` primitive.

Type validation: the JSON schema declared in ``MCP_TOOL_SPECS`` is
enforced *before* any SDK dispatch. Out-of-range numeric arguments raise
``ValueError`` from the tool handler, which FastMCP surfaces as a
JSON-RPC error to the caller.

Dangerous methods: each spec entry's ``is_dangerous`` flag drives both
the MCP ``destructiveHint`` annotation and the JWT ``danger_allow``
gate via ``EdgeContext.authorize``.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from mcp.types import ToolAnnotations

if TYPE_CHECKING:
    from ..sdk_executor import SDKExecutor
    from .dynamic_tools import DynamicToolRegistry

logger = logging.getLogger(__name__)


_TYPE_TO_JSON: Dict[str, str] = {
    "float": "number",
    "number": "number",
    "int": "integer",
    "integer": "integer",
    "string": "string",
    "str": "string",
    "bool": "boolean",
    "boolean": "boolean",
}


def register_sdk_typed_tools(
    registry: "DynamicToolRegistry",
    executor: "SDKExecutor",
) -> int:
    """Walk sdk_wrappers and register one MCP tool per MCP_TOOL_SPECS entry."""

    pkg_name = "galois_edge.sdk_wrappers"
    try:
        pkg = importlib.import_module(pkg_name)
    except ImportError:
        logger.warning("SDK wrappers package not importable: %s", pkg_name)
        return 0

    count = 0
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        if mod_info.ispkg:
            continue
        full_name = f"{pkg_name}.{mod_info.name}"
        try:
            module = importlib.import_module(full_name)
        except Exception as exc:
            logger.debug("skipping %s: import failed (%s)", full_name, exc)
            continue
        specs = getattr(module, "MCP_TOOL_SPECS", None)
        if not specs:
            continue
        wrapper_stem = mod_info.name
        for spec in specs:
            try:
                _register_one(registry, executor, wrapper_stem, spec)
                count += 1
            except Exception:
                logger.exception(
                    "failed to register SDK tool %s.%s",
                    wrapper_stem,
                    spec.get("name", "?"),
                )
    logger.info("Registered %d SDK-typed MCP tools", count)
    return count


def _register_one(
    registry: "DynamicToolRegistry",
    executor: "SDKExecutor",
    wrapper_stem: str,
    spec: Dict[str, Any],
) -> None:
    spec_name = spec["name"]
    method_name = spec.get("method", spec_name)
    tool_name = f"{wrapper_stem}__{spec_name}"
    description = spec.get("description", f"Run {wrapper_stem}.{method_name}")
    is_dangerous = bool(spec.get("is_dangerous", False))
    params: Dict[str, Dict[str, Any]] = spec.get("params", {})

    properties: Dict[str, Any] = {}
    required: List[str] = []
    for pname, pdecl in params.items():
        properties[pname] = _param_to_jsonschema(pdecl)
        if pdecl.get("default") is None and not pdecl.get("optional"):
            required.append(pname)

    handler = _make_sdk_handler(
        executor=executor,
        registry=registry,
        tool_name=tool_name,
        method_name=method_name,
        properties=properties,
        is_dangerous=is_dangerous,
    )

    schema_for_signature = {"properties": properties, "required": required}
    from .dynamic_tools import _make_signature_wrapper

    wrapped = _make_signature_wrapper(handler, schema_for_signature, tool_name)
    wrapped.__doc__ = description

    annotations = ToolAnnotations(destructiveHint=is_dangerous)
    registry._mcp.add_tool(  # noqa: SLF001 — registry holds the FastMCP
        wrapped,
        name=tool_name,
        description=description,
        annotations=annotations,
    )


def _param_to_jsonschema(decl: Dict[str, Any]) -> Dict[str, Any]:
    raw_type = str(decl.get("type", "string"))
    schema: Dict[str, Any] = {"type": _TYPE_TO_JSON.get(raw_type, raw_type)}
    if "description" in decl:
        desc = decl["description"]
        if "unit" in decl:
            desc = f"{desc} (unit: {decl['unit']})"
        schema["description"] = desc
    elif "unit" in decl:
        schema["description"] = f"unit: {decl['unit']}"
    if "minimum" in decl:
        schema["minimum"] = decl["minimum"]
    if "maximum" in decl:
        schema["maximum"] = decl["maximum"]
    if "enum" in decl:
        schema["enum"] = list(decl["enum"])
    if "default" in decl and decl["default"] is not None:
        schema["default"] = decl["default"]
    return schema


def _make_sdk_handler(
    executor: "SDKExecutor",
    registry: "DynamicToolRegistry",
    tool_name: str,
    method_name: str,
    properties: Dict[str, Any],
    is_dangerous: bool,
):
    ctx = registry._ctx  # noqa: SLF001

    async def _execute(**kwargs: Any) -> Dict[str, Any]:
        ctx.authorize(
            tool_name=tool_name,
            scope=method_name,
            is_dangerous=is_dangerous,
        )
        # Range / enum validation prior to dispatch.
        for pname, pdef in properties.items():
            if pname not in kwargs:
                continue
            value = kwargs[pname]
            if isinstance(value, (int, float)):
                if pdef.get("minimum") is not None and value < pdef["minimum"]:
                    raise ValueError(
                        f"{pname}={value} below minimum {pdef['minimum']}"
                    )
                if pdef.get("maximum") is not None and value > pdef["maximum"]:
                    raise ValueError(
                        f"{pname}={value} above maximum {pdef['maximum']}"
                    )
            if pdef.get("enum") is not None and value not in pdef["enum"]:
                raise ValueError(
                    f"{pname}={value!r} not in {pdef['enum']}"
                )

        instrument_id = kwargs.pop("instrument_id", None)
        if not instrument_id:
            return {
                "success": False,
                "error": "instrument_id is required",
                "response": "",
                "execution_time_ms": 0,
            }
        loop = asyncio.get_running_loop()
        start = time.time()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: executor.call_method(
                    instrument_id=instrument_id,
                    method_name=method_name,
                    params=kwargs or None,
                ),
            )
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "response": "",
                "execution_time_ms": int((time.time() - start) * 1000),
            }
        return result

    _execute.__name__ = tool_name
    return _execute
