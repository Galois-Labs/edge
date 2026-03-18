"""Shared Modbus transport manager for RS-485 multi-drop.

Multiple slave IDs on the same physical serial bus share one pymodbus
client and one lock.  The ``ModbusBusManager`` owns clients keyed by
``(port, baud, parity, stopbits)`` for RTU or ``(host, port)`` for TCP.
"""

from __future__ import annotations

import logging
import threading
from urllib.parse import urlparse

from pymodbus.client import ModbusSerialClient, ModbusTcpClient

logger = logging.getLogger(__name__)


class ModbusBusManager:
    """Manages shared pymodbus clients for physical buses."""

    def __init__(self) -> None:
        self._buses: dict[str, dict] = {}
        self._mgr_lock = threading.Lock()

    def _bus_key(self, transport_uri: str, **kwargs: object) -> str:
        """Generate unique key for a physical bus."""
        parsed = urlparse(transport_uri)

        if parsed.scheme in ("rtu", "ascii"):
            port = parsed.path or parsed.netloc
            baud = kwargs.get("baudrate", 9600)
            parity = kwargs.get("parity", "N")
            stopbits = kwargs.get("stopbits", 1)
            return f"{parsed.scheme}:{port}:{baud}:{parity}:{stopbits}"

        if parsed.scheme == "tcp":
            host = parsed.hostname or "127.0.0.1"
            port_num = parsed.port or 502
            return f"tcp:{host}:{port_num}"

        raise ValueError(f"Unknown transport scheme: {parsed.scheme}")

    def get_client(self, transport_uri: str, **kwargs: object) -> tuple:
        """Return ``(client, lock)`` for the given transport URI.

        Creates and connects the client on first request.  Subsequent
        requests for the same physical bus return the same client.
        """
        with self._mgr_lock:
            key = self._bus_key(transport_uri, **kwargs)

            if key not in self._buses:
                parsed = urlparse(transport_uri)

                if parsed.scheme in ("rtu", "ascii"):
                    port = parsed.path or parsed.netloc
                    # Windows COM>9 needs \\.\COM10 format
                    if port.upper().startswith("COM"):
                        try:
                            port_num = int(port[3:])
                            if port_num >= 10:
                                port = f"\\\\.\\{port.upper()}"
                        except ValueError:
                            pass

                    client = ModbusSerialClient(
                        port,
                        baudrate=int(kwargs.get("baudrate", 9600)),
                        parity=str(kwargs.get("parity", "N")),
                        stopbits=int(kwargs.get("stopbits", 1)),
                        timeout=float(kwargs.get("timeout", 1.0)),
                    )
                elif parsed.scheme == "tcp":
                    host = parsed.hostname or "127.0.0.1"
                    port_num = parsed.port or 502
                    client = ModbusTcpClient(
                        host,
                        port=port_num,
                        timeout=float(kwargs.get("timeout", 1.0)),
                    )
                else:
                    raise ValueError(f"Unknown transport: {parsed.scheme}")

                connected = client.connect()
                if not connected:
                    logger.warning("Modbus client failed to connect: %s", transport_uri)

                self._buses[key] = {
                    "client": client,
                    "lock": threading.Lock(),
                    "ref_count": 0,
                }
                logger.info("Created Modbus bus: %s", key)

            bus = self._buses[key]
            bus["ref_count"] += 1
            return bus["client"], bus["lock"]

    def release(self, transport_uri: str, **kwargs: object) -> None:
        """Release a reference.  Closes client when ref_count hits 0."""
        with self._mgr_lock:
            key = self._bus_key(transport_uri, **kwargs)
            if key not in self._buses:
                return

            bus = self._buses[key]
            bus["ref_count"] -= 1
            if bus["ref_count"] <= 0:
                try:
                    bus["client"].close()
                except Exception:
                    pass
                del self._buses[key]
                logger.info("Closed Modbus bus: %s", key)
