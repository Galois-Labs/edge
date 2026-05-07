"""Hot-plug smoke gate (§4.7 #2).

Boots an MCPServer on an ephemeral port, opens a streamable-HTTP MCP
session, calls tools/list, then synthetically registers a new instrument
on the live CapabilityManager. Within 2 seconds we expect both:
    - a notifications/tools/list_changed message on the session, and
    - the new per-instrument tools to appear in a follow-up tools/list.

Run from the worktree root with:

    .venv/bin/python tests/mcp/smoke_hotplug.py
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
from contextlib import closing


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_synthetic_profile(profile_key: str = "synth_smu"):
    from galois_edge.profile_schema import (
        CommandConfig,
        IdentityConfig,
        InstrumentMetadata,
        InstrumentProfile,
        ParameterConfig,
        SettingsConfig,
    )

    if "_" in profile_key:
        manufacturer, _, model = profile_key.partition("_")
    else:
        manufacturer, model = profile_key, "x"

    return InstrumentProfile(
        instrument=InstrumentMetadata(
            manufacturer=manufacturer, model=model, instrument_class="smu",
        ),
        identity=IdentityConfig(patterns=[".*"]),
        settings=SettingsConfig(timeout_ms=5000),
        commands={
            "synth_set": CommandConfig(
                scpi=":SYN {value}",
                type="write",
                params={
                    "value": ParameterConfig(
                        type="float", min=0.0, max=10.0, unit="V",
                    ),
                },
            ),
            "synth_read": CommandConfig(
                scpi=":SYN?",
                type="query",
            ),
        },
    )


async def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.abspath(os.path.join(here, "..", "..", "src"))
    tests_dir = os.path.abspath(os.path.join(here, ".."))

    # Pre-load the PyPI mcp SDK BEFORE any sys.path manipulation that
    # would let tests/mcp/__init__.py shadow the top-level ``mcp``.
    import mcp  # noqa: F401
    import mcp.server.fastmcp  # noqa: F401
    import mcp.client.streamable_http  # noqa: F401

    if src not in sys.path:
        sys.path.insert(0, src)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)

    from galois_edge.capability_manager import CapabilityManager
    from galois_edge.command_handler import CommandHandler
    from galois_edge.mcp.server import MCPServer
    from conftest import MockInstrumentManager
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    cap_mgr = CapabilityManager()
    inst_mgr = MockInstrumentManager(resources=[], idn_map={})
    handler = CommandHandler(inst_mgr)

    port = _free_port()
    server = MCPServer(
        capability_manager=cap_mgr,
        command_handler=handler,
        instrument_manager=inst_mgr,
        port=port,
        path="/mcp",
        edge_id="hotplug-smoke",
        edge_name="hotplug",
    )
    await server.start()

    notification_received = asyncio.Event()
    notification_arrival_t: list = []

    async def message_handler(msg) -> None:
        from mcp import types
        if isinstance(msg, Exception):
            return
        try:
            root = msg.root
        except AttributeError:
            return
        if isinstance(root, types.ToolListChangedNotification):
            notification_arrival_t.append(time.time())
            notification_received.set()

    try:
        url = f"http://127.0.0.1:{port}/mcp"
        async with streamablehttp_client(url) as (read, write, _get_id):
            async with ClientSession(
                read, write, message_handler=message_handler,
            ) as session:
                await session.initialize()
                tools_before = await session.list_tools()
                names_before = {t.name for t in tools_before.tools}
                print(f"tools/list (before): {len(names_before)} tools")

                # Synthetically register a new instrument — the
                # CapabilityManager listener will fire and the dynamic
                # registry will broadcast tools/list_changed.
                profile = _build_synthetic_profile()
                t_register = time.time()
                cap_mgr.register_instrument(
                    instrument_id="HOTPLUG::DEV01",
                    visa_address="HOTPLUG::DEV01",
                    idn_response="synth_smu,x,1,0",
                    profile=profile,
                )

                # Wait up to 2 seconds for the notification.
                try:
                    await asyncio.wait_for(
                        notification_received.wait(), timeout=2.0,
                    )
                    elapsed = (notification_arrival_t[0] - t_register) * 1000.0
                    print(
                        f"tools/list_changed received after "
                        f"{elapsed:.1f} ms"
                    )
                except asyncio.TimeoutError:
                    print(
                        "FAIL: did not receive tools/list_changed within 2s"
                    )
                    return 1

                tools_after = await session.list_tools()
                names_after = {t.name for t in tools_after.tools}
                added = names_after - names_before
                print(f"tools/list (after): {len(names_after)} tools")
                print(f"new tools: {sorted(added)}")
                expected = {"synth_smu__synth_set", "synth_smu__synth_read"}
                if not expected.issubset(added):
                    print(
                        f"FAIL: expected new tools {expected} not present in "
                        f"added set {added}"
                    )
                    return 1

                print("OK")
                return 0
    finally:
        await server.stop()
        await asyncio.sleep(0.1)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
