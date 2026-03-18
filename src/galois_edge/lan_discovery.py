"""
LAN instrument discovery for TCPIP-connected SCPI instruments.

Discovers instruments on the local network via:
  1. Static IP:port:protocol list from configuration
  2. Optional mDNS/Zeroconf browsing for LXI devices (``_lxi._tcp``)

Each candidate is TCP-probed before being returned to confirm reachability.
Instruments are addressed using standard TCPIP VISA resource strings
(e.g. ``TCPIP::192.168.1.100::5025::SOCKET``) and communicate through
PyVISA identically to USB-TMC devices.

Key design points:
  - Zeroconf import is GUARDED — discovery works without it (static only)
  - Default port is 5025 (standard SCPI raw socket)
  - Three VISA protocol flavours: SOCKET, INSTR (VXI-11), HISLIP
  - TCP connect probe confirms reachability before returning a resource
"""

import logging
import socket
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol enum
# ---------------------------------------------------------------------------

class TCPIPProtocol(Enum):
    """TCPIP VISA protocol suffixes."""

    SOCKET = "SOCKET"       # Raw TCP socket (most common)
    INSTR = "INSTR"         # VXI-11
    HISLIP = "hislip0"      # HiSLIP


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class LANInstrument:
    """A LAN-connected instrument endpoint."""

    ip: str
    port: int = 5025
    protocol: TCPIPProtocol = TCPIPProtocol.SOCKET

    @property
    def visa_resource(self) -> str:
        """Construct the TCPIP VISA resource string."""
        if self.protocol == TCPIPProtocol.HISLIP:
            return f"TCPIP::{self.ip}::{self.protocol.value}::INSTR"
        if self.protocol == TCPIPProtocol.INSTR:
            return f"TCPIP::{self.ip}::INSTR"
        # SOCKET — include port
        return f"TCPIP::{self.ip}::{self.port}::{self.protocol.value}"


# ---------------------------------------------------------------------------
# Utility helpers (importable by other modules)
# ---------------------------------------------------------------------------

def is_tcpip_resource(visa_address: str) -> bool:
    """Return True when *visa_address* is a TCPIP VISA string."""
    return visa_address.upper().startswith("TCPIP")


def is_tcpip_socket_resource(visa_address: str) -> bool:
    """Return True when *visa_address* is a raw TCP SOCKET resource."""
    return is_tcpip_resource(visa_address) and visa_address.upper().endswith(
        "::SOCKET"
    )


# ---------------------------------------------------------------------------
# Discovery engine
# ---------------------------------------------------------------------------

