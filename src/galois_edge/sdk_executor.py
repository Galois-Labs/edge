"""
SDK Executor for vendor Python SDK-based instruments.

Handles dynamic import, client lifecycle, and command execution for
instruments controlled via Python SDKs instead of SCPI/VISA. Vendor
SDKs are loaded at runtime via importlib.import_module() so the daemon
does not hard-depend on any particular vendor package.
"""

from __future__ import annotations

import importlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .profile_schema import SDKCallConfig, SDKConfig

logger = logging.getLogger(__name__)


@dataclass
class _SDKClient:
    """Internal bookkeeping for a connected SDK client."""
    client: Any
    sdk_config: SDKConfig
    lock: threading.Lock = field(default_factory=threading.Lock)


class SDKExecutor:
    """Manages SDK client lifecycle and command execution.

    Each instrument gets its own client instance protected by a
    per-client lock for thread safety. The InstrumentManager is
    accepted as a constructor dependency (injected, not imported).
    """

    def __init__(self, instrument_manager: Any) -> None:
        self._instrument_manager = instrument_manager
        self._clients: Dict[str, _SDKClient] = {}

    # -- Connection lifecycle ---

    def connect(
        self,
        instrument_id: str,
        sdk_config: SDKConfig,
        runtime_args: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Import vendor SDK, instantiate client, and connect.

        Returns True on success (or if already connected), False on error.
        """
        if instrument_id in self._clients:
            logger.warning("SDK client already connected: %s", instrument_id)
            return True

        try:
            module = importlib.import_module(sdk_config.import_path)
        except ImportError:
            logger.error(
                "Cannot import '%s'. Install with: pip install %s",
                sdk_config.import_path, sdk_config.package,
            )
            return False

        cls = getattr(module, sdk_config.class_name, None)
        if cls is None:
            logger.error(
                "Class '%s' not found in module '%s'",
                sdk_config.class_name, sdk_config.import_path,
            )
            return False

        try:
            constructor_args = _resolve_args(sdk_config.connect.constructor_args, runtime_args)
            client = cls(**constructor_args) if constructor_args else cls()

            if sdk_config.connect.method:
                connect_args = _resolve_args(
                    sdk_config.connect.args, runtime_args, sdk_config.connect.defaults,
                )
                connect_fn = getattr(client, sdk_config.connect.method)
                if connect_args:
                    connect_fn(**connect_args)
                else:
                    connect_fn()

            self._clients[instrument_id] = _SDKClient(client=client, sdk_config=sdk_config)
            logger.info("SDK client connected: %s", instrument_id)
            return True

        except Exception as exc:
            logger.error("Failed to connect SDK client %s: %s", instrument_id, exc)
            return False

    def disconnect(self, instrument_id: str) -> bool:
        """Disconnect and remove an SDK client. Returns True if found."""
        entry = self._clients.pop(instrument_id, None)
        if entry is None:
            return False

        with entry.lock:
            try:
                disconnect_fn = getattr(entry.client, entry.sdk_config.disconnect.method, None)
                if disconnect_fn is not None and callable(disconnect_fn):
                    disconnect_fn()
            except Exception as exc:
                logger.warning("Error during SDK disconnect for %s: %s", instrument_id, exc)

        logger.info("SDK client disconnected: %s", instrument_id)
        return True

    def disconnect_all(self) -> None:
        """Disconnect every SDK client. Call during shutdown."""
        for instrument_id in list(self._clients.keys()):
            self.disconnect(instrument_id)

    def is_connected(self, instrument_id: str) -> bool:
        return instrument_id in self._clients

    # -- Command execution ---

    def execute(
        self,
        instrument_id: str,
        command_name: str,
        params: Optional[Dict[str, Any]] = None,
        sdk_call: Optional[SDKCallConfig] = None,
        is_query: bool = True,
    ) -> Dict[str, Any]:
        """Execute an SDK command on a connected instrument.

        Returns dict with success, response, error, execution_time_ms.
        """
        entry = self._clients.get(instrument_id)
        if entry is None:
            return _fail(f"SDK client not connected: {instrument_id}")

        if sdk_call is None:
            return _fail(f"No sdk_call config for '{command_name}' on {instrument_id}")

        start = time.monotonic()
        with entry.lock:
            try:
                coerced = _coerce_params(params)
                result = _dispatch(entry.client, sdk_call, coerced, is_query)
                response = _serialize_result(result, sdk_call.result_field)
                elapsed = (time.monotonic() - start) * 1000.0
                logger.info("SDK '%s' on %s completed in %.1fms", command_name, instrument_id, elapsed)
                return {"success": True, "response": response, "error": "", "execution_time_ms": round(elapsed, 2)}
            except Exception as exc:
                elapsed = (time.monotonic() - start) * 1000.0
                msg = f"SDK execution error ({command_name}): {exc}"
                logger.error("%s (instrument=%s)", msg, instrument_id)
                return {"success": False, "response": "", "error": msg, "execution_time_ms": round(elapsed, 2)}

    # -- Identity query ---

    def identify(self, instrument_id: str, sdk_config: SDKConfig) -> Optional[str]:
        """Get identity string from an SDK instrument, or None."""
        if sdk_config.identity is None:
            return None
        entry = self._clients.get(instrument_id)
        if entry is None:
            return None

        with entry.lock:
            try:
                ident = sdk_config.identity
                if ident.method:
                    return str(getattr(entry.client, ident.method)())
                if ident.property:
                    return str(getattr(entry.client, ident.property))
            except Exception as exc:
                logger.warning("SDK identity query failed for %s: %s", instrument_id, exc)
        return None

    def connected_instruments(self) -> List[str]:
        """List of currently connected SDK instrument IDs."""
        return list(self._clients.keys())

    # -- MCP tool registration (Phase 3) ---

    def register_with_mcp(self, registry: Any) -> int:
        """Emit per-SDK typed MCP tools for every wrapper that publishes
        an ``MCP_TOOL_SPECS`` constant.

        Each tool routes through :meth:`call_method` internally so the
        agent never sees the opaque ``ProxySDKCall`` surface — they call
        e.g. ``dps150_wrapper__set_voltage(instrument_id="x", value=5.0)``
        and the dispatch lands on the right per-instrument lock.

        Returns the number of tools registered.
        """
        from .mcp.sdk_tools import register_sdk_typed_tools

        return register_sdk_typed_tools(registry, self)

    def call_method(
        self,
        instrument_id: str,
        method_name: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Invoke a method by name on a connected SDK client.

        Used by the Phase-3 typed SDK tool surface. Each wrapper's
        ``MCP_TOOL_SPECS`` declares a ``name``; this method resolves
        ``getattr(client, name)`` and calls it with the supplied kwargs.
        """
        entry = self._clients.get(instrument_id)
        if entry is None:
            return _fail(f"SDK client not connected: {instrument_id}")

        start = time.monotonic()
        with entry.lock:
            try:
                fn = getattr(entry.client, method_name, None)
                if fn is None or not callable(fn):
                    return _fail(
                        f"SDK method '{method_name}' not found on {instrument_id}"
                    )
                kwargs = _coerce_params(params) or {}
                result = fn(**kwargs) if kwargs else fn()
                elapsed = (time.monotonic() - start) * 1000.0
                return {
                    "success": True,
                    "response": _serialize_result(result),
                    "error": "",
                    "execution_time_ms": round(elapsed, 2),
                }
            except Exception as exc:
                elapsed = (time.monotonic() - start) * 1000.0
                return {
                    "success": False,
                    "response": "",
                    "error": f"SDK method '{method_name}' raised: {exc}",
                    "execution_time_ms": round(elapsed, 2),
                }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _dispatch(
    client: Any, sdk_call: SDKCallConfig,
    params: Optional[Dict[str, Any]], is_query: bool,
) -> Any:
    """Route to the correct SDK method or property."""
    mapped = _map_params(sdk_call.args_map, params)

    if sdk_call.is_property:
        if is_query:
            prop = sdk_call.getter or sdk_call.method
            if not prop:
                raise ValueError("SDK property read requires 'getter' or 'method'")
            return getattr(client, prop)
        else:
            prop = sdk_call.setter or sdk_call.method
            if not prop:
                raise ValueError("SDK property write requires 'setter' or 'method'")
            value = mapped.get("value") if mapped else None
            setattr(client, prop, value)
            return "OK"

    method_name = (sdk_call.getter or sdk_call.method) if is_query else (sdk_call.setter or sdk_call.method)
    if not method_name:
        raise ValueError("SDK command has no method name for this action")

    fn = getattr(client, method_name)
    return fn(**mapped) if mapped else fn()


# ---------------------------------------------------------------------------
# Parameter helpers
# ---------------------------------------------------------------------------

def _resolve_args(
    template: Optional[Dict[str, str]],
    runtime: Optional[Dict[str, str]],
    defaults: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Substitute {placeholder} values in argument templates."""
    if not template:
        return None
    merged: Dict[str, str] = {}
    if defaults:
        merged.update({k: str(v) for k, v in defaults.items()})
    if runtime:
        merged.update(runtime)

    resolved: Dict[str, Any] = {}
    for key, val in template.items():
        if isinstance(val, str) and val.startswith("{") and val.endswith("}"):
            placeholder = val[1:-1]
            resolved[key] = _coerce_value(merged[placeholder]) if placeholder in merged else val
        else:
            resolved[key] = val
    return resolved


def _map_params(
    args_map: Optional[Dict[str, str]], params: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Map profile param names to SDK kwarg names via args_map."""
    if not params:
        return None
    if not args_map:
        return params
    mapped = {sdk_name: params[profile_name] for profile_name, sdk_name in args_map.items() if profile_name in params}
    return mapped or None


def _coerce_params(params: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Coerce string parameter values to int/float where possible."""
    if not params:
        return params
    return {k: (_coerce_value(v) if isinstance(v, str) else v) for k, v in params.items()}


def _coerce_value(value: str) -> Any:
    """Attempt to coerce a string to int, float, or bool."""
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    lower = value.lower()
    if lower in ("true", "false"):
        return lower == "true"
    return value


def _fail(msg: str) -> Dict[str, Any]:
    """Build an error result dict."""
    return {"success": False, "response": "", "error": msg, "execution_time_ms": 0.0}


# ---------------------------------------------------------------------------
# Result serialization
# ---------------------------------------------------------------------------

def _serialize_result(result: Any, result_field: Optional[str] = None) -> str:
    """Convert an SDK return value to a string for gRPC responses."""
    if result_field:
        if isinstance(result, dict):
            result = result.get(result_field, result)
        elif hasattr(result, result_field):
            result = getattr(result, result_field)

    if result is None:
        return ""
    if isinstance(result, (int, float, bool)):
        return str(result)
    if isinstance(result, str):
        return result
    if isinstance(result, bytes):
        return result.decode("utf-8", errors="replace")
    try:
        return json.dumps(result, default=str)
    except (TypeError, ValueError):
        return str(result)
