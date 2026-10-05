"""MCP tree-navigation tools (contracts/edge-api.md §3, E5).

Thin wrappers: resolve instrument_id to its profile and return the matching
galois_profiles.nav function's dict unchanged (contracts/python/galois_profiles_nav.py).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, NoReturn

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..context import EdgeContext

RELATIONS = ("writes", "reads", "affects", "gated_by", "implements", "see_also", "used_by_sequence")


def _fail(message: str, suggestions: List[str]) -> NoReturn:
    """CI-19: an MCP tool error (isError=true) whose text is {"error", "suggestions"} JSON."""
    raise ToolError(json.dumps({"error": message, "suggestions": suggestions[:5]}))


def register_navigate_tools(mcp: FastMCP, ctx: EdgeContext) -> None:
    """Register the six command-tree navigation tools onto a FastMCP server."""
    from galois_profiles import nav

    def _profile(instrument_id: str) -> Any:
        caps = ctx.capability_manager.get_instrument_caps(instrument_id)
        if caps is None or caps.profile is None:
            _fail(f"Unknown instrument or no profile: {instrument_id}", [])
        try:
            return caps.profile.galois_profile
        except Exception as exc:
            _fail(f"Profile for {instrument_id} is not navigable: {exc}", [])

    def _suggest(profile: Any, text: str) -> List[str]:
        try:
            return [hit["path"] for hit in nav.search(profile, text, limit=5)["results"]]
        except Exception:
            return []

    @mcp.tool(name="list_command_groups", description=(
        "Browse an instrument's command tree one level at a time. path='' is the root; "
        "returns child groups (with command counts) and commands. Start here for large instruments."))
    async def list_command_groups(instrument_id: str, path: str = "", depth: int = 1) -> Dict[str, Any]:
        profile = _profile(instrument_id)
        try:
            return dict(nav.list_groups(profile, path=path, depth=depth))
        except (KeyError, ValueError):
            _fail(f"Unknown path '{path}' for {instrument_id}", _suggest(profile, path))

    @mcp.tool(name="search_commands", description=(
        "Keyword search over an instrument's command paths, aliases, descriptions and capabilities."))
    async def search_commands(instrument_id: str, query: str, limit: int = 10, path: str = "") -> Dict[str, Any]:
        profile = _profile(instrument_id)
        try:
            return dict(nav.search(profile, query, limit=limit, path=path))
        except (KeyError, ValueError):
            _fail(f"Unknown path '{path}' for {instrument_id}", _suggest(profile, query))

    @mcp.tool(name="describe_command", description=(
        "Full description of one command (path like 'trigger.edge.level' or an old alias): "
        "parameters with min/max/units/defaults, return type, gating, related state."))
    async def describe_command(instrument_id: str, command: str) -> Dict[str, Any]:
        profile = _profile(instrument_id)
        try:
            return dict(nav.describe(profile, command))
        except (KeyError, ValueError):
            _fail(f"Unknown command '{command}' for {instrument_id}", _suggest(profile, command))

    @mcp.tool(name="related_commands", description=(
        "Commands related to one command by a graph relation: writes, reads, affects, gated_by, "
        "implements, see_also, used_by_sequence."))
    async def related_commands(instrument_id: str, command: str, relation: str) -> Dict[str, Any]:
        profile = _profile(instrument_id)
        if relation not in RELATIONS:
            _fail(f"Unknown relation '{relation}' for {instrument_id}", list(RELATIONS))
        try:
            return dict(nav.related(profile, command, relation))
        except (KeyError, ValueError):
            _fail(f"Unknown command '{command}' for {instrument_id}", _suggest(profile, command))

    @mcp.tool(name="find_capability", description=(
        "Which commands implement a capability (e.g. 'psu.set_voltage') on one instrument or all of them."))
    async def find_capability(capability: str, instrument_id: str = "") -> Dict[str, Any]:
        if instrument_id:
            profile = _profile(instrument_id)
            profiles = {profile.key: profile}
        else:
            profiles = {}
            for caps in ctx.capability_manager.all_instruments.values():
                if caps.profile is None:
                    continue
                try:
                    gp = caps.profile.galois_profile
                except Exception:
                    continue
                profiles.setdefault(gp.key, gp)
        try:
            return dict(nav.find_capability(profiles, capability))
        except (KeyError, ValueError):
            _fail(f"Unknown capability '{capability}'", [])

    @mcp.tool(name="get_state_schema", description=(
        "Browse an instrument's typed state (the world-model state schema) one level at a time."))
    async def get_state_schema(instrument_id: str, path: str = "", depth: int = 1) -> Dict[str, Any]:
        profile = _profile(instrument_id)
        try:
            return dict(nav.state_schema(profile, path=path, depth=depth))
        except (KeyError, ValueError):
            _fail(f"Unknown state path '{path}' for {instrument_id}", [])
