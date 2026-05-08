"""Generic OPC-UA driver that interprets an OPC-UA profile YAML at runtime.

The driver subclasses ``BaseProtocolDriver`` and overrides ``subscribe()``
with a native OPC-UA monitored-item subscription — change-detection without
polling, with optional deadband. The ``opcua_method`` command type adds
server-side method calls on top of the base ``query / action / sequence``
vocabulary.

I/O is funneled through the ``OPCUABusManager``'s background asyncio loop;
all coroutines are dispatched via ``run_coroutine_threadsafe`` so the rest
of the (synchronous) daemon never sees an event loop directly.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Any, Callable, Optional

from galois_edge.drivers.base import BaseProtocolDriver
from galois_edge.drivers.point import Point

from .transport import (
    DEFAULT_LOOP_CALL_TIMEOUT,
    OPCUABusManager,
    OPCUA_AVAILABLE,
)

if OPCUA_AVAILABLE:
    from asyncua import ua
else:  # pragma: no cover — environments without asyncua
    ua = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Variant type mapping
# ---------------------------------------------------------------------------

# Maps the data_type strings we accept in YAML to (VariantType, python_caster).
# The python_caster coerces the user-provided value to a Python type that
# asyncua's ua.Variant constructor accepts.
def _build_variant_map() -> dict[str, tuple[Any, Callable[[Any], Any]]]:
    if not OPCUA_AVAILABLE:
        return {}
    return {
        "Boolean":   (ua.VariantType.Boolean,   lambda v: bool(v)),
        "SByte":     (ua.VariantType.SByte,     lambda v: int(v)),
        "Byte":      (ua.VariantType.Byte,      lambda v: int(v)),
        "Int16":     (ua.VariantType.Int16,     lambda v: int(v)),
        "UInt16":    (ua.VariantType.UInt16,    lambda v: int(v)),
        "Int32":     (ua.VariantType.Int32,     lambda v: int(v)),
        "UInt32":    (ua.VariantType.UInt32,    lambda v: int(v)),
        "Int64":     (ua.VariantType.Int64,     lambda v: int(v)),
        "UInt64":    (ua.VariantType.UInt64,    lambda v: int(v)),
        "Float":     (ua.VariantType.Float,     lambda v: float(v)),
        "Double":    (ua.VariantType.Double,    lambda v: float(v)),
        "String":    (ua.VariantType.String,    lambda v: str(v)),
        "DateTime":  (ua.VariantType.DateTime,  lambda v: v),
        "ByteString": (ua.VariantType.ByteString, lambda v: bytes(v) if not isinstance(v, bytes) else v),
        "NodeId":    (ua.VariantType.NodeId,    lambda v: v),
        "Guid":      (ua.VariantType.Guid,      lambda v: v),
        "LocalizedText": (ua.VariantType.LocalizedText, lambda v: v),
        "QualifiedName": (ua.VariantType.QualifiedName, lambda v: v),
    }


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


# Asyncua server returns BadUserAccessDenied (or BadNodeIdUnknown / BadNodeIdInvalid)
# for non-existent nodes depending on the server config. Treat the access-denied
# variant as "node id we can't resolve" too — it's the same operator-facing
# failure mode and surfacing it as KeyError matches the spec contract.
_BAD_NODE_ID_TOKENS = (
    "BadNodeIdUnknown",
    "BadNodeIdInvalid",
    "BadUserAccessDenied",
)


def _is_bad_node_id(exc: BaseException) -> bool:
    name = type(exc).__name__
    if any(tok in name for tok in _BAD_NODE_ID_TOKENS):
        return True
    msg = str(exc)
    return any(tok in msg for tok in _BAD_NODE_ID_TOKENS)


# ---------------------------------------------------------------------------
# Subscription handler bridge
# ---------------------------------------------------------------------------


class _SubHandler:
    """asyncua subscription handler that bridges into the user callback.

    asyncua calls ``datachange_notification(node, val, data)`` from the
    background loop. We map ``node.nodeid`` back to the user's point name
    and invoke the user callback with ``{point_name: value}``.

    The callback runs on the asyncio loop thread; the user callback is sync
    and must be cheap. Heavy work belongs in a worker thread.
    """

    def __init__(
        self,
        node_id_to_name: dict[str, str],
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        self._node_id_to_name = node_id_to_name
        self._callback = callback

    def datachange_notification(
        self, node: Any, val: Any, data: Any
    ) -> None:
        try:
            point_name = self._node_id_to_name.get(node.nodeid.to_string())
            if point_name is None:
                # Try a looser match — namespace+identifier without quoting.
                for nid, name in self._node_id_to_name.items():
                    if str(node.nodeid) == nid:
                        point_name = name
                        break
            if point_name is None:
                return
            self._callback({point_name: val})
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning("subscription callback error: %s", exc)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class GenericOpcuaDriver(BaseProtocolDriver):
    """Generic OPC-UA driver — interprets an OpcuaProfile YAML at runtime.

    Connection / read / write are dispatched onto the bus manager's shared
    asyncio loop via ``_loop_call``.

    The ``addressing`` field on each :class:`Point` carries:
      * ``node_id`` — the OPC-UA NodeId string (e.g., ``"ns=2;s=Foo"``)
      * ``data_type`` — the variant type name (Boolean/Int32/…)
      * ``deadband`` — optional absolute change threshold (subscriptions)
    """

    def __init__(
        self,
        instrument_id: str,
        transport_uri: str,
        profile: dict[str, Any],
        bus_manager: OPCUABusManager,
        **kwargs: Any,
    ) -> None:
        super().__init__(instrument_id, transport_uri, **kwargs)
        if not OPCUA_AVAILABLE:
            raise RuntimeError("asyncua is not installed")

        self.profile = profile
        self.bus_manager = bus_manager
        self.client: Any = None

        conn = profile.get("connection", {})

        # Endpoint URL: explicit kwarg > profile.connection.endpoint_url >
        # transport_uri.
        self.endpoint_url: str = (
            kwargs.get("endpoint_url")
            or conn.get("endpoint_url")
            or transport_uri
        )
        self.security_policy: str = kwargs.get(
            "security_policy", conn.get("security_policy", "None")
        )
        self.security_mode: str = kwargs.get(
            "security_mode", conn.get("security_mode", "None")
        )
        self.user_token: str = kwargs.get(
            "user_token", conn.get("user_token", "anonymous")
        )
        self.username: str = kwargs.get(
            "username", conn.get("username", "")
        )
        # Password is read from an env var by name (never inlined in YAML).
        password_env = kwargs.get(
            "password_env", conn.get("password_env", "")
        )
        self._password: str = (
            kwargs.get("password")
            or (os.environ.get(password_env, "") if password_env else "")
        )
        self.client_certificate_path: str = kwargs.get(
            "client_certificate_path", conn.get("client_certificate_path", "")
        )
        self.client_private_key_path: str = kwargs.get(
            "client_private_key_path", conn.get("client_private_key_path", "")
        )
        self.session_timeout_ms: int = int(kwargs.get(
            "session_timeout_ms", conn.get("session_timeout_ms", 60000)
        ))
        self.publish_interval_ms: int = int(kwargs.get(
            "publish_interval_ms", conn.get("publish_interval_ms", 1000)
        ))
        self.loop_call_timeout: float = float(kwargs.get(
            "loop_call_timeout", DEFAULT_LOOP_CALL_TIMEOUT
        ))

        self._variant_map = _build_variant_map()

        # Build Point objects from YAML nodes.
        for name, node_def in profile.get("nodes", {}).items():
            access = node_def.get("access", "read")
            data_type = node_def.get("data_type", "Double")
            addressing: dict[str, Any] = {
                "node_id": node_def["node_id"],
                "data_type": data_type,
                "deadband": node_def.get("deadband"),
                "browse_path": node_def.get("browse_path"),
            }
            range_val = None
            if "range" in node_def:
                r = node_def["range"]
                range_val = (float(r[0]), float(r[1]))

            self._points[name] = Point(
                name=name,
                data_type=data_type,
                access=access,
                scale=float(node_def.get("scale", 1.0)),
                unit=node_def.get("unit", ""),
                range=range_val,
                enum=node_def.get("enum"),
                description=node_def.get("description", ""),
                addressing=addressing,
            )

        # Methods (server-side OPC-UA Method nodes, callable RPCs).
        self._methods: dict[str, dict[str, Any]] = profile.get("methods", {}) or {}
        self._commands = profile.get("commands", {}) or {}

        # Active subscription bookkeeping. Each entry holds the subscription
        # object and the list of monitored-item handles for unsubscribe.
        self._sub_lock = threading.Lock()
        self._subs: dict[str, dict[str, Any]] = {}

    # -- Helpers --

    def _loop_call(self, coro: Any, timeout: Optional[float] = None) -> Any:
        if timeout is None:
            timeout = self.loop_call_timeout
        return self.bus_manager.loop_call(coro, timeout=timeout)

    # -- Lifecycle --

    def connect(self) -> None:
        with self.lock:
            if self._connected:
                return
            self.client = self.bus_manager.get_client(
                endpoint_url=self.endpoint_url,
                security_policy=self.security_policy,
                security_mode=self.security_mode,
                user_token=self.user_token,
                username=self.username,
                password=self._password,
                client_certificate_path=self.client_certificate_path,
                client_private_key_path=self.client_private_key_path,
                session_timeout_ms=self.session_timeout_ms,
                timeout=self.loop_call_timeout,
            )
            self._connected = True
        logger.info(
            "OPC-UA driver connected: %s (instrument=%s)",
            self.endpoint_url, self.instrument_id,
        )

    def disconnect(self) -> None:
        with self.lock:
            if not self._connected:
                return
            # Cancel subscriptions first.
            sub_ids = list(self._subs.keys())
        for sub_id in sub_ids:
            try:
                self.unsubscribe(sub_id)
            except Exception as exc:  # pragma: no cover
                logger.warning("Error during disconnect unsubscribe: %s", exc)

        with self.lock:
            self.bus_manager.release_client(
                endpoint_url=self.endpoint_url,
                user_token=self.user_token,
                username=self.username,
                client_certificate_path=self.client_certificate_path,
                timeout=self.loop_call_timeout,
            )
            self.client = None
            self._connected = False

    def identify(self) -> str:
        """Read the server's product URI and BuildInfo for an identification string."""
        if not self._connected:
            return self._identify_from_profile()

        async def _fetch() -> str:
            parts: list[str] = []
            try:
                product_uri_node = self.client.get_node(
                    ua.NodeId(ua.ObjectIds.Server_ServerStatus_BuildInfo_ProductUri)
                )
                pu = await product_uri_node.read_value()
                if pu:
                    parts.append(str(pu))
            except Exception:
                pass

            try:
                product_name_node = self.client.get_node(
                    ua.NodeId(ua.ObjectIds.Server_ServerStatus_BuildInfo_ProductName)
                )
                pn = await product_name_node.read_value()
                if pn:
                    parts.append(str(pn))
            except Exception:
                pass

            try:
                manuf_node = self.client.get_node(
                    ua.NodeId(ua.ObjectIds.Server_ServerStatus_BuildInfo_ManufacturerName)
                )
                mn = await manuf_node.read_value()
                if mn:
                    parts.append(str(mn))
            except Exception:
                pass

            try:
                ver_node = self.client.get_node(
                    ua.NodeId(ua.ObjectIds.Server_ServerStatus_BuildInfo_SoftwareVersion)
                )
                ver = await ver_node.read_value()
                if ver:
                    parts.append(str(ver))
            except Exception:
                pass

            return " | ".join(parts) if parts else ""

        try:
            ident = self._loop_call(_fetch())
            if ident:
                return f"{ident} @ {self.endpoint_url}"
        except Exception as exc:
            logger.debug("OPC-UA identify fetch failed: %s", exc)
        return self._identify_from_profile()

    def _identify_from_profile(self) -> str:
        identity = self.profile.get("identity", {})
        mfr = identity.get("manufacturer", "?")
        model = identity.get("model", "?")
        return f"{mfr} {model} @ {self.endpoint_url}"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "protocol": "opcua",
            "profile": self.profile.get("identity", {}).get("model", "unknown"),
            "endpoint_url": self.endpoint_url,
            "commands": list(self._commands.keys()),
            "methods": list(self._methods.keys()),
            "points": [p.to_dict() for p in self._points.values()],
            "node_count": len(self._points),
            "writable": sum(
                1 for p in self._points.values() if p.access == "read_write"
            ),
            "supports_native_subscription": True,
        }

    # -- Point I/O --

    def read_point(self, point: Point) -> Any:
        if not self._connected:
            raise IOError("not connected")
        node_id = point.addressing.get("node_id")
        if not node_id:
            raise KeyError(f"point '{point.name}' has no node_id")

        async def _read() -> Any:
            node = self.client.get_node(node_id)
            return await node.read_value()

        try:
            raw = self._loop_call(_read())
        except Exception as exc:
            if _is_bad_node_id(exc):
                raise KeyError(f"unknown node id: {node_id}") from exc
            raise

        if point.enum:
            return point.enum.get(int(raw), raw) if isinstance(raw, int) else raw
        if point.scale and point.scale != 1.0 and isinstance(raw, (int, float)):
            return raw * point.scale
        return raw

    def write_point(self, point: Point, value: Any) -> None:
        if point.access == "read":
            raise PermissionError(
                f"point '{point.name}' (node_id={point.addressing.get('node_id')}) is read-only"
            )
        if not self._connected:
            raise IOError("not connected")

        if point.range is not None and isinstance(value, (int, float)):
            lo, hi = point.range
            if not (lo <= float(value) <= hi):
                raise ValueError(
                    f"value {value} out of range [{lo}, {hi}] for '{point.name}'"
                )

        # Inverse enum mapping (string → int).
        if point.enum and value in point.enum.values():
            inv = {v: k for k, v in point.enum.items()}
            value = inv[value]

        # Inverse scale.
        if point.scale and point.scale != 1.0 and isinstance(value, (int, float)):
            value = value / point.scale

        variant = self._coerce_variant(value, point.addressing.get("data_type", point.data_type))
        node_id = point.addressing["node_id"]

        async def _write() -> None:
            node = self.client.get_node(node_id)
            await node.write_value(variant)

        try:
            self._loop_call(_write())
        except Exception as exc:
            if _is_bad_node_id(exc):
                raise KeyError(f"unknown node id: {node_id}") from exc
            raise

    def read_points(self, points: list[Point]) -> dict[str, Any]:
        """Batch read using a single OPC-UA Read request."""
        if not points:
            return {}
        if not self._connected:
            raise IOError("not connected")

        async def _batch() -> list[Any]:
            nodes = [self.client.get_node(p.addressing["node_id"]) for p in points]
            # asyncua exposes session.read_values(nodes) for batch.
            return await self.client.read_values(nodes)

        raw_values = self._loop_call(_batch())
        results: dict[str, Any] = {}
        for p, raw in zip(points, raw_values):
            if p.enum and isinstance(raw, int):
                results[p.name] = p.enum.get(raw, raw)
            elif p.scale and p.scale != 1.0 and isinstance(raw, (int, float)):
                results[p.name] = raw * p.scale
            else:
                results[p.name] = raw
        return results

    # -- Native subscription override --

    def subscribe(
        self,
        points: list[Point],
        callback: Callable[[dict[str, Any]], None],
        interval_ms: int = 1000,
    ) -> str:
        """Subscribe via OPC-UA monitored items (no polling).

        Per-point absolute deadband is taken from ``point.addressing['deadband']``
        when set. A ``DataChangeFilter`` with ``DeadbandType.Absolute`` is
        installed so the server only sends notifications when the value
        changes by more than the deadband.
        """
        if not self._connected:
            raise IOError("not connected")
        if not points:
            raise ValueError("subscribe() requires at least one point")

        sub_id = str(uuid.uuid4())
        node_id_to_name: dict[str, str] = {}

        async def _build_sub() -> tuple[Any, list[int]]:
            handler = _SubHandler(node_id_to_name, callback)
            sub = await self.client.create_subscription(interval_ms, handler)

            handles: list[int] = []
            for p in points:
                node = self.client.get_node(p.addressing["node_id"])
                node_id_to_name[node.nodeid.to_string()] = p.name
                deadband = p.addressing.get("deadband")
                if deadband is not None and deadband > 0:
                    flt = ua.DataChangeFilter()
                    flt.Trigger = ua.DataChangeTrigger.StatusValue
                    flt.DeadbandType = ua.DeadbandType.Absolute.value
                    flt.DeadbandValue = float(deadband)
                    mparams = ua.MonitoringParameters()
                    mparams.SamplingInterval = float(interval_ms)
                    mparams.QueueSize = 1
                    mparams.DiscardOldest = True
                    mparams.Filter = flt
                    mparams.ClientHandle = len(handles) + 1
                    mi = ua.MonitoredItemCreateRequest()
                    mi.ItemToMonitor = ua.ReadValueId()
                    mi.ItemToMonitor.NodeId = node.nodeid
                    mi.ItemToMonitor.AttributeId = ua.AttributeIds.Value
                    mi.MonitoringMode = ua.MonitoringMode.Reporting
                    mi.RequestedParameters = mparams
                    results = await sub.create_monitored_items([mi])
                    res = results[0]
                    handle = res if isinstance(res, int) else res.MonitoredItemId
                    handles.append(handle)
                else:
                    h = await sub.subscribe_data_change(node)
                    if isinstance(h, list):
                        handles.extend(int(x) for x in h if isinstance(x, int))
                    else:
                        handles.append(int(h))
            return sub, handles

        sub, handles = self._loop_call(_build_sub())
        with self._sub_lock:
            self._subs[sub_id] = {"sub": sub, "handles": handles}
        return sub_id

    def unsubscribe(self, subscription_id: str) -> None:
        with self._sub_lock:
            entry = self._subs.pop(subscription_id, None)
        if entry is None:
            # Fall back to base class behaviour for poller-based subs (none
            # expected for OPC-UA, but cheap correctness).
            super().unsubscribe(subscription_id)
            return

        sub = entry["sub"]
        handles = entry["handles"]

        async def _teardown() -> None:
            if handles:
                try:
                    await sub.unsubscribe(handles)
                except Exception:
                    pass
            try:
                await sub.delete()
            except Exception:
                pass

        try:
            self._loop_call(_teardown())
        except Exception as exc:  # pragma: no cover
            logger.warning("OPC-UA unsubscribe error: %s", exc)

    # -- Method invocation --

    def call_method(
        self, method_name: str, arguments: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Invoke an OPC-UA Method declared in the profile's ``methods`` block."""
        if not self._connected:
            raise IOError("not connected")
        spec = self._methods.get(method_name)
        if spec is None:
            raise KeyError(f"unknown method: {method_name}")

        object_node_id = spec["object_node_id"]
        method_node_id = spec["method_node_id"]
        input_args_spec = spec.get("input_arguments", []) or []
        arguments = arguments or {}

        # Build positional args, in declared order, coerced to Variants.
        variants: list[Any] = []
        for arg_spec in input_args_spec:
            arg_name = arg_spec["name"]
            if arg_name not in arguments:
                raise KeyError(
                    f"method '{method_name}' missing argument '{arg_name}'"
                )
            arg_dt = arg_spec.get("data_type", "Variant")
            variants.append(self._coerce_variant(arguments[arg_name], arg_dt))

        async def _invoke() -> Any:
            obj_node = self.client.get_node(object_node_id)
            method_node = self.client.get_node(method_node_id)
            return await obj_node.call_method(method_node, *variants)

        result = self._loop_call(_invoke())

        # Map back to declared output_arguments dict if provided.
        out_spec = spec.get("output_arguments", []) or []
        if not out_spec:
            return result
        if not isinstance(result, (list, tuple)):
            result_list = [result]
        else:
            result_list = list(result)
        out_dict: dict[str, Any] = {}
        for i, out in enumerate(out_spec):
            if i >= len(result_list):
                break
            out_dict[out["name"]] = result_list[i]
        # If only one output was declared, also return the bare value for
        # convenience — common for "status: bool" style methods.
        if len(out_spec) == 1:
            return out_dict[out_spec[0]["name"]]
        return out_dict

    # -- Browse helpers --

    def browse_path(self, browse_path: list[str], starting_node: Optional[str] = None) -> str:
        """Resolve a browse path to a NodeId string.

        ``browse_path`` is a list of ``"<ns>:<name>"`` segments (asyncua's
        ``get_child`` syntax).
        """
        if not self._connected:
            raise IOError("not connected")

        async def _resolve() -> str:
            if starting_node is None:
                root = self.client.nodes.root
            else:
                root = self.client.get_node(starting_node)
            child = await root.get_child(browse_path)
            return child.nodeid.to_string()

        return self._loop_call(_resolve())

    # -- Command execution: extend with opcua_method --

    def execute_command(
        self, command_name: str, params: Optional[dict[str, Any]] = None,
    ) -> Any:
        cmd = self._commands.get(command_name)
        if cmd is None:
            raise ValueError(f"Unknown command: {command_name}")
        params = params or {}
        if cmd.get("type") == "opcua_method":
            return self._exec_opcua_method(cmd, params)
        return super().execute_command(command_name, params)

    def _exec_opcua_method(
        self, cmd: dict[str, Any], params: dict[str, Any],
    ) -> Any:
        method_name = cmd.get("method")
        if not method_name:
            raise ValueError("opcua_method command missing 'method' field")
        raw_args = cmd.get("arguments", {}) or {}
        resolved: dict[str, Any] = {}
        for k, v in raw_args.items():
            resolved[k] = self._resolve_param(v, params)
        with self.lock:
            return self.call_method(method_name, resolved)

    # -- Variant coercion --

    def _coerce_variant(self, value: Any, data_type: str) -> Any:
        """Coerce a Python value into an ``ua.Variant`` of the declared type."""
        if not OPCUA_AVAILABLE:
            raise RuntimeError("asyncua not installed")

        # Already a Variant — pass through.
        if isinstance(value, ua.Variant):
            return value

        entry = self._variant_map.get(data_type)
        if entry is None:
            # Unknown type — let asyncua sniff it.
            return ua.Variant(value)
        vtype, caster = entry
        try:
            return ua.Variant(caster(value), vtype)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"cannot coerce {value!r} to {data_type}: {exc}"
            ) from exc
