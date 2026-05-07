"""Dynamic per-instrument MCP tool registry.

Subscribes to ``CapabilityManager`` register/unregister events and adds
or removes one MCP tool per (profile_key, command_name) pair (and per
sequence) from the FastMCP ToolManager. After each mutation it broadcasts
``notifications/tools/list_changed`` to every active streamable-HTTP
session so connected MCP clients re-fetch their tool surface.

Tool naming:
    - First instance of a profile:    ``<profile_key>__<command_name>``
    - Same profile, multiple instances: ``<profile_key>__<short_id>__<command_name>``
      where ``short_id`` is the last 8 chars of ``instrument_id`` with
      MCP-illegal characters (anything outside ``A-Za-z0-9_-.``) replaced
      by underscore.
    - Sequences: ``__sequence__`` infix between profile_key and sequence_name.

When a second instance of the same profile registers, the first instance's
already-emitted tools are removed and re-emitted with the disambiguating
suffix so the agent never sees a stable bare name competing with a
suffixed one.

See docs/mcp-integration.md section 4.2.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage
from mcp.types import ToolAnnotations

from .context import EdgeContext
from .schema import command_to_input_schema, parameter_to_json_schema

if TYPE_CHECKING:
    from ..capability_manager import CapabilityManager, InstrumentCapabilities
    from ..command_handler import CommandHandler
    from ..profile_schema import CommandConfig, SequenceConfig

logger = logging.getLogger(__name__)


# Substring used to disambiguate sequences from commands in the generated
# tool name. Matches the spec in §4.2.3.
SEQUENCE_INFIX = "__sequence__"

# MCP tool names are restricted to [A-Za-z0-9_.-] per SEP-986. Sanitize
# the suffix derived from instrument_id (which can contain '::', '/' etc.).
_TOOL_NAME_CLEAN = re.compile(r"[^A-Za-z0-9_.-]")


def _sanitize_short_id(instrument_id: str) -> str:
    """Last 8 chars of instrument_id with MCP-illegal characters scrubbed."""
    tail = instrument_id[-8:] if len(instrument_id) > 8 else instrument_id
    return _TOOL_NAME_CLEAN.sub("_", tail) or "x"


class DynamicToolRegistry:
    """Tracks per-instrument MCP tools and keeps FastMCP in sync."""

    def __init__(
        self,
        mcp: FastMCP,
        ctx: EdgeContext,
        emit_list_changed: bool = True,
    ) -> None:
        self._mcp = mcp
        self._ctx = ctx
        self._caps = ctx.capability_manager
        self._handler = ctx.command_handler
        self._emit_list_changed = emit_list_changed

        # instrument_id -> [tool_name, ...]
        self._registered: Dict[str, List[str]] = {}
        # tool_name -> instrument_id (for collision detection / cleanup)
        self._tool_owner: Dict[str, str] = {}

        # Subscribe last so a register_instrument that happens at exactly
        # the same time can't deliver events before _registered exists.
        self._listener = self._on_capability_change
        self._caps.add_listener(self._listener)

        # Snapshot any instruments registered before we attached.
        for inst_id in list(self._caps.all_instruments.keys()):
            self._add_tools_for(inst_id)

    # -- public lifecycle ----------------------------------------------------

    def detach(self) -> None:
        """Detach from CapabilityManager and forget tracked tools.

        Used by MCPServer.stop() and tests that want to clean up between
        runs without restarting the FastMCP host.
        """
        self._caps.remove_listener(self._listener)
        for tool_name in list(self._tool_owner.keys()):
            self._safe_remove(tool_name)
        self._registered.clear()
        self._tool_owner.clear()

    def registered_tools(self) -> Dict[str, List[str]]:
        """Snapshot of (instrument_id -> tool names) for tests / introspection."""
        return {k: list(v) for k, v in self._registered.items()}

    # -- listener ------------------------------------------------------------

    def _on_capability_change(self, event: str, instrument_id: str) -> None:
        if event == "registered":
            self._add_tools_for(instrument_id)
        elif event == "unregistered":
            self._remove_tools_for(instrument_id)
        else:
            logger.debug("ignoring unknown capability event: %s", event)

    # -- mutation ------------------------------------------------------------

    def _add_tools_for(self, instrument_id: str) -> None:
        # Already-registered? Could happen during the constructor's
        # snapshot pass when an earlier sibling's reregister-disambiguated
        # path beat us to it.
        if self._registered.get(instrument_id):
            return

        caps = self._caps.get_instrument_caps(instrument_id)
        if caps is None or caps.profile is None:
            # Profile-less instruments still get the static execute_command
            # surface; we can't generate typed tools without a profile.
            self._registered.setdefault(instrument_id, [])
            return

        profile = caps.profile
        # If this is the second-or-later instance of the same profile,
        # rewrite the prior siblings' tool names to the disambiguated form
        # before adding ours, so the agent never observes a bare name
        # competing with a suffixed one.
        siblings = self._instances_with_profile(profile.profile_key)
        # Drop ourselves from the sibling list (we're not yet registered)
        siblings = [iid for iid in siblings if iid != instrument_id]

        if siblings:
            for sibling_id in siblings:
                if self._is_disambiguated(sibling_id):
                    continue
                self._reregister_disambiguated(sibling_id)

        disambiguate = bool(siblings)
        short_id = _sanitize_short_id(instrument_id)
        added = self._build_tools_for(
            instrument_id=instrument_id,
            profile=profile,
            short_id=short_id,
            disambiguate=disambiguate,
        )

        if added and self._emit_list_changed:
            self._broadcast_list_changed()

    def _build_tools_for(
        self,
        instrument_id: str,
        profile,
        short_id: str,
        disambiguate: bool,
    ) -> List[str]:
        added: List[str] = []
        try:
            for cmd_name, cmd_cfg in profile.commands.items():
                if not cmd_cfg.enabled:
                    continue
                tool_name = self._command_tool_name(
                    profile.profile_key, cmd_name, short_id, disambiguate,
                )
                if self._add_command_tool(
                    tool_name=tool_name,
                    instrument_id=instrument_id,
                    command_name=cmd_name,
                    command=cmd_cfg,
                ):
                    added.append(tool_name)

            sequences = profile.sequences or {}
            for seq_name, seq_cfg in sequences.items():
                if not seq_cfg.enabled:
                    continue
                tool_name = self._sequence_tool_name(
                    profile.profile_key, seq_name, short_id, disambiguate,
                )
                if self._add_sequence_tool(
                    tool_name=tool_name,
                    instrument_id=instrument_id,
                    sequence_name=seq_name,
                    sequence=seq_cfg,
                ):
                    added.append(tool_name)
        except Exception:
            logger.exception("dynamic tool registration failed for %s", instrument_id)
            for tool_name in added:
                self._safe_remove(tool_name)
            self._registered[instrument_id] = []
            raise

        self._registered[instrument_id] = added
        for tool_name in added:
            self._tool_owner[tool_name] = instrument_id
        return added

    def _instances_with_profile(self, profile_key: str) -> List[str]:
        return [
            iid for iid, c in self._caps.all_instruments.items()
            if c.profile is not None and c.profile.profile_key == profile_key
        ]

    def _is_disambiguated(self, instrument_id: str) -> bool:
        names = self._registered.get(instrument_id, [])
        if not names:
            return False
        short = _sanitize_short_id(instrument_id)
        return any(f"__{short}__" in n for n in names) or any(
            f"__{short}{SEQUENCE_INFIX}" in n for n in names
        )

    def _reregister_disambiguated(self, instrument_id: str) -> None:
        """Remove the bare-name tools for an instrument and re-add with suffix."""
        caps = self._caps.get_instrument_caps(instrument_id)
        if caps is None or caps.profile is None:
            return
        # Drop existing tools
        for tool_name in self._registered.get(instrument_id, []):
            self._safe_remove(tool_name)
            self._tool_owner.pop(tool_name, None)
        self._registered[instrument_id] = []
        short_id = _sanitize_short_id(instrument_id)
        self._build_tools_for(
            instrument_id=instrument_id,
            profile=caps.profile,
            short_id=short_id,
            disambiguate=True,
        )

    def _remove_tools_for(self, instrument_id: str) -> None:
        names = self._registered.pop(instrument_id, None)
        if not names:
            return
        for tool_name in names:
            self._safe_remove(tool_name)
            self._tool_owner.pop(tool_name, None)
        if self._emit_list_changed:
            self._broadcast_list_changed()

    def _safe_remove(self, tool_name: str) -> None:
        try:
            self._mcp.remove_tool(tool_name)
        except Exception:
            # Tool may already be gone (e.g. registry detached after FastMCP
            # cleared its tools). Don't propagate.
            logger.debug("remove_tool(%s) raised — already gone?", tool_name)

    # -- naming --------------------------------------------------------------

    def _needs_disambiguation(self, profile_key: str) -> bool:
        """True iff more than one connected instrument shares this profile."""
        count = 0
        for caps in self._caps.all_instruments.values():
            if caps.profile is not None and caps.profile.profile_key == profile_key:
                count += 1
                if count > 1:
                    return True
        return False

    @staticmethod
    def _command_tool_name(
        profile_key: str,
        command_name: str,
        short_id: str,
        disambiguate: bool,
    ) -> str:
        if disambiguate:
            return f"{profile_key}__{short_id}__{command_name}"
        return f"{profile_key}__{command_name}"

    @staticmethod
    def _sequence_tool_name(
        profile_key: str,
        sequence_name: str,
        short_id: str,
        disambiguate: bool,
    ) -> str:
        if disambiguate:
            return (
                f"{profile_key}__{short_id}{SEQUENCE_INFIX}{sequence_name}"
            )
        return f"{profile_key}{SEQUENCE_INFIX}{sequence_name}"

    # -- tool factories ------------------------------------------------------

    def _add_command_tool(
        self,
        tool_name: str,
        instrument_id: str,
        command_name: str,
        command: "CommandConfig",
    ) -> bool:
        try:
            input_schema = command_to_input_schema(command)
        except Exception:
            logger.exception(
                "skipping %s: failed to build input schema",
                tool_name,
            )
            return False

        description = self._command_description(command_name, command)

        annotations = ToolAnnotations(
            destructiveHint=bool(command.is_dangerous),
        )

        handler = self._make_command_handler(
            instrument_id=instrument_id,
            command_name=command_name,
            command=command,
            tool_name=tool_name,
            input_schema=input_schema,
        )

        self._mcp.add_tool(
            handler,
            name=tool_name,
            description=description,
            annotations=annotations,
        )
        return True

    def _add_sequence_tool(
        self,
        tool_name: str,
        instrument_id: str,
        sequence_name: str,
        sequence: "SequenceConfig",
    ) -> bool:
        params_schema: Dict[str, Any] = {"type": "object", "properties": {}}
        if sequence.parameters:
            properties: Dict[str, Any] = {}
            required: List[str] = []
            for pname, pc in sequence.parameters.items():
                try:
                    properties[pname] = parameter_to_json_schema(pc)
                except Exception:
                    properties[pname] = {"type": "string"}
                if pc.default is None:
                    required.append(pname)
            params_schema["properties"] = properties
            if required:
                params_schema["required"] = required

        description = (
            sequence.description or f"Run sequence '{sequence_name}'"
        )
        description = (
            f"{description}\nProfile sequence; multi-step. "
            f"Maps to execute_sequence({instrument_id}, {sequence_name})."
        )

        annotations = ToolAnnotations(destructiveHint=True)

        handler = self._make_sequence_handler(
            instrument_id=instrument_id,
            sequence_name=sequence_name,
            tool_name=tool_name,
            params_schema=params_schema,
        )

        self._mcp.add_tool(
            handler,
            name=tool_name,
            description=description,
            annotations=annotations,
        )
        return True

    @staticmethod
    def _command_description(command_name: str, cmd: "CommandConfig") -> str:
        head = cmd.description or f"Run profile command '{command_name}'"
        notes: List[str] = []
        if cmd.is_dangerous:
            notes.append("destructive")
        if cmd.streamable:
            notes.append("streamable — prefer start_stream for periodic reads")
        if cmd.requires_sweep:
            notes.append("requires_sweep — use start_sweep instead")
        if notes:
            return f"{head} ({', '.join(notes)})"
        return head

    # -- handler builders ----------------------------------------------------

    def _make_command_handler(
        self,
        instrument_id: str,
        command_name: str,
        command: "CommandConfig",
        tool_name: str,
        input_schema: Dict[str, Any],
    ) -> Callable[..., Any]:
        ctx = self._ctx
        properties = list(input_schema.get("properties", {}).keys())

        async def _impl(**kwargs: Any) -> Dict[str, Any]:
            ctx.authorize(
                tool_name="execute_command",
                scope=tool_name,
                is_dangerous=bool(command.is_dangerous),
            )
            # Validate against the JSON schema we built. FastMCP/Pydantic
            # already validates types at the framework boundary; this
            # double-check covers numeric range constraints which Pydantic
            # surfaces only when we declare an explicit BaseModel.
            for pname, pdef in input_schema.get("properties", {}).items():
                if pname not in kwargs:
                    continue
                value = kwargs[pname]
                if isinstance(value, (int, float)):
                    minimum = pdef.get("minimum")
                    maximum = pdef.get("maximum")
                    if minimum is not None and value < minimum:
                        raise ValueError(
                            f"{pname}={value} below minimum {minimum}"
                        )
                    if maximum is not None and value > maximum:
                        raise ValueError(
                            f"{pname}={value} above maximum {maximum}"
                        )
                if pdef.get("enum") is not None:
                    if value not in pdef["enum"]:
                        raise ValueError(
                            f"{pname}={value!r} not in {pdef['enum']}"
                        )

            if command.requires_sweep:
                return _error(
                    f"Command '{command_name}' requires sweep — use start_sweep.",
                )

            params = {k: str(v) for k, v in kwargs.items() if k in properties}
            cap_mgr = ctx.capability_manager
            caps = cap_mgr.get_instrument_caps(instrument_id)
            if caps is None:
                return _error(f"Instrument disappeared: {instrument_id}")

            is_query = command.type == "query" or command.force_query

            dispatch = cap_mgr.resolve_command(
                instrument_id=instrument_id,
                command_name=command_name,
                params=params or None,
                is_query=is_query,
            )
            if dispatch is None:
                return _error(
                    f"Failed to resolve command '{command_name}' for {instrument_id}"
                )

            if not isinstance(dispatch, str):
                return _error(
                    f"Command '{command_name}' is SDK-backed; call the per-SDK tool."
                )

            timeout_ms = (
                caps.profile.settings.timeout_ms
                if caps.profile is not None else 5000
            )
            start = time.time()
            loop = asyncio.get_running_loop()
            try:
                result = await loop.run_in_executor(
                    None,
                    lambda: ctx.command_handler.execute_command(
                        scpi_cmd=dispatch,
                        instrument_id=instrument_id,
                        timeout_ms=timeout_ms,
                        force_query=is_query,
                    ),
                )
            except Exception as exc:
                logger.exception("dynamic tool dispatch raised: %s", tool_name)
                return _error(str(exc), scpi=dispatch, start=start)

            elapsed_ms = int((time.time() - start) * 1000)
            response = result.get("response", "")
            if command.returns and result.get("success"):
                response = command.returns.parse_response(response)

            return {
                "success": bool(result.get("success", False)),
                "data": response,
                "error": result.get("error", ""),
                "scpi_command": dispatch,
                "execution_time_ms": elapsed_ms,
            }

        # FastMCP's Tool.from_function inspects the actual function
        # signature (not `**kwargs`) to build the JSON Schema. Build a
        # wrapper with concrete parameters so min/max/enum metadata
        # surfaces correctly in tools/list.
        wrapper = _make_signature_wrapper(_impl, input_schema, tool_name)
        wrapper.__doc__ = self._command_description(command_name, command)
        return wrapper

    def _make_sequence_handler(
        self,
        instrument_id: str,
        sequence_name: str,
        tool_name: str,
        params_schema: Dict[str, Any],
    ) -> Callable[..., Any]:
        ctx = self._ctx

        async def _impl(**kwargs: Any) -> Dict[str, Any]:
            ctx.authorize(
                tool_name="execute_sequence",
                scope=tool_name,
                is_dangerous=True,
            )
            for pname, pdef in params_schema.get("properties", {}).items():
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
            params = {k: str(v) for k, v in kwargs.items()}
            # Reuse the static execute_sequence tool's logic by importing
            # it lazily — keeps a single dispatch path for sequence runs.
            from .tools.execute import register_execute_tools  # noqa: F401
            # Direct call into the EdgeContext-bound capability manager
            cap_mgr = ctx.capability_manager
            caps = cap_mgr.get_instrument_caps(instrument_id)
            if caps is None:
                return _error(f"Instrument disappeared: {instrument_id}")
            seq = caps.get_sequence(sequence_name)
            if seq is None:
                return _error(f"Sequence '{sequence_name}' disabled or removed")

            start = time.time()
            captured: Dict[str, str] = {}
            steps_executed: List[str] = []
            timeout_ms = (
                caps.profile.settings.timeout_ms if caps.profile else 30000
            )
            step_count = max(1, len(seq.steps))
            per_step_timeout = max(1000, timeout_ms // step_count)
            loop = asyncio.get_running_loop()

            try:
                for step in seq.steps:
                    if step.command:
                        cmd = caps.get_command(step.command)
                        if cmd is None:
                            raise ValueError(
                                f"Sequence step references unknown command '{step.command}'"
                            )
                        step_args: Dict[str, Any] = {}
                        if step.args:
                            for k, v in step.args.items():
                                val = v
                                for pk, pv in params.items():
                                    val = val.replace(f"{{{pk}}}", str(pv))
                                for ck, cv in captured.items():
                                    val = val.replace(f"{{{ck}}}", str(cv))
                                step_args[k] = val
                        if cmd.is_sdk_command:
                            raise ValueError(
                                "SDK steps are not supported via dynamic sequence tool"
                            )
                        scpi = cmd.format_scpi(step_args or None)
                        result = await loop.run_in_executor(
                            None,
                            lambda s=scpi, t=per_step_timeout: ctx.command_handler.execute_command(
                                scpi_cmd=s,
                                instrument_id=instrument_id,
                                timeout_ms=t,
                            ),
                        )
                        steps_executed.append(scpi)
                    elif step.scpi:
                        scpi = step.scpi
                        for pk, pv in params.items():
                            scpi = scpi.replace(f"{{{pk}}}", str(pv))
                        for ck, cv in captured.items():
                            scpi = scpi.replace(f"{{{ck}}}", str(cv))
                        result = await loop.run_in_executor(
                            None,
                            lambda s=scpi, t=per_step_timeout: ctx.command_handler.execute_command(
                                scpi_cmd=s,
                                instrument_id=instrument_id,
                                timeout_ms=t,
                            ),
                        )
                        steps_executed.append(scpi)
                    else:
                        continue
                    if not result.get("success", False):
                        raise ValueError(
                            f"Step failed: {result.get('error', '')}"
                        )
                    if step.capture:
                        captured[step.capture] = result.get("response", "")
                final_result = ""
                if seq.returns and seq.returns in captured:
                    final_result = captured[seq.returns]
                return {
                    "result": final_result,
                    "status": "completed",
                    "error": "",
                    "steps_executed": steps_executed,
                    "execution_time_ms": int((time.time() - start) * 1000),
                }
            except Exception as exc:
                return {
                    "result": "",
                    "status": "error",
                    "error": str(exc),
                    "steps_executed": steps_executed,
                    "execution_time_ms": int((time.time() - start) * 1000),
                }

        wrapper = _make_signature_wrapper(_impl, params_schema, tool_name)
        return wrapper

    # -- list_changed broadcast ----------------------------------------------

    def _broadcast_list_changed(self) -> None:
        """Send notifications/tools/list_changed to every active session.

        FastMCP's ToolManager.add_tool / remove_tool do NOT auto-fire this
        notification, so we reach into the StreamableHTTP session manager
        and write directly to each transport's outbound stream. Best-effort:
        if the manager hasn't been initialized yet (no active sessions),
        the call is a no-op.
        """
        try:
            sm = self._mcp._session_manager  # type: ignore[attr-defined]
        except Exception:
            return
        if sm is None:
            return

        instances = getattr(sm, "_server_instances", None)
        if not instances:
            return

        notif = types.JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/tools/list_changed",
            params=None,
        )
        envelope = types.JSONRPCMessage(notif)
        message = SessionMessage(envelope)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Not inside an event loop (e.g. synchronous test harness).
            return

        for transport in list(instances.values()):
            stream = getattr(transport, "_write_stream", None)
            if stream is None:
                continue
            loop.create_task(_send_or_drop(stream, message))


def _error(
    message: str,
    scpi: str = "",
    start: Optional[float] = None,
) -> Dict[str, Any]:
    elapsed = 0
    if start is not None:
        elapsed = int((time.time() - start) * 1000)
    return {
        "success": False,
        "data": "",
        "error": message,
        "scpi_command": scpi,
        "execution_time_ms": elapsed,
    }


async def _send_or_drop(stream: Any, message: SessionMessage) -> None:
    try:
        await stream.send(message)
    except Exception:
        # Closed transport; nothing to do.
        logger.debug("list_changed broadcast dropped on closed stream")


# JSON-Schema "type" -> Python typing annotation used for the synthetic
# wrapper signature. FastMCP runs Pydantic over the resulting model so
# numeric bounds in the JSON schema are NOT enforced — we re-validate
# bounds inside the wrapper itself (see _make_command_handler).
_JSONSCHEMA_TO_PY_TYPE = {
    "number": float,
    "integer": int,
    "string": str,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _make_signature_wrapper(
    impl: Callable[..., Any],
    input_schema: Dict[str, Any],
    tool_name: str,
) -> Callable[..., Any]:
    """Wrap ``impl`` (a ``**kwargs``-only async fn) with a synthetic
    signature derived from ``input_schema`` so FastMCP / Pydantic can
    surface typed parameters in tools/list output.

    Numeric ``minimum`` / ``maximum`` constraints from the JSON Schema
    are propagated as ``typing.Annotated[T, pydantic.Field(ge=..., le=...)]``
    so Pydantic emits them in the model_json_schema() output that FastMCP
    forwards to the agent.
    """
    properties = input_schema.get("properties", {}) or {}
    required = set(input_schema.get("required", []) or [])

    if not properties:
        impl.__name__ = tool_name
        return impl

    import inspect
    from typing import Annotated, Literal

    from pydantic import Field

    parameters: List[inspect.Parameter] = []
    annotations: Dict[str, Any] = {}
    for pname, pdef in properties.items():
        json_type = str(pdef.get("type", "string"))
        py_type = _JSONSCHEMA_TO_PY_TYPE.get(json_type, Any)
        enum_values = pdef.get("enum")
        if enum_values is not None:
            # Use a Literal for enums — Pydantic emits the enum schema
            # back out and validates client args against the allowed set.
            try:
                py_type = Literal[tuple(enum_values)]  # type: ignore[misc]
            except TypeError:
                py_type = str
            annotated_type: Any = py_type
        else:
            field_kwargs: Dict[str, Any] = {}
            if pdef.get("minimum") is not None:
                field_kwargs["ge"] = pdef["minimum"]
            if pdef.get("maximum") is not None:
                field_kwargs["le"] = pdef["maximum"]
            if pdef.get("description"):
                field_kwargs["description"] = pdef["description"]
            if field_kwargs:
                annotated_type = Annotated[py_type, Field(**field_kwargs)]
            else:
                annotated_type = py_type

        if pname in required:
            default: Any = inspect.Parameter.empty
        else:
            default = pdef.get("default", None)
        parameters.append(
            inspect.Parameter(
                name=pname,
                kind=inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=annotated_type,
            )
        )
        annotations[pname] = annotated_type
    annotations["return"] = Dict[str, Any]

    sig = inspect.Signature(
        parameters=parameters,
        return_annotation=Dict[str, Any],
    )

    async def _wrapper(**kwargs: Any) -> Dict[str, Any]:
        return await impl(**kwargs)

    _wrapper.__name__ = tool_name
    _wrapper.__signature__ = sig  # type: ignore[attr-defined]
    _wrapper.__annotations__ = annotations
    return _wrapper
