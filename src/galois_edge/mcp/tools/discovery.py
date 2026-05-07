"""Discovery MCP tools: list_instruments, get_capabilities, scan_instruments,
list_profiles, get_status.

These read from CapabilityManager / InstrumentManager directly. None of
them mutate hardware state.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import socket
import time
from typing import Any, Dict, List

from mcp.server.fastmcp import FastMCP

from ..context import EdgeContext

logger = logging.getLogger(__name__)


def register_discovery_tools(mcp: FastMCP, ctx: EdgeContext) -> None:
    """Register the five Phase-1 discovery tools onto a FastMCP server."""

    start_time = time.time()

    @mcp.tool(
        name="list_instruments",
        description=(
            "List all instruments currently known to the daemon. Returns "
            "a list of objects with id, manufacturer, model, address, "
            "profile_name, instrument_class, and is_connected. Reads "
            "cached state — does not trigger a hardware scan. Use "
            "scan_instruments for that."
        ),
    )
    async def list_instruments(filter: str = "") -> List[Dict[str, Any]]:
        cap_mgr = ctx.capability_manager
        inst_mgr = ctx.instrument_manager
        results: List[Dict[str, Any]] = []
        for instrument_id, caps in cap_mgr.all_instruments.items():
            entry = {
                "id": instrument_id,
                "manufacturer": caps.manufacturer,
                "model": caps.model,
                "address": caps.visa_address,
                "profile_name": caps.profile_key,
                "instrument_class": caps.instrument_class,
                "is_connected": _safe_is_connected(inst_mgr, instrument_id),
            }
            if filter:
                haystack = " ".join(
                    str(v).lower() for v in entry.values()
                )
                if filter.lower() not in haystack:
                    continue
            results.append(entry)
        return results

    @mcp.tool(
        name="get_capabilities",
        description=(
            "Return the command catalogue for one or more instruments. "
            "Use this to learn what commands are available before "
            "calling execute_command. Pass instrument_id to scope to "
            "one device, instrument_class to scope to a class (e.g. "
            "smu, dmm), or neither to fetch every connected instrument."
        ),
    )
    async def get_capabilities(
        instrument_id: str = "",
        instrument_class: str = "",
    ) -> List[Dict[str, Any]]:
        cap_mgr = ctx.capability_manager
        if instrument_id:
            caps = cap_mgr.get_instrument_caps(instrument_id)
            return [caps.to_capability_dict()] if caps else []
        if instrument_class:
            return [
                c.to_capability_dict()
                for c in cap_mgr.find_by_class(instrument_class)
            ]
        return cap_mgr.get_all_capabilities_list()

    @mcp.tool(
        name="scan_instruments",
        description=(
            "Trigger a fresh hardware scan and return everything that "
            "was discovered. May block briefly while VISA enumerates "
            "the bus. Prefer list_instruments for the cached view."
        ),
    )
    async def scan_instruments() -> List[Dict[str, Any]]:
        inst_mgr = ctx.instrument_manager
        loop = asyncio.get_running_loop()
        try:
            resources = await loop.run_in_executor(None, inst_mgr.rescan_all)
        except Exception as exc:
            logger.warning("scan_instruments rescan failed: %s", exc)
            resources = ()

        cap_mgr = ctx.capability_manager
        results: List[Dict[str, Any]] = []
        for visa_address in resources:
            caps = cap_mgr.get_instrument_caps(visa_address)
            results.append(
                {
                    "id": visa_address,
                    "address": visa_address,
                    "manufacturer": caps.manufacturer if caps else "",
                    "model": caps.model if caps else "",
                    "profile_name": caps.profile_key if caps else "",
                    "instrument_class": (
                        caps.instrument_class if caps else ""
                    ),
                    "is_connected": _safe_is_connected(
                        inst_mgr, visa_address
                    ),
                }
            )
        return results

    @mcp.tool(
        name="list_profiles",
        description=(
            "Return the loaded instrument profiles, each with a count "
            "of currently matched instruments. Profiles are YAML files "
            "loaded at daemon startup; their command and sequence "
            "catalogues populate get_capabilities for any instrument "
            "whose *IDN? string matches."
        ),
    )
    async def list_profiles() -> List[Dict[str, Any]]:
        cap_mgr = ctx.capability_manager
        match_counts: Dict[str, int] = {}
        for caps in cap_mgr.all_instruments.values():
            if caps.has_profile:
                match_counts[caps.profile_key] = (
                    match_counts.get(caps.profile_key, 0) + 1
                )
        return [
            {"profile_key": key, "matched_instruments": count}
            for key, count in sorted(match_counts.items())
        ]

    @mcp.tool(
        name="get_status",
        description=(
            "Return high-level daemon health: edge_id, edge_name, "
            "version, instrument_count, uptime_seconds, hostname, "
            "and OS info."
        ),
    )
    async def get_status() -> Dict[str, Any]:
        cap_mgr = ctx.capability_manager
        return {
            "edge_id": ctx.edge_id,
            "edge_name": ctx.edge_name,
            "version": ctx.version,
            "instrument_count": cap_mgr.instrument_count,
            "profiled_count": cap_mgr.profiled_count,
            "hostname": socket.gethostname(),
            "uptime_seconds": int(time.time() - start_time),
            "os_info": f"{platform.system()} {platform.release()}",
        }


def _safe_is_connected(inst_mgr: Any, instrument_id: str) -> bool:
    try:
        return bool(inst_mgr.is_connected(instrument_id))
    except Exception:
        return False
