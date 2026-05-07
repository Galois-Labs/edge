"""Caller-JWT validation for MCP-over-relay calls.

Phase 2 of docs/mcp-integration.md authenticates each per-tool MCP call with
a short-lived RS256 JWT minted by the cloud and validated here. The signing
key pair lives on the cloud; we fetch the public key from the cloud's JWKS
endpoint, cache it for 24h, and refresh on signature failure.

The validator is invoked from a FastMCP middleware (server.py) when the
inbound HTTP request carries a `Galois-Caller-JWT` header (set by the Go
relay client when forwarding `mcp_request.caller_jwt` to the local FastMCP).
Tailnet-direct callers (Phase 1) don't carry that header and bypass this
module entirely — that is intentional, because tailnet membership *is* the
auth boundary in Phase 1.

Error model:
- `InvalidJWTError` covers signature failure, expiry, audience mismatch, and
  malformed bodies. The middleware translates it into an MCP JSON-RPC error
  before the tool handler runs.
- `PermissionError` is the standard library exception raised by `authorize`
  when the caller's claims allow neither the tool nor the scope.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
import jwt as pyjwt
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class InvalidJWTError(Exception):
    """Raised when a JWT fails validation (signature, expiry, audience)."""


# --------------------------------------------------------------------------
# Claim shape
# --------------------------------------------------------------------------


@dataclass
class CallerJWT:
    """Validated claim set for one MCP call.

    Mirrors the JWT shape in docs/mcp-integration.md §3.3.1. Only fields
    consulted by the daemon are listed here; the cloud may carry additional
    private claims but the daemon ignores them.
    """

    iss: str
    aud: str
    sub: str
    exp: int
    edge_id: str
    tools_allow: list[str]
    danger_allow: bool
    iat: int = 0

    @classmethod
    def from_claims(cls, claims: dict) -> "CallerJWT":
        try:
            return cls(
                iss=str(claims["iss"]),
                aud=str(claims["aud"]),
                sub=str(claims["sub"]),
                exp=int(claims["exp"]),
                iat=int(claims.get("iat", 0)),
                edge_id=str(claims["edge_id"]),
                tools_allow=list(claims.get("tools_allow") or []),
                danger_allow=bool(claims.get("danger_allow", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidJWTError(f"malformed claims: {exc}") from exc


# --------------------------------------------------------------------------
# JWKS cache
# --------------------------------------------------------------------------


@dataclass
class _JWKSCache:
    """Tiny TTL cache around `PyJWKClient`.

    PyJWKClient itself caches keys, but we want a deterministic 24h refresh
    plus an explicit `invalidate()` hook the validator can call on signature
    failure. We hold the underlying client and our own `expires_at`.
    """

    jwks_url: str
    ttl_s: int
    client: Optional[PyJWKClient] = None
    expires_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def get(self) -> PyJWKClient:
        async with self.lock:
            now = time.monotonic()
            if self.client is None or now >= self.expires_at:
                # PyJWKClient does the actual HTTP fetch lazily on
                # get_signing_key_from_jwt; build a fresh client to ensure
                # a stale cache is dropped.
                self.client = PyJWKClient(self.jwks_url, cache_keys=True)
                self.expires_at = now + self.ttl_s
            return self.client

    async def invalidate(self) -> None:
        async with self.lock:
            self.client = None
            self.expires_at = 0.0


# --------------------------------------------------------------------------
# Validator
# --------------------------------------------------------------------------


class JWTValidator:
    """Validates inbound caller JWTs against the cloud-served JWKS.

    Construction is cheap; the JWKS is fetched lazily on the first call.
    Pass the same instance into FastMCP middleware (one per process).
    """

    def __init__(
        self,
        jwks_url: str,
        expected_aud: str,
        expected_iss: str = "https://cloud.galoislabs.ai",
        cache_ttl_s: int = 86_400,
    ) -> None:
        self._jwks_url = jwks_url
        self._expected_aud = expected_aud
        self._expected_iss = expected_iss
        self._jwks = _JWKSCache(jwks_url=jwks_url, ttl_s=cache_ttl_s)

    @property
    def jwks_url(self) -> str:
        return self._jwks_url

    @property
    def expected_aud(self) -> str:
        return self._expected_aud

    async def validate(self, token: str) -> CallerJWT:
        """Validate `token` and return parsed claims, or raise."""
        if not token:
            raise InvalidJWTError("empty token")

        client = await self._jwks.get()
        try:
            signing_key = client.get_signing_key_from_jwt(token).key
        except (InvalidTokenError, httpx.HTTPError, Exception) as exc:
            # Drop the cache and try once more; the cloud may have rotated
            # keys since our last fetch.
            await self._jwks.invalidate()
            try:
                client = await self._jwks.get()
                signing_key = client.get_signing_key_from_jwt(token).key
            except Exception as exc2:
                raise InvalidJWTError(f"JWKS lookup failed: {exc2}") from exc2

        try:
            claims = pyjwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self._expected_aud,
                issuer=self._expected_iss,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except InvalidTokenError as exc:
            raise InvalidJWTError(str(exc)) from exc

        return CallerJWT.from_claims(claims)

    async def invalidate_cache(self) -> None:
        """Force a JWKS refresh on the next validate() call."""
        await self._jwks.invalidate()


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


def authorize(
    claims: CallerJWT,
    tool_name: str,
    scope: Optional[str],
    is_dangerous: bool,
) -> None:
    """Enforce the per-call ACL contained in `claims`.

    Raises `PermissionError` if neither the bare tool name nor the qualified
    `<tool>:<scope>` form appears in `tools_allow`. Raises additionally if
    `is_dangerous` is True and `danger_allow` is False.

    Empty `tools_allow` is treated as deny-all — minting code at the cloud
    must always populate this list, never elide it.
    """
    if is_dangerous and not claims.danger_allow:
        raise PermissionError(
            f"tool {tool_name!r} is dangerous and danger_allow=false"
        )

    if not claims.tools_allow:
        raise PermissionError(f"tool {tool_name!r} not in (empty) tools_allow")

    if tool_name in claims.tools_allow:
        return
    if scope is not None:
        qualified = f"{tool_name}:{scope}"
        if qualified in claims.tools_allow:
            return

    raise PermissionError(
        f"tool {tool_name!r} (scope={scope!r}) not in tools_allow"
    )
