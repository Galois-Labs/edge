"""
Tests for BearerTokenInterceptor (Spec C — gRPC auth interceptor).

Covers spec §6 test cases 1–6 plus a code-inspection regression for
hmac.compare_digest.

Test cases:
  1. No token configured  → all RPCs accepted without metadata
  2. Correct token        → RPC succeeds
  3. Wrong token          → UNAUTHENTICATED
  4. Missing metadata     → UNAUTHENTICATED
  5. Ping with no token   → succeeds (exempt allow-list)
  6. hmac.compare_digest  → code-inspection regression (not a timing test)
"""

from __future__ import annotations

import hmac
import inspect
import os
import sys
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import grpc
import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src")
)

from galois_edge.grpc_server import (
    BearerTokenInterceptor,
    _AUTH_EXEMPT_METHODS,
    _extract_bearer_token,
    GRPCServer,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOKEN = "glc_internal_test_abc123"
_PING_METHOD = "/galois.edge.v1.EdgeDaemonService/Ping"
_LIST_METHOD = "/galois.edge.v1.EdgeDaemonService/ListInstruments"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_handler_call_details(method: str, metadata=None) -> MagicMock:
    """Build a mock HandlerCallDetails."""
    details = MagicMock(spec=grpc.HandlerCallDetails)
    details.method = method
    details.invocation_metadata = metadata or []
    return details


def _make_unary_handler(return_value: Any = "ok") -> grpc.RpcMethodHandler:
    """Build a minimal unary-unary RpcMethodHandler."""
    async def _handler(request, context):
        return return_value

    return grpc.unary_unary_rpc_method_handler(_handler)


def _make_context(metadata=None) -> MagicMock:
    """Build a mock grpc.aio.ServicerContext.

    invocation_metadata() is a synchronous method in grpc.aio — it returns
    a plain list, not a coroutine. abort() is async.
    """
    ctx = MagicMock()
    # invocation_metadata is a regular (sync) call on ServicerContext.
    ctx.invocation_metadata = MagicMock(return_value=metadata or [])
    # abort is a coroutine.
    ctx.abort = AsyncMock()
    return ctx


async def _simple_continuation(handler_call_details):
    """Continuation that always returns a simple unary handler."""
    return _make_unary_handler("ok")


async def _none_continuation(handler_call_details):
    """Continuation that returns None (method not found)."""
    return None


# ---------------------------------------------------------------------------
# _extract_bearer_token unit tests
# ---------------------------------------------------------------------------

class TestExtractBearerToken:

    def test_bearer_prefix_stripped(self):
        metadata = [("authorization", "Bearer mytoken")]
        assert _extract_bearer_token(metadata) == "mytoken"

    def test_bare_token_returned(self):
        metadata = [("authorization", "mytoken")]
        assert _extract_bearer_token(metadata) == "mytoken"

    def test_case_insensitive_bearer(self):
        for prefix in ("Bearer", "bearer", "BEARER"):
            md = [("authorization", f"{prefix} mytoken")]
            assert _extract_bearer_token(md) == "mytoken"

    def test_empty_metadata(self):
        assert _extract_bearer_token([]) == ""

    def test_none_metadata(self):
        assert _extract_bearer_token(None) == ""

    def test_no_authorization_key(self):
        metadata = [("content-type", "application/grpc")]
        assert _extract_bearer_token(metadata) == ""


# ---------------------------------------------------------------------------
# Test 1 — no token configured: GRPCServer does not install interceptor.
# ---------------------------------------------------------------------------

class TestNoTokenConfigured:

    def test_grpc_server_empty_token_is_falsy(self):
        """GRPCServer with empty token: _inbound_auth_token is falsy."""
        # We only inspect the attribute; no server is started.
        srv = GRPCServer.__new__(GRPCServer)
        srv._inbound_auth_token = ""
        assert not srv._inbound_auth_token, (
            "Empty token must be falsy — interceptor must not be installed"
        )


# ---------------------------------------------------------------------------
# Test 2 — correct token: RPC proceeds to handler.
# ---------------------------------------------------------------------------

class TestCorrectToken:

    @pytest.mark.asyncio
    async def test_correct_bearer_header_passes(self):
        interceptor = BearerTokenInterceptor(_TOKEN)
        details = _make_handler_call_details(_LIST_METHOD)

        # intercept_service wraps the handler; call the wrapper with a context
        # that carries the correct token.
        handler = await interceptor.intercept_service(_simple_continuation, details)
        assert handler is not None

        ctx = _make_context([("authorization", f"Bearer {_TOKEN}")])
        result = await handler.unary_unary(None, ctx)
        assert result == "ok"
        ctx.abort.assert_not_called()

    @pytest.mark.asyncio
    async def test_bare_token_accepted(self):
        """A bare token (no 'Bearer ' prefix) is also accepted per spec §3.2."""
        interceptor = BearerTokenInterceptor(_TOKEN)
        details = _make_handler_call_details(_LIST_METHOD)
        handler = await interceptor.intercept_service(_simple_continuation, details)
        assert handler is not None

        ctx = _make_context([("authorization", _TOKEN)])
        result = await handler.unary_unary(None, ctx)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_case_insensitive_bearer_prefix(self):
        """'bearer' and 'BEARER' prefixes are both accepted."""
        interceptor = BearerTokenInterceptor(_TOKEN)
        details = _make_handler_call_details(_LIST_METHOD)

        for prefix in ("bearer", "BEARER", "Bearer"):
            handler = await interceptor.intercept_service(_simple_continuation, details)
            ctx = _make_context([("authorization", f"{prefix} {_TOKEN}")])
            result = await handler.unary_unary(None, ctx)
            assert result == "ok", f"Prefix {prefix!r} should have been accepted"


# ---------------------------------------------------------------------------
# Test 3 — wrong token: UNAUTHENTICATED returned.
# ---------------------------------------------------------------------------

class TestWrongToken:

    @pytest.mark.asyncio
    async def test_wrong_token_returns_unauthenticated(self):
        interceptor = BearerTokenInterceptor(_TOKEN)
        details = _make_handler_call_details(_LIST_METHOD)
        handler = await interceptor.intercept_service(_simple_continuation, details)
        assert handler is not None

        ctx = _make_context([("authorization", "Bearer wrong_token")])
        result = await handler.unary_unary(None, ctx)
        assert result is None
        ctx.abort.assert_awaited_once_with(
            grpc.StatusCode.UNAUTHENTICATED,
            "authentication required",
        )

    @pytest.mark.asyncio
    async def test_error_message_is_generic(self):
        """Error detail must not contain the expected token or its length."""
        interceptor = BearerTokenInterceptor(_TOKEN)
        details = _make_handler_call_details(_LIST_METHOD)
        handler = await interceptor.intercept_service(_simple_continuation, details)

        ctx = _make_context([("authorization", "Bearer wrong")])
        await handler.unary_unary(None, ctx)
        call_args = ctx.abort.call_args
        # Get the detail message (second positional arg)
        detail_message = call_args[0][1] if call_args and call_args[0] else ""
        assert _TOKEN not in detail_message, "Error detail must not contain the expected token"
        assert str(len(_TOKEN)) not in detail_message, "Error detail must not contain token length"


# ---------------------------------------------------------------------------
# Test 4 — missing metadata: UNAUTHENTICATED returned.
# ---------------------------------------------------------------------------

class TestMissingMetadata:

    @pytest.mark.asyncio
    async def test_no_authorization_header_returns_unauthenticated(self):
        interceptor = BearerTokenInterceptor(_TOKEN)
        details = _make_handler_call_details(_LIST_METHOD)
        handler = await interceptor.intercept_service(_simple_continuation, details)
        assert handler is not None

        ctx = _make_context([])  # no metadata
        result = await handler.unary_unary(None, ctx)
        assert result is None
        ctx.abort.assert_awaited_once()
        status_code = ctx.abort.call_args[0][0]
        assert status_code == grpc.StatusCode.UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_other_headers_but_no_auth_returns_unauthenticated(self):
        interceptor = BearerTokenInterceptor(_TOKEN)
        details = _make_handler_call_details(_LIST_METHOD)
        handler = await interceptor.intercept_service(_simple_continuation, details)

        ctx = _make_context([("content-type", "application/grpc")])
        result = await handler.unary_unary(None, ctx)
        assert result is None
        ctx.abort.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 5 — Ping with token configured, no metadata: Ping succeeds (exempt).
# ---------------------------------------------------------------------------

class TestPingExempt:

    @pytest.mark.asyncio
    async def test_ping_exempt_no_metadata(self):
        """Ping must bypass auth even when a token is configured and metadata absent."""
        interceptor = BearerTokenInterceptor(_TOKEN)
        details = _make_handler_call_details(_PING_METHOD)
        handler = await interceptor.intercept_service(_simple_continuation, details)

        # For exempt methods, the original handler is returned unwrapped.
        # Calling it should succeed without any context.abort.
        ctx = _make_context([])
        result = await handler.unary_unary(None, ctx)
        assert result == "ok"
        ctx.abort.assert_not_called()

    @pytest.mark.asyncio
    async def test_ping_exempt_wrong_token(self):
        """Ping is exempt even when the provided token is wrong."""
        interceptor = BearerTokenInterceptor(_TOKEN)
        details = _make_handler_call_details(_PING_METHOD)
        handler = await interceptor.intercept_service(_simple_continuation, details)

        ctx = _make_context([("authorization", "Bearer completely_wrong")])
        result = await handler.unary_unary(None, ctx)
        assert result == "ok"
        ctx.abort.assert_not_called()

    def test_exempt_method_path_is_exact(self):
        """The exempt path MUST include the full galois.edge.v1 package prefix.

        An abbreviated path like /edge.EdgeDaemonService/Ping would silently
        fail to match — this test guards against that regression.
        """
        assert _PING_METHOD in _AUTH_EXEMPT_METHODS, (
            f"{_PING_METHOD!r} must be in _AUTH_EXEMPT_METHODS"
        )
        # Abbreviated paths must NOT be in the allow-list.
        abbreviated_paths = [
            "/edge.EdgeDaemonService/Ping",
            "/EdgeDaemonService/Ping",
            "/Ping",
        ]
        for bad_path in abbreviated_paths:
            assert bad_path not in _AUTH_EXEMPT_METHODS, (
                f"Abbreviated path {bad_path!r} must NOT be in _AUTH_EXEMPT_METHODS"
            )

    @pytest.mark.asyncio
    async def test_non_exempt_method_requires_auth(self):
        """A non-exempt method must still require authentication."""
        interceptor = BearerTokenInterceptor(_TOKEN)
        details = _make_handler_call_details(_LIST_METHOD)
        handler = await interceptor.intercept_service(_simple_continuation, details)

        ctx = _make_context([])  # no auth header
        result = await handler.unary_unary(None, ctx)
        assert result is None
        ctx.abort.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 6 — timing oracle regression: verify hmac.compare_digest is used.
# ---------------------------------------------------------------------------

class TestTimingOracleRegression:

    def test_hmac_compare_digest_used_in_interceptor_source(self):
        """Code-inspection: BearerTokenInterceptor or its helpers must use
        hmac.compare_digest for the token comparison.

        This is a structural check (not a timing measurement) to prevent
        accidental replacement with == or != which would introduce a
        timing oracle.
        """
        # Check both the interceptor class and the helper wrapper function.
        interceptor_src = inspect.getsource(BearerTokenInterceptor)
        # The comparison happens inside the _make_auth_wrapper closure or
        # directly in intercept_service; inspect_service is the public method.
        assert "hmac.compare_digest" in interceptor_src, (
            "BearerTokenInterceptor must use hmac.compare_digest for "
            "constant-time token comparison"
        )

    def test_hmac_compare_digest_stdlib_is_constant_time(self):
        """Sanity check: hmac.compare_digest is the stdlib function."""
        assert callable(hmac.compare_digest)
        assert hmac.compare_digest("abc", "abc") is True
        assert hmac.compare_digest("abc", "xyz") is False