class LANDiscovery:
    """Discover LAN-connected instruments and produce TCPIP VISA strings.

    Discovery sources
    -----------------
    1. **Static list** — comma-separated ``ip:port:protocol`` entries
       provided in the constructor (typically from an env var).
    2. **mDNS** — optional Zeroconf browse for ``_lxi._tcp`` services.

    Each candidate undergoes a TCP connect probe to confirm reachability
    before being included in the result set.

    Parameters
    ----------
    static_list:
        Comma-separated entries.  Each entry is ``ip[:port[:protocol]]``.
    default_port:
        Port assumed when an entry omits one.
    default_protocol:
        Protocol assumed when an entry omits one (``SOCKET`` | ``INSTR``
        | ``HISLIP``).
    mdns_enabled:
        Whether to browse for ``_lxi._tcp`` mDNS services.
    mdns_timeout:
        Seconds to wait for mDNS responses.
    probe_timeout:
        Seconds for the TCP connect probe.
    """

    def __init__(
        self,
        static_list: str = "",
        default_port: int = 5025,
        default_protocol: str = "SOCKET",
        mdns_enabled: bool = False,
        mdns_timeout: float = 3.0,
        probe_timeout: float = 2.0,
    ):
        self._static_list = static_list
        self._default_port = default_port
        self._default_protocol = self._parse_protocol(default_protocol)
        self._mdns_enabled = mdns_enabled
        self._mdns_timeout = mdns_timeout
        self._probe_timeout = probe_timeout

    # ------------------------------------------------------------------
    # Protocol parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_protocol(value: str) -> TCPIPProtocol:
        """Convert a protocol string to the enum, defaulting to SOCKET."""
        upper = value.strip().upper()
        if upper == "INSTR":
            return TCPIPProtocol.INSTR
        if upper in ("HISLIP", "HISLIP0"):
            return TCPIPProtocol.HISLIP
        return TCPIPProtocol.SOCKET

    # ------------------------------------------------------------------
    # Static list parsing
    # ------------------------------------------------------------------

    def _parse_static_list(self) -> list[LANInstrument]:
        """Parse the static instrument list string.

        Format: ``"ip:port:protocol,ip:port,ip"``

        * *port* defaults to ``default_port``
        * *protocol* defaults to ``default_protocol``

        Examples::

            "192.168.1.100:5025"
            "192.168.1.100:5025:SOCKET,10.0.0.50:5555:INSTR"
            "192.168.1.100"
        """
        if not self._static_list or not self._static_list.strip():
            return []

        instruments: list[LANInstrument] = []
        for entry in self._static_list.split(","):
            entry = entry.strip()
            if not entry:
                continue

            parts = entry.split(":")
            ip = parts[0].strip()
            if not ip:
                continue

            port = self._default_port
            protocol = self._default_protocol

            if len(parts) >= 2 and parts[1].strip():
                try:
                    port = int(parts[1].strip())
                except ValueError:
                    logger.warning(
                        "Invalid port in LAN_INSTRUMENTS entry '%s', "
                        "using default %d",
                        entry,
                        self._default_port,
                    )

            if len(parts) >= 3 and parts[2].strip():
                protocol = self._parse_protocol(parts[2])

            instruments.append(
                LANInstrument(ip=ip, port=port, protocol=protocol)
            )

        return instruments

    # ------------------------------------------------------------------
    # TCP probe
    # ------------------------------------------------------------------

    def _probe_tcp(self, ip: str, port: int) -> bool:
        """Return True if a TCP connection to *ip:port* succeeds."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._probe_timeout)
                sock.connect((ip, port))
                return True
        except (socket.timeout, socket.error, OSError) as exc:
            logger.debug("TCP probe failed for %s:%d: %s", ip, port, exc)
            return False

    # ------------------------------------------------------------------
    # mDNS discovery
    # ------------------------------------------------------------------

    def _discover_mdns(self) -> list[LANInstrument]:
        """Browse for ``_lxi._tcp`` mDNS services on the local network.

        Returns an empty list if the ``zeroconf`` package is not installed.
        """
        try:
            from zeroconf import ServiceBrowser, Zeroconf
        except ImportError:
            logger.debug("zeroconf not installed — mDNS discovery skipped")
            return []

        instruments: list[LANInstrument] = []
        zc = Zeroconf()

        class _Listener:
            """Collect LXI services as they are announced."""

            def add_service(self, zc_inst, service_type, name):
                info = zc_inst.get_service_info(service_type, name)
                if info and info.parsed_addresses():
                    ip = info.parsed_addresses()[0]
                    port = info.port or 5025
                    instruments.append(
                        LANInstrument(
                            ip=ip,
                            port=port,
                            protocol=TCPIPProtocol.SOCKET,
                        )
                    )
                    logger.info(
                        "mDNS: found LXI instrument at %s:%d (%s)",
                        ip,
                        port,
                        name,
                    )

            def remove_service(self, zc_inst, service_type, name):
                pass

            def update_service(self, zc_inst, service_type, name):
                pass

        try:
            ServiceBrowser(zc, "_lxi._tcp.local.", _Listener())
            import time
            time.sleep(self._mdns_timeout)
        except Exception as exc:
            logger.warning("mDNS discovery error: %s", exc)
        finally:
            zc.close()

        return instruments

    # ------------------------------------------------------------------
    # Targeted probe
    # ------------------------------------------------------------------

    def probe_reachable(self, visa_address: str) -> bool:
        """Quick TCP connect probe for a specific VISA address.

        Extracts IP and port from the VISA string and performs a
        connect-only probe. Used by the periodic reconciler to verify
        known LAN instruments are still reachable without running
        the full discover() pipeline.

        Returns True if the instrument is reachable, False otherwise.
        """
        # Parse TCPIP VISA string to extract IP and port
        # Format: TCPIP::ip::port::SOCKET or TCPIP::ip::INSTR
        upper = visa_address.upper()
        if not upper.startswith("TCPIP"):
            return False

        parts = visa_address.split("::")
        if len(parts) < 2:
            return False

        ip = parts[1]
        port = self._default_port

        # Try to extract port from the VISA string
        if len(parts) >= 3:
            try:
                port = int(parts[2])
            except ValueError:
                pass  # Not a port (e.g. "INSTR" or "hislip0")

        return self._probe_tcp(ip, port)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def discover(self) -> list[str]:
        """Run all discovery methods and return reachable VISA strings.

        Deduplicates by VISA resource string and TCP-probes each
        candidate before including it in the result.
        """
        candidates: list[LANInstrument] = []

        # Static list
        candidates.extend(self._parse_static_list())

        # mDNS (optional)
        if self._mdns_enabled:
            candidates.extend(self._discover_mdns())

        # Deduplicate and probe
        seen: set[str] = set()
        results: list[str] = []

        for inst in candidates:
            visa = inst.visa_resource
            if visa in seen:
                continue
            seen.add(visa)

            if self._probe_tcp(inst.ip, inst.port):
                logger.info("LAN instrument reachable: %s", visa)
                results.append(visa)
            else:
                logger.warning(
                    "LAN instrument unreachable: %s:%d", inst.ip, inst.port
                )

        return results
