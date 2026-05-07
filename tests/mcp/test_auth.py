"""Tests for the daemon-side caller-JWT validator + authorize() helper.

Mirrors docs/mcp-integration.md §3.7. The validator is exercised against an
in-memory RSA key pair + an httpx ASGI mock that serves a JWKS document, so
no network is involved.
"""

from __future__ import annotations

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

from galois_edge.mcp.auth import (  # noqa: E402
    CallerJWT,
    InvalidJWTError,
    JWTValidator,
    authorize,
)


# --------------------------------------------------------------------------
# Test fixtures: RSA key pair + minimal JWKS file on disk
# --------------------------------------------------------------------------


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks_for(key, kid: str = "test-kid") -> dict:
    pub = key.public_key().public_numbers()
    n = pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")
    e = pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big")
    import base64

    def b64u(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    return {
        "keys": [
            {
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "kid": kid,
                "n": b64u(n),
                "e": b64u(e),
            }
        ]
    }


@pytest.fixture
def jwks_url(tmp_path, rsa_key):
    """Write a JWKS document to disk and return a file:// URL.

    PyJWKClient accepts file:// URLs via urllib, so this avoids the need to
    spin up a real HTTP server in unit tests.
    """
    jwks = _jwks_for(rsa_key)
    import json

    p = tmp_path / "jwks.json"
    p.write_text(json.dumps(jwks))
    return f"file://{p}"


def _mint(
    rsa_key,
    *,
    aud: str = "edge:abc",
    iss: str = "https://cloud.galoislabs.ai",
    sub: str = "user:u1",
    edge_id: str = "abc",
    tools_allow=None,
    danger_allow: bool = False,
    exp_offset: int = 60,
    kid: str = "test-kid",
) -> str:
    pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    payload = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "iat": now,
        "exp": now + exp_offset,
        "edge_id": edge_id,
        "tools_allow": tools_allow if tools_allow is not None else [],
        "danger_allow": danger_allow,
    }
    return pyjwt.encode(payload, pem, algorithm="RS256", headers={"kid": kid})


# --------------------------------------------------------------------------
# Validator tests
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_happy_path(jwks_url, rsa_key):
    v = JWTValidator(jwks_url=jwks_url, expected_aud="edge:abc")
    token = _mint(rsa_key)
    claims = await v.validate(token)
    assert isinstance(claims, CallerJWT)
    assert claims.aud == "edge:abc"
    assert claims.edge_id == "abc"
    assert claims.danger_allow is False


@pytest.mark.asyncio
async def test_validate_rejects_expired(jwks_url, rsa_key):
    v = JWTValidator(jwks_url=jwks_url, expected_aud="edge:abc")
    token = _mint(rsa_key, exp_offset=-60)
    with pytest.raises(InvalidJWTError) as excinfo:
        await v.validate(token)
    assert "expir" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_validate_rejects_wrong_audience(jwks_url, rsa_key):
    v = JWTValidator(jwks_url=jwks_url, expected_aud="edge:other")
    token = _mint(rsa_key, aud="edge:abc")
    with pytest.raises(InvalidJWTError):
        await v.validate(token)


@pytest.mark.asyncio
async def test_validate_rejects_signature_failure(jwks_url, rsa_key):
    # Sign with a different key.
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = JWTValidator(jwks_url=jwks_url, expected_aud="edge:abc")
    token = _mint(other_key)
    with pytest.raises(InvalidJWTError):
        await v.validate(token)


@pytest.mark.asyncio
async def test_validate_rejects_empty_token(jwks_url):
    v = JWTValidator(jwks_url=jwks_url, expected_aud="edge:abc")
    with pytest.raises(InvalidJWTError):
        await v.validate("")


@pytest.mark.asyncio
async def test_validate_caches_jwks_across_calls(jwks_url, rsa_key):
    """First validate() warms the cache; a second call reuses it without
    re-reading the JWKS file."""
    v = JWTValidator(jwks_url=jwks_url, expected_aud="edge:abc")
    t1 = _mint(rsa_key)
    t2 = _mint(rsa_key)
    await v.validate(t1)
    cached_client = v._jwks.client  # noqa: SLF001
    await v.validate(t2)
    assert v._jwks.client is cached_client  # noqa: SLF001


@pytest.mark.asyncio
async def test_invalidate_drops_cache(jwks_url, rsa_key):
    v = JWTValidator(jwks_url=jwks_url, expected_aud="edge:abc")
    await v.validate(_mint(rsa_key))
    assert v._jwks.client is not None  # noqa: SLF001
    await v.invalidate_cache()
    assert v._jwks.client is None  # noqa: SLF001


# --------------------------------------------------------------------------
# authorize() tests
# --------------------------------------------------------------------------


def _claims(tools_allow, danger_allow=False) -> CallerJWT:
    return CallerJWT(
        iss="https://cloud.galoislabs.ai",
        aud="edge:abc",
        sub="user:u1",
        exp=int(time.time()) + 60,
        edge_id="abc",
        tools_allow=tools_allow,
        danger_allow=danger_allow,
    )


def test_authorize_allows_listed_tool():
    claims = _claims(["list_instruments"])
    authorize(claims, "list_instruments", scope=None, is_dangerous=False)


def test_authorize_denies_unlisted_tool():
    claims = _claims(["list_instruments"])
    with pytest.raises(PermissionError):
        authorize(claims, "execute_command", scope=None, is_dangerous=False)


def test_authorize_allows_qualified_scope():
    claims = _claims(["execute_command:keithley_2400__measure_current"])
    authorize(
        claims,
        "execute_command",
        scope="keithley_2400__measure_current",
        is_dangerous=False,
    )


def test_authorize_denies_wrong_scope():
    claims = _claims(["execute_command:keithley_2400__measure_current"])
    with pytest.raises(PermissionError):
        authorize(
            claims,
            "execute_command",
            scope="keithley_2400__source_voltage",
            is_dangerous=False,
        )


def test_authorize_blocks_dangerous_when_disallowed():
    # Tool is in the allow list, but danger_allow=False blocks it.
    claims = _claims(["execute_command"], danger_allow=False)
    with pytest.raises(PermissionError) as excinfo:
        authorize(claims, "execute_command", scope=None, is_dangerous=True)
    assert "dangerous" in str(excinfo.value)


def test_authorize_permits_dangerous_when_allowed():
    claims = _claims(["execute_command"], danger_allow=True)
    authorize(claims, "execute_command", scope=None, is_dangerous=True)


def test_authorize_empty_tools_allow_denies():
    claims = _claims([])
    with pytest.raises(PermissionError):
        authorize(claims, "list_instruments", scope=None, is_dangerous=False)
