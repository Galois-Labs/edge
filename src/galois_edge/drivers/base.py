"""Abstract base class for all protocol drivers (non-SCPI instruments).

Protocol drivers use a shared transport (not per-driver), expose commands
defined in YAML profiles, and are thread-safe via per-instrument locks.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Callable

from galois_edge.drivers.point import Point

logger = logging.getLogger(__name__)


class BaseProtocolDriver(ABC):
    """Contract for protocol drivers.

    Each driver:
    - Uses a SHARED transport (supports RS-485 multi-drop).
    - Exposes commands defined in YAML profile.
    - Thread-safe via per-instrument lock.
    - Reports capabilities for cloud advertisement.
    - Supports batch point reads and subscriptions.
    """

    def __init__(self, instrument_id: str, transport_uri: str, **kwargs: Any) -> None:
        self.instrument_id = instrument_id
        self.transport_uri = transport_uri
        self.lock = threading.Lock()
        self._connected = False
        self._points: dict[str, Point] = {}
        self._commands: dict[str, dict[str, Any]] = {}
        self._subscriptions: dict[str, threading.Event] = {}

    @property
    def connected(self) -> bool:
        return self._connected

    # -- Lifecycle (abstract) --

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def identify(self) -> str: ...

    @abstractmethod
    def get_capabilities(self) -> dict[str, Any]: ...

    # -- Point I/O (abstract) --

    @abstractmethod
    def read_point(self, point: Point) -> Any:
        """Read a single point, return engineering value."""
        ...

    @abstractmethod
    def write_point(self, point: Point, value: Any) -> None:
        """Write a value to a point."""
        ...

    # -- Batch I/O --

    def read_points(self, points: list[Point]) -> dict[str, Any]:
        """Batch read multiple points.

        Default implementation does sequential reads. Protocol subclasses
        may override for efficiency (e.g., contiguous Modbus register reads,
        OPC-UA ReadValueId batching).
        """
        return {p.name: self.read_point(p) for p in points}

    # -- Subscriptions (default: polling) --

    def subscribe(
        self,
        points: list[Point],
        callback: Callable[[dict[str, Any]], None],
        interval_ms: int = 1000,
    ) -> str:
        """Subscribe to point changes via polling.

        OPC-UA/CANopen drivers override with native subscriptions.
        Returns subscription_id for ``unsubscribe()``.
        """
        sub_id = str(uuid.uuid4())
        stop_event = threading.Event()
        self._subscriptions[sub_id] = stop_event

        def _poll() -> None:
            while not stop_event.is_set():
                try:
                    with self.lock:
                        values = self.read_points(points)
                    callback(values)
                except Exception as exc:
                    logger.warning("Subscription %s poll error: %s", sub_id, exc)
                stop_event.wait(interval_ms / 1000.0)

        t = threading.Thread(target=_poll, daemon=True, name=f"sub-{sub_id[:8]}")
        t.start()
        return sub_id

    def unsubscribe(self, subscription_id: str) -> None:
        """Cancel a subscription."""
        stop_event = self._subscriptions.pop(subscription_id, None)
        if stop_event is not None:
            stop_event.set()

    # -- Command execution (YAML-interpreted) --

    def execute_command(self, command_name: str, params: dict[str, Any] | None = None) -> Any:
        """Execute a command defined in the YAML profile."""
        cmd = self._commands.get(command_name)
        if not cmd:
            raise ValueError(f"Unknown command: {command_name}")
        params = params or {}
        with self.lock:
            return self._execute_yaml_command(cmd, params)

    def _execute_yaml_command(self, cmd: dict[str, Any], params: dict[str, Any]) -> Any:
        """Interpret a YAML command definition at runtime."""
        cmd_type = cmd.get("type", "query")

        if cmd_type == "query":
            return self._exec_query(cmd)
        elif cmd_type == "action":
            return self._exec_action(cmd, params)
        elif cmd_type == "sequence":
            return self._exec_sequence(cmd, params)
        else:
            raise ValueError(f"Unknown command type: {cmd_type}")

    def _exec_query(self, cmd: dict[str, Any]) -> Any:
        reads = cmd.get("reads", [])
        points = [self._points[r] for r in reads]
        if len(points) == 1:
            return self.read_point(points[0])
        return self.read_points(points)

    def _exec_action(self, cmd: dict[str, Any], params: dict[str, Any]) -> dict[str, str]:
        for write_spec in cmd.get("writes", []):
            point = self._points[write_spec["register"]]
            value = self._resolve_param(write_spec["value"], params)
            self.write_point(point, value)
        return {"status": "ok"}

    def _exec_sequence(self, cmd: dict[str, Any], params: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for step in cmd.get("steps", []):
            if "write" in step:
                w = step["write"]
                point = self._points[w["register"]]
                value = self._resolve_param(w["value"], params)
                self.write_point(point, value)
                results.append({"wrote": point.name, "value": value})
            elif "wait" in step:
                w = step["wait"]
                point = self._points[w["register"]]
                timeout = w.get("timeout", 60)
                condition_str = w.get("condition", "True")
                deadline = time.time() + timeout
                while time.time() < deadline:
                    value = self.read_point(point)
                    # Evaluate simple conditions like "abs(value - {target}) < 1.0"
                    resolved_cond = self._resolve_param(condition_str, params)
                    try:
                        if eval(str(resolved_cond), {"abs": abs, "value": value, "__builtins__": {}}):
                            break
                    except Exception:
                        break
                    time.sleep(1.0)
                results.append({"waited": point.name, "value": value})
        return results

    @staticmethod
    def _resolve_param(value: Any, params: dict[str, Any]) -> Any:
        """Substitute ``{param_name}`` placeholders with actual values."""
        if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
            param_name = value[1:-1]
            return params[param_name]
        return value
