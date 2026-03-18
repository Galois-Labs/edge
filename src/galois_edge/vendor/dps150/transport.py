"""Serial transport layer for the FNIRSI DPS-150.

Wraps pyserial with a background reader thread that feeds incoming bytes
into a ``PacketBuffer`` and dispatches parsed packets via callback.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import serial

from .protocol import PacketBuffer

log = logging.getLogger(__name__)

PacketCallback = Callable[[int, bytes], None]  # (register, payload)


class SerialTransport:
    """Low-level serial I/O with background packet reader."""

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        rtscts: bool = True,
        inter_command_delay: float = 0.05,
    ) -> None:
        self._port_name = port
        self._baudrate = baudrate
        self._rtscts = rtscts
        self._delay = inter_command_delay
        self._serial: serial.Serial | None = None
        self._packet_buf = PacketBuffer()
        self._callback: PacketCallback | None = None
        self._stop_event = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._write_lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def open(self, callback: PacketCallback | None = None) -> None:
        """Open the serial port and start the background reader."""
        self._callback = callback
        self._serial = serial.Serial(
            port=self._port_name,
            baudrate=self._baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            rtscts=self._rtscts,
            timeout=0.1,
        )
        self._stop_event.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="dps150-reader"
        )
        self._reader_thread.start()

    def close(self) -> None:
        """Stop the reader thread and close the serial port."""
        self._stop_event.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None
        if self._serial is not None and self._serial.is_open:
            self._serial.close()
        self._serial = None

    def send(self, data: bytes) -> None:
        """Write *data* to the serial port with inter-command delay."""
        with self._write_lock:
            if self._serial is not None and self._serial.is_open:
                self._serial.write(data)
                time.sleep(self._delay)

    def _reader_loop(self) -> None:
        """Background thread: read bytes, extract packets, fire callback."""
        while not self._stop_event.is_set():
            try:
                if self._serial is None or not self._serial.is_open:
                    break
                waiting = self._serial.in_waiting
                if waiting > 0:
                    raw = self._serial.read(waiting)
                else:
                    raw = self._serial.read(1)  # blocks up to timeout
                if not raw:
                    continue
                packets = self._packet_buf.feed(raw)
                for _hdr, _cmd, register, payload in packets:
                    if self._callback is not None:
                        try:
                            self._callback(register, payload)
                        except Exception:
                            log.exception("Error in packet callback")
            except serial.SerialException:
                log.exception("Serial read error")
                break
            except Exception:
                log.exception("Unexpected error in reader loop")
