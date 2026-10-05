"""Profiles for the M1 MCP tests: one v1 (edge's DP800) and one v2 (edgesim's PSU), plus a 250-command v2."""
from __future__ import annotations

import json
from pathlib import Path

from galois_edge.profile_schema import _gp_api, profile_from_dict, profile_from_galois

ROOT = Path(__file__).resolve().parents[2]
PSU_ADDR = "TCPIP0::127.0.0.1::5025::SOCKET"
DP_ADDR = "TCPIP0::10.0.0.9::5555::SOCKET"
BIG_ADDR = "TCPIP0::10.0.0.10::5025::SOCKET"


def psu_profile():
    text = (ROOT / "third_party/edgesim/contracts/examples/profiles/galois_sim-psu-2.yaml").read_text()
    return profile_from_dict(_gp_api("load_yaml")(text))


def dp800_profile():
    return profile_from_galois(_gp_api("load_profile")(ROOT / "src/galois_edge/profiles/scpi/rigol_dp800.yaml"))


def big_profile(groups: int = 25, per_group: int = 10):
    commands = {f"g{g}": {"_doc": f"group {g}",
                          **{f"c{i}": {"scpi": f":G{g}:C{i}?", "type": "query"} for i in range(per_group)}}
                for g in range(groups)}
    return profile_from_dict({"schema_version": 2,
                              "instrument": {"manufacturer": "ACME", "model": "BIG", "class": "dmm"},
                              "identity": {"patterns": ["ACME,BIG"]}, "commands": commands})


def register_m1_instruments(cap_mgr) -> None:
    cap_mgr.register_instrument(PSU_ADDR, PSU_ADDR, "GALOIS,SIM-PSU-2,0,1", psu_profile())
    cap_mgr.register_instrument(DP_ADDR, DP_ADDR, "RIGOL TECHNOLOGIES,DP832,0,1", dp800_profile())
    cap_mgr.register_instrument(BIG_ADDR, BIG_ADDR, "ACME,BIG,0,1", big_profile())


def parse(result):
    """Unwrap FastMCP call_tool results (same logic as tests/mcp/test_execute_tools.py:_parse_call)."""
    if isinstance(result, tuple) and len(result) == 2:
        structured = result[1]
        return structured["result"] if isinstance(structured, dict) and "result" in structured else structured
    blocks = result if isinstance(result, list) else result[0]
    return json.loads(blocks[0].text) if blocks else None


def tool_error(exc) -> dict:
    """FastMCP wraps raised errors as 'Error executing tool <name>: <message>' (CI-19)."""
    return json.loads(str(exc).split(": ", 1)[1])
