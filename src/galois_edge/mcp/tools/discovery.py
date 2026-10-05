"""Discovery MCP tools: list_instruments, get_capabilities, scan_instruments,
list_profiles, get_status.

These read from CapabilityManager / InstrumentManager directly. None of
them mutate hardware state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import platform
import socket
import time
from typing import Any, Dict, List, Literal, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..context import EdgeContext

logger = logging.getLogger(__name__)


def register_discovery_tools(
    mcp: FastMCP,
    ctx: EdgeContext,
    *,
    dynamic_tools_max: Optional[int] = None,
    mark_simulated: Optional[bool] = None,
) -> None:
    """Register the five Phase-1 discovery tools onto a FastMCP server.

    ``dynamic_tools_max`` (MCP_DYNAMIC_TOOLS_MAX) and ``mark_simulated``
    (SIM_MARK_INSTRUMENTS) fall back to ``Config()`` when None (CI-11).
    """
    from ...config import Config

    cfg = Config() if dynamic_tools_max is None or mark_simulated is None else None
    max_n = dynamic_tools_max if dynamic_tools_max is not None else cfg.mcp_dynamic_tools_max
    mark = mark_simulated if mark_simulated is not None else cfg.sim_mark_instruments

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
            "smu, dmm), or neither to fetch every connected instrument. "
            "detail='full' returns every command (paged by 'page', "
            "0-based; the response has page and pages), 'summary' the "
            "top-level command groups with counts, and 'group' one level "
            "under 'path'. The default is 'full' for small instruments "
            "and 'summary' for large ones."
        ),
    )
    async def get_capabilities(
        instrument_id: str = "",
        instrument_class: str = "",
        detail: Optional[Literal["summary", "group", "full"]] = None,
        path: str = "",
        page: int = 0,
    ) -> List[Dict[str, Any]]:
        cap_mgr = ctx.capability_manager
        if instrument_id:
            caps_list = [c for c in [cap_mgr.get_instrument_caps(instrument_id)] if c is not None]
        elif instrument_class:
            caps_list = cap_mgr.find_by_class(instrument_class)
        else:
            caps_list = list(cap_mgr.all_instruments.values())
        return [_capabilities_for(c, detail, path, page, max_n) for c in caps_list]

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


_IDENTITY_KEYS = ("has_profile", "profile_key", "manufacturer", "model", "instrument_class",
                  "instrument_id", "visa_address", "sequences", "settings")


def _enrich_params(commands: List[Dict[str, Any]], caps: Any) -> None:
    """CI-20: min/max/map/unit on every param (MCP only; gRPC unchanged)."""
    for entry in commands:
        cfg = caps.profile.commands.get(entry["name"]) if caps.profile is not None else None
        declared = (cfg.params or {}) if cfg is not None else {}
        for p in entry.get("parameters", []):
            pc = declared.get(p["name"])
            if pc is None:
                continue
            p["min"], p["max"], p["map"] = pc.min, pc.max, pc.map
            p["unit"] = pc.unit or ""


def _listing(caps: Any, path: str) -> Dict[str, Any]:
    """galois_profiles.nav.list_groups at ``path``; ToolError JSON (CI-19) when it cannot list."""
    from galois_profiles import nav

    try:
        profile = caps.profile.galois_profile
    except Exception as exc:
        raise ToolError(json.dumps({
            "error": f"Profile for {caps.instrument_id} is not navigable: {exc}", "suggestions": []}))
    try:
        return dict(nav.list_groups(profile, path=path, depth=1))
    except (KeyError, ValueError):
        try:
            hits = [h["path"] for h in nav.search(profile, path, limit=5)["results"]]
        except Exception:
            hits = []
        raise ToolError(json.dumps({"error": f"Unknown path '{path}' for {caps.instrument_id}",
                                    "suggestions": hits}))


def _capabilities_for(caps: Any, detail: Optional[str], path: str, page: int, max_n: int) -> Dict[str, Any]:
    """One instrument's get_capabilities entry (edge-api.md §3, F17)."""
    base = caps.to_capability_dict()
    if caps.profile is None:
        return base                                   # protocol drivers / unprofiled: old payload
    n_enabled = len(caps.enabled_commands)
    mode = detail or ("full" if n_enabled <= max_n else "summary")
    if mode == "full":
        commands = base["commands"]
        _enrich_params(commands, caps)
        size = max(max_n, 1)
        pages = max(1, math.ceil(len(commands) / size))
        if not 0 <= page < pages:
            raise ToolError(json.dumps({"error": f"page {page} out of range (pages={pages})"}))
        base.update(commands=commands[page * size:(page + 1) * size], detail="full", page=page, pages=pages)
        return base
    listing = _listing(caps, "" if mode == "summary" else path)
    out = {k: base[k] for k in _IDENTITY_KEYS if k in base}
    out.update(detail=mode, path=listing["path"], groups=listing["children"],
               truncated=listing["truncated"], command_count=n_enabled)
    return out


def _safe_is_connected(inst_mgr: Any, instrument_id: str) -> bool:
    try:
        return bool(inst_mgr.is_connected(instrument_id))
    except Exception:
        return False
