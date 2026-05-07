"""EdgeContext — handle passed to every MCP tool implementation.

Phase 1 carries direct references to CapabilityManager, CommandHandler,
InstrumentManager. Phase 2 adds `caller_jwt` (validated claim set) and an
`authorize` hook that tools call before any side effect.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..capability_manager import CapabilityManager
    from ..command_handler import CommandHandler
    from .auth import CallerJWT, JWTValidator


# Per-request caller-JWT context. The Starlette middleware in server.py sets
# this from the Galois-Caller-JWT header before the tool handler runs; tools
# read it via EdgeContext.current_caller(). Phase 1 callers (no header set,
# tailnet-direct) get None and skip authorization.
_caller_var: contextvars.ContextVar[Optional["CallerJWT"]] = contextvars.ContextVar(
    "galois_mcp_caller", default=None
)


def set_current_caller(claims: Optional["CallerJWT"]) -> contextvars.Token:
    return _caller_var.set(claims)


def reset_current_caller(token: contextvars.Token) -> None:
    _caller_var.reset(token)


def get_current_caller() -> Optional["CallerJWT"]:
    return _caller_var.get()


@dataclass
class EdgeContext:
    """Per-call references to the daemon's in-process subsystems.

    Tool implementations call into these directly rather than dialling
    localhost gRPC; the servicer is in the same process and the proto
    translation cost is wasted CPU when in-memory dispatch is available.
    """

    capability_manager: "CapabilityManager"
    command_handler: "CommandHandler"
    instrument_manager: Any
    edge_id: str = ""
    edge_name: str = ""
    version: str = "1.0.0"
    sweep_state: Optional[Any] = None

    # Phase 2: optional caller-JWT validator. When None (Phase 1 / tailnet
    # path) every authorize() call is a no-op.
    jwt_validator: Optional["JWTValidator"] = None

    def authorize(
        self,
        tool_name: str,
        scope: Optional[str] = None,
        is_dangerous: bool = False,
    ) -> None:
        """Enforce the per-call ACL on the current caller, if any.

        No-op when the validator isn't configured (Phase 1) or when no
        caller-JWT is on the request (tailnet-direct call). Raises
        PermissionError when the caller is identified but not authorized.
        """
        if self.jwt_validator is None:
            return
        claims = get_current_caller()
        if claims is None:
            return
        from .auth import authorize as _auth

        _auth(claims, tool_name, scope, is_dangerous)
