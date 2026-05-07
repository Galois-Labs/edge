"""Tests for the JWT-aware Starlette middleware on the FastMCP listener.

Stand-in for an end-to-end relay round trip: synthesizes ASGI requests with
the Galois-Caller-JWT header set and asserts the middleware (a) places
validated claims onto the contextvar, (b) rejects bad tokens with a 401
JSON-RPC error, and (c) leaves Phase 1 (no header) requests untouched.
"""

from __future__ import annotations

import json
import os
import sys
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from galois_edge.mcp.auth import JWTValidator  # noqa: E402
from galois_edge.mcp.context import get_current_caller  # noqa: E402
from galois_edge.mcp.server import _CallerJWTMiddleware  # noqa: E402


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks_url(tmp_path, rsa_key):
    pub = rsa_key.public_key().public_numbers()
    n = pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")
    e = pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big")
    import base64

    def b64u(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "kid": "test-kid",
                "n": b64u(n),
                "e": b64u(e),
            }
        ]
    }
    p = tmp_path / "jwks.json"
    p.write_text(json.dumps(jwks))
    return f"file://{p}"


def _mint(rsa_key, **overrides):
    pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    payload = {
        "iss": "https://cloud.galoislabs.ai",
        "aud": "edge:abc",
        "sub": "user:u1",
        "iat": now,
        "exp": now + 60,
        "edge_id": "abc",
        "tools_allow": ["list_instruments"],
        "danger_allow": False,
    }
    payload.update(overrides)
    return pyjwt.encode(payload, pem, algorithm="RS256", headers={"kid": "test-kid"})


# --------------------------------------------------------------------------
# Minimal ASGI harness — exercises the middleware in isolation
# --------------------------------------------------------------------------


class _Captured:
    """Inner ASGI app that records the contextvar's value at call time."""

    def __init__(self) -> None:
        self.captured = None
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        self.captured = get_current_caller()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})


async def _drive(middleware, headers):
    """Invoke `middleware` with a synthetic POST /mcp request and return
    (status, body_bytes)."""
    sent_messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(msg):
        sent_messages.append(msg)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers],
    }
    await middleware(scope, receive, send)

    status = 0
    body = b""
    for m in sent_messages:
        if m["type"] == "http.response.start":
            status = m["status"]
        elif m["type"] == "http.response.body":
            body += m["body"]
    return status, body


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_header_skips_validation(jwks_url):
    inner = _Captured()
    v = JWTValidator(jwks_url=jwks_url, expected_aud="edge:abc")
    mw = _CallerJWTMiddleware(inner, v)
    status, body = await _drive(mw, headers=[])
    assert status == 200
    assert inner.called
    assert inner.captured is None  # contextvar untouched on Phase-1 path


@pytest.mark.asyncio
async def test_valid_token_populates_contextvar(jwks_url, rsa_key):
    inner = _Captured()
    v = JWTValidator(jwks_url=jwks_url, expected_aud="edge:abc")
    mw = _CallerJWTMiddleware(inner, v)
    token = _mint(rsa_key)
    status, _ = await _drive(mw, headers=[("Galois-Caller-JWT", token)])
    assert status == 200
    assert inner.captured is not None
    assert inner.captured.edge_id == "abc"
    assert "list_instruments" in inner.captured.tools_allow


@pytest.mark.asyncio
async def test_invalid_token_short_circuits_with_401(jwks_url, rsa_key):
    inner = _Captured()
    v = JWTValidator(jwks_url=jwks_url, expected_aud="edge:abc")
    mw = _CallerJWTMiddleware(inner, v)
    token = _mint(rsa_key, exp=int(time.time()) - 60)  # expired
    status, body = await _drive(mw, headers=[("Galois-Caller-JWT", token)])
    assert status == 401
    assert not inner.called
    assert b"caller_jwt invalid" in body


@pytest.mark.asyncio
async def test_garbage_token_short_circuits_with_401(jwks_url):
    inner = _Captured()
    v = JWTValidator(jwks_url=jwks_url, expected_aud="edge:abc")
    mw = _CallerJWTMiddleware(inner, v)
    status, body = await _drive(mw, headers=[("Galois-Caller-JWT", "not.a.token")])
    assert status == 401
    assert not inner.called
