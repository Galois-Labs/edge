"""Execute MCP tools: execute_command, execute_sequence, send_scpi.

Each dispatches in-process to CommandHandler / CapabilityManager. The
generic execute_command tool refuses commands flagged requires_sweep
(safety interlock — see grpc_server.ExecuteCommand).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..context import EdgeContext

logger = logging.getLogger(__name__)


def register_execute_tools(mcp: FastMCP, ctx: EdgeContext) -> None:
    """Register execute_command, execute_sequence, and send_scpi."""

    @mcp.tool(
        name="execute_command",
        description=(
            "Run a profile-defined named command on an instrument. "
            "Pass instrument_id (e.g. a VISA address), command_name "
            "(from get_capabilities), and any required parameters as "
            "a dict of stringified values. Set is_query=True for "
            "queries. Returns success, data, scpi_command, and "
            "execution_time_ms. Refuses commands flagged "
            "requires_sweep — use start_sweep for those."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def execute_command(
        instrument_id: str,
        command_name: str,
        parameters: Optional[Dict[str, str]] = None,
        is_query: bool = False,
    ) -> Dict[str, Any]:
        start = time.time()
        cap_mgr = ctx.capability_manager
        caps = cap_mgr.get_instrument_caps(instrument_id)
        if caps is None:
            return _error(
                f"Instrument not found: {instrument_id}",
                start,
            )

        cmd_config = caps.get_command(command_name)
        if cmd_config is None:
            return _error(
                f"Command '{command_name}' not found or disabled "
                f"for {instrument_id}",
                start,
            )

        if cmd_config.requires_sweep:
            return _error(
                f"Command '{command_name}' requires sweep for safety. "
                f"Use start_sweep with an explicit sweep_rate.",
                start,
            )

        params = dict(parameters) if parameters else None
        dispatch = cap_mgr.resolve_command(
            instrument_id=instrument_id,
            command_name=command_name,
            params=params,
            is_query=is_query,
        )
        if dispatch is None:
            return _error(
                f"Failed to resolve command '{command_name}' for "
                f"{instrument_id}",
                start,
            )

        if not isinstance(dispatch, str):
            # SDK dispatch is out of scope for the generic execute tool
            # in Phase 1; per-SDK typed tools land in Phase 3.
            return _error(
                f"Command '{command_name}' dispatches to a vendor SDK; "
                f"call it via the per-SDK tool surface (Phase 3).",
                start,
            )

        timeout_ms = (
            caps.profile.settings.timeout_ms if caps.profile else 5000
        )
        loop = asyncio.get_running_loop()

        # IEEE 488.2 definite-length block commands must go through the
        # raw byte path — the text query() path corrupts binary and
        # terminates early on 0x0A payload bytes (doc §2.1).
        returns_cfg = cmd_config.returns
        if returns_cfg is not None and getattr(
            returns_cfg, "is_ieee_block", False
        ):
            binary_config = returns_cfg.effective_binary
            preamble_scpi = None
            if binary_config.preamble_command and caps.profile is not None:
                preamble_scpi = caps.profile.resolve_scpi_ref(
                    binary_config.preamble_command, params
                )
            try:
                result = await loop.run_in_executor(
                    None,
                    lambda: ctx.command_handler.execute_binary_block_query(
                        scpi_cmd=dispatch,
                        instrument_id=instrument_id,
                        binary_config=binary_config,
                        preamble_scpi=preamble_scpi,
                        timeout_ms=timeout_ms,
                    ),
                )
            except Exception as exc:
                logger.exception("execute_command binary dispatch raised")
                return _error(str(exc), start, scpi_command=dispatch)

            elapsed_ms = int((time.time() - start) * 1000)
            if not result.get("success"):
                return _error(
                    result.get("error", "Binary block query failed"),
                    start,
                    scpi_command=dispatch,
                )

            block = result["block"]
            return {
                "success": True,
                "data": {
                    "y_data_base64": base64.b64encode(
                        block["y_data"]
                    ).decode("ascii"),
                    "y_dtype": block["y_dtype"],
                    "y_length": block["y_length"],
                    "x_start": block["x_start"],
                    "x_increment": block["x_increment"],
                    "y_scale": block["y_scale"],
                    "y_offset": block["y_offset"],
                    "x_unit": returns_cfg.x_unit or "",
                    "y_unit": returns_cfg.unit or "",
                },
                "error": "",
                "scpi_command": dispatch,
                "execution_time_ms": elapsed_ms,
            }

        try:
            result = await loop.run_in_executor(
                None,
                lambda: ctx.command_handler.execute_command(
                    scpi_cmd=dispatch,
                    instrument_id=instrument_id,
                    timeout_ms=timeout_ms,
                    force_query=is_query or cmd_config.force_query,
                ),
            )
        except Exception as exc:
            logger.exception("execute_command dispatch raised")
            return _error(str(exc), start, scpi_command=dispatch)

        elapsed_ms = int((time.time() - start) * 1000)
        if cmd_config.returns and result.get("success"):
            response = cmd_config.returns.parse_response(
                result.get("response", ""),
            )
        else:
            response = result.get("response", "")

        return {
            "success": bool(result.get("success", False)),
            "data": response,
            "error": result.get("error", ""),
            "scpi_command": dispatch,
            "execution_time_ms": elapsed_ms,
        }

    @mcp.tool(
        name="execute_sequence",
        description=(
            "Run a multi-step profile sequence. Returns the final "
            "captured value, the list of steps executed, and elapsed "
            "time. May take seconds or longer depending on the "
            "sequence."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def execute_sequence(
        instrument_id: str,
        sequence_name: str,
        parameters: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        start = time.time()
        cap_mgr = ctx.capability_manager
        caps = cap_mgr.get_instrument_caps(instrument_id)
        if caps is None:
            return {
                "result": "",
                "status": "error",
                "error": f"Instrument not found: {instrument_id}",
                "steps_executed": [],
                "execution_time_ms": int((time.time() - start) * 1000),
            }

        seq_config = caps.get_sequence(sequence_name)
        if seq_config is None:
            return {
                "result": "",
                "status": "error",
                "error": (
                    f"Sequence '{sequence_name}' not found or disabled"
                ),
                "steps_executed": [],
                "execution_time_ms": int((time.time() - start) * 1000),
            }

        params = dict(parameters) if parameters else {}
        captured_values: Dict[str, str] = {}
        steps_executed: list = []
        loop = asyncio.get_running_loop()
        timeout_ms = (
            caps.profile.settings.timeout_ms if caps.profile else 30000
        )
        step_count = max(1, len(seq_config.steps))
        per_step_timeout = max(1000, timeout_ms // step_count)

        try:
            for step in seq_config.steps:
                if step.command:
                    cmd = caps.get_command(step.command)
                    if cmd is None:
                        raise ValueError(
                            f"Command '{step.command}' not found in profile"
                        )
                    step_args: Dict[str, Any] = {}
                    if step.args:
                        for k, v in step.args.items():
                            val = v
                            for pk, pv in params.items():
                                val = val.replace(f"{{{pk}}}", str(pv))
                            for ck, cv in captured_values.items():
                                val = val.replace(f"{{{ck}}}", str(cv))
                            step_args[k] = val
                    if cmd.is_sdk_command:
                        raise ValueError(
                            "Sequences with SDK steps are not supported "
                            "from MCP in Phase 1."
                        )
                    scpi_str = cmd.format_scpi(step_args or None)
                    result = await loop.run_in_executor(
                        None,
                        lambda s=scpi_str, t=per_step_timeout: (
                            ctx.command_handler.execute_command(
                                scpi_cmd=s,
                                instrument_id=instrument_id,
                                timeout_ms=t,
                            )
                        ),
                    )
                    steps_executed.append(scpi_str)
                elif step.scpi:
                    scpi_cmd = step.scpi
                    for pk, pv in params.items():
                        scpi_cmd = scpi_cmd.replace(f"{{{pk}}}", str(pv))
                    for ck, cv in captured_values.items():
                        scpi_cmd = scpi_cmd.replace(f"{{{ck}}}", str(cv))
                    result = await loop.run_in_executor(
                        None,
                        lambda s=scpi_cmd, t=per_step_timeout: (
                            ctx.command_handler.execute_command(
                                scpi_cmd=s,
                                instrument_id=instrument_id,
                                timeout_ms=t,
                            )
                        ),
                    )
                    steps_executed.append(scpi_cmd)
                else:
                    continue

                if not result.get("success", False):
                    raise ValueError(
                        f"Step failed: {result.get('error', '')}"
                    )

                capture_key = step.capture
                if capture_key:
                    captured_values[capture_key] = result.get(
                        "response", ""
                    )

            final_result = ""
            if seq_config.returns and seq_config.returns in captured_values:
                final_result = captured_values[seq_config.returns]

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

    @mcp.tool(
        name="send_scpi",
        description=(
            "Send a raw SCPI command. Bypasses profile validation — "
            "useful for unmatched instruments or debugging only. "
            "Trailing '?' is auto-detected for queries. Returns "
            "response, error, and execution_time_ms."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def send_scpi(
        instrument_id: str,
        scpi_command: str,
    ) -> Dict[str, Any]:
        start = time.time()
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: ctx.command_handler.execute_command(
                    scpi_cmd=scpi_command,
                    instrument_id=instrument_id,
                ),
            )
        except Exception as exc:
            return {
                "response": "",
                "error": str(exc),
                "execution_time_ms": int((time.time() - start) * 1000),
            }
        return {
            "response": result.get("response", ""),
            "error": result.get("error", ""),
            "success": bool(result.get("success", False)),
            "execution_time_ms": int((time.time() - start) * 1000),
        }


def _error(
    message: str,
    start: float,
    scpi_command: str = "",
) -> Dict[str, Any]:
    return {
        "success": False,
        "data": "",
        "error": message,
        "scpi_command": scpi_command,
        "execution_time_ms": int((time.time() - start) * 1000),
    }
