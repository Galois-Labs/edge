"""Sweep MCP tools: start_sweep, get_sweep_status, stop_sweep.

Sibling pattern (SEP-1686 Tasks aren't in the SDK yet). Sweep state is
held in MCP-server scope; the daemon-resident sweep continues even if
the agent disconnects.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..context import EdgeContext

logger = logging.getLogger(__name__)


@dataclass
class _SweepRecord:
    sweep_id: str
    instrument_id: str
    command_name: str
    target_value: float
    sweep_rate: float
    status: str = "running"
    current_value: float = 0.0
    error: str = ""
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None


class SweepRegistry:
    """In-process sweep tracker shared across the three sweep tools."""

    def __init__(self) -> None:
        self._records: Dict[str, _SweepRecord] = {}
        self._sweeping: set[str] = set()

    def register(self, record: _SweepRecord) -> None:
        self._records[record.sweep_id] = record
        self._sweeping.add(record.instrument_id)

    def get(self, sweep_id: str) -> Optional[_SweepRecord]:
        return self._records.get(sweep_id)

    def is_sweeping(self, instrument_id: str) -> bool:
        return instrument_id in self._sweeping

    def release(self, record: _SweepRecord) -> None:
        self._sweeping.discard(record.instrument_id)


def register_sweep_tools(mcp: FastMCP, ctx: EdgeContext) -> None:
    """Register start_sweep, get_sweep_status, and stop_sweep."""
    registry: SweepRegistry = (
        ctx.sweep_state if isinstance(ctx.sweep_state, SweepRegistry) else SweepRegistry()
    )
    ctx.sweep_state = registry

    @mcp.tool(
        name="start_sweep",
        description=(
            "Begin a long-running, profile-defined sweep on an "
            "instrument. Returns {sweep_id, status='running'}. The "
            "sweep continues if you disconnect; use get_sweep_status "
            "to poll and stop_sweep to abort or hold."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def start_sweep(
        instrument_id: str,
        command_name: str,
        target_value: float,
        sweep_rate: float,
        parameters: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        cap_mgr = ctx.capability_manager
        caps = cap_mgr.get_instrument_caps(instrument_id)
        if caps is None:
            return {
                "sweep_id": "",
                "status": "error",
                "error": f"Instrument not found: {instrument_id}",
            }

        cmd = caps.get_command(command_name)
        if cmd is None or cmd.sweep is None:
            return {
                "sweep_id": "",
                "status": "error",
                "error": (
                    f"Command '{command_name}' has no sweep configuration"
                ),
            }

        if registry.is_sweeping(instrument_id):
            return {
                "sweep_id": "",
                "status": "error",
                "error": (
                    f"Instrument '{instrument_id}' is already sweeping"
                ),
            }

        sweep_id = (
            f"{instrument_id}:{command_name}:{uuid.uuid4().hex[:8]}"
        )
        record = _SweepRecord(
            sweep_id=sweep_id,
            instrument_id=instrument_id,
            command_name=command_name,
            target_value=target_value,
            sweep_rate=sweep_rate,
        )
        registry.register(record)

        sweep_cfg = cmd.sweep
        params: Dict[str, str] = {
            "value": str(target_value),
            "sweep_rate": str(sweep_rate),
        }
        if parameters:
            params.update(parameters)
        sweep_scpi = sweep_cfg.command
        for k, v in params.items():
            sweep_scpi = sweep_scpi.replace(f"{{{k}}}", v)

        timeout_ms = (
            caps.profile.settings.timeout_ms if caps.profile else 5000
        )

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: ctx.command_handler.execute_command(
                    scpi_cmd=sweep_scpi,
                    instrument_id=instrument_id,
                    timeout_ms=timeout_ms,
                ),
            )
        except Exception as exc:
            record.status = "error"
            record.error = str(exc)
            registry.release(record)
            return {
                "sweep_id": sweep_id,
                "status": "error",
                "error": str(exc),
            }

        record.task = asyncio.create_task(
            _sweep_poll_loop(ctx, record, sweep_cfg, timeout_ms, registry)
        )

        return {"sweep_id": sweep_id, "status": "running"}

    @mcp.tool(
        name="get_sweep_status",
        description=(
            "Return the current state of a sweep: status (running, "
            "completed, aborted, error, not_found), current_value, "
            "target_value, and error message if any."
        ),
    )
    async def get_sweep_status(sweep_id: str) -> Dict[str, Any]:
        record = registry.get(sweep_id)
        if record is None:
            return {
                "sweep_id": sweep_id,
                "status": "not_found",
                "error": "Sweep not found",
            }
        return {
            "sweep_id": sweep_id,
            "status": record.status,
            "current_value": record.current_value,
            "target_value": record.target_value,
            "sweep_rate": record.sweep_rate,
            "error": record.error,
        }

    @mcp.tool(
        name="stop_sweep",
        description=(
            "Abort or hold an active sweep. Set hold=True to leave the "
            "instrument at its current setpoint instead of issuing the "
            "profile's stop command."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    async def stop_sweep(
        sweep_id: str,
        hold: bool = False,
    ) -> Dict[str, Any]:
        record = registry.get(sweep_id)
        if record is None:
            return {
                "sweep_id": sweep_id,
                "status": "not_found",
                "error": "Sweep not found",
            }
        record.cancel_event.set()
        if record.task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(record.task),
                    timeout=5.0,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        return {
            "sweep_id": sweep_id,
            "status": "holding" if hold else record.status,
        }


async def _sweep_poll_loop(
    ctx: EdgeContext,
    record: _SweepRecord,
    sweep_cfg: Any,
    timeout_ms: int,
    registry: SweepRegistry,
) -> None:
    """Poll until the sweep completes, errors, or is cancelled."""
    poll_interval = max(0.05, sweep_cfg.poll_interval_ms / 1000.0)
    loop = asyncio.get_running_loop()

    try:
        while True:
            if record.cancel_event.is_set():
                if sweep_cfg.stop_command:
                    try:
                        await loop.run_in_executor(
                            None,
                            lambda: ctx.command_handler.execute_command(
                                scpi_cmd=sweep_cfg.stop_command,
                                instrument_id=record.instrument_id,
                                timeout_ms=timeout_ms,
                            ),
                        )
                    except Exception:
                        pass
                record.status = "aborted"
                return

            try:
                result = await loop.run_in_executor(
                    None,
                    lambda: ctx.command_handler.execute_command(
                        scpi_cmd=sweep_cfg.check_command,
                        instrument_id=record.instrument_id,
                        force_query=True,
                        timeout_ms=timeout_ms,
                    ),
                )
                response_str = (
                    result.get("response", "")
                    if isinstance(result, dict)
                    else str(result)
                )
                if sweep_cfg.check_idle_match and re.search(
                    sweep_cfg.check_idle_match, response_str
                ):
                    record.status = "completed"
                    return
            except Exception as exc:
                record.status = "error"
                record.error = str(exc)
                return

            await asyncio.sleep(poll_interval)
    finally:
        registry.release(record)
