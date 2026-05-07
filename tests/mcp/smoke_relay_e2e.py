"""Manual smoke test for the Phase 2 relay round-trip.

Not part of the pytest suite — run by hand with::

    cd .claude/worktrees/mcp-integration
    .venv/bin/python tests/mcp/smoke_relay_e2e.py

Exercises the relay envelope without touching the cloud. Specifically:

  1. Start a FastMCP server (the same MCPServer class main.py uses) on
     127.0.0.1:8767 with a JWT validator wired against a tmp JWKS file.
  2. Mint a hand-rolled RS256 JWT covering the same key.
  3. POST a JSON-RPC `initialize` body with the Galois-Caller-JWT header
     set, asserting we get a 200 and a sane initialize response.
  4. POST `tools/list` and assert the static Phase-1 tool surface comes
     back.

What's NOT exercised here (and would need a scratch Postgres + the cloud
relay running):

  - The actual mcp_request frame on the relay WebSocket.
  - The cloud's streamable-HTTP termination in handler/mcp.go.
  - The /.well-known/jwks.json endpoint round-trip.

Those are deferred to an integration test that brings up the cloud +
Postgres in a docker-compose. This script proves the daemon-side plumbing
holds together end-to-end.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import time
from contextlib import suppress

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import httpx
import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _make_jwks_file(key) -> str:
    pub = key.public_key().public_numbers()
    n = pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")
    e = pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big")
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "kid": "smoke-kid",
                "n": _b64u(n),
                "e": _b64u(e),
            }
        ]
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(jwks, f)
    return path


def _mint(key, edge_id: str = "smoke-edge") -> str:
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    payload = {
        "iss": "https://cloud.galoislabs.ai",
        "aud": f"edge:{edge_id}",
        "sub": "user:smoke",
        "iat": now,
        "exp": now + 300,
        "edge_id": edge_id,
        "tools_allow": [
            "list_instruments",
            "get_capabilities",
            "list_profiles",
            "get_status",
        ],
        "danger_allow": False,
    }
    return pyjwt.encode(payload, pem, algorithm="RS256", headers={"kid": "smoke-kid"})


async def _smoke() -> int:
    from galois_edge.capability_manager import CapabilityManager
    from galois_edge.command_handler import CommandHandler
    from galois_edge.instrument_manager import InstrumentManager
    from galois_edge.mcp.auth import JWTValidator
    from galois_edge.mcp.server import MCPServer

    edge_id = "smoke-edge"

    # In-memory subsystems with no real instruments.
    caps = CapabilityManager()
    insts = InstrumentManager(gpib_enabled=False, usb_raw_enabled=False)
    handler = CommandHandler(insts)

    # JWKS + JWT.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks_path = _make_jwks_file(key)
    jwks_url = f"file://{jwks_path}"
    validator = JWTValidator(jwks_url=jwks_url, expected_aud=f"edge:{edge_id}")

    server = MCPServer(
        capability_manager=caps,
        command_handler=handler,
        instrument_manager=insts,
        port=8769,
        path="/mcp",
        edge_id=edge_id,
        edge_name="smoke",
        jwt_validator=validator,
    )
    await server.start()
    try:
        token = _mint(key, edge_id=edge_id)

        # Use the streamable_http client built into mcp's SDK rather than
        # crafting raw HTTP; this matches what a real client (Anthropic
        # connector, Claude Desktop) would send.
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
            "http://127.0.0.1:8769/mcp",
            headers={"Galois-Caller-JWT": token},
        ) as (r, w, _):
            async with ClientSession(r, w) as session:
                init = await session.initialize()
                print(f"initialize: serverInfo={init.serverInfo}")
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                print(f"tools/list returned {len(names)} tools: {names}")
                if "list_instruments" not in names:
                    print("FAIL: list_instruments not present")
                    return 1

        # Negative: a bogus JWT should 401.
        async with httpx.AsyncClient() as hc:
            resp = await hc.post(
                "http://127.0.0.1:8769/mcp",
                json={"jsonrpc": "2.0", "id": 99, "method": "initialize"},
                headers={
                    "Galois-Caller-JWT": "not.a.token",
                    "Accept": "application/json, text/event-stream",
                },
            )
            print(f"bogus-jwt status={resp.status_code} body={resp.text[:120]}")
            if resp.status_code != 401:
                print("FAIL: expected 401 for bogus JWT")
                return 1

    finally:
        with suppress(Exception):
            await server.stop()
        with suppress(Exception):
            os.unlink(jwks_path)

    print("\nSMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_smoke()))
