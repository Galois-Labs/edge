"""
Async gRPC server implementing EdgeDaemonService.

Binds to 127.0.0.1:GRPC_PORT (localhost only -- Go proxy handles external
access via Tailscale). Uses grpc.aio for async handlers and a
ThreadPoolExecutor for blocking VISA calls.

Implements ALL RPCs defined in edge.proto:
  SendCommand, ListInstruments, GetInstrument, ScanInstruments,
  GetCapabilities, ExecuteCommand, ExecuteSequence, StreamMeasurement,
  StopStream, StreamCommands, GetStatus, Ping, GetWebcamSnapshot,
  RegisterEdge, Heartbeat, ProxySDKCall
"""

from __future__ import annotations

import asyncio
import hmac
import importlib
import json
import logging
import platform
import re
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Callable, Dict, Optional, Set

import grpc
from grpc import aio as grpc_aio

from . import edge_pb2
from . import edge_pb2_grpc
from .config import Config
from .instrument_manager import InstrumentManager
from .capability_manager import (
    CapabilityManager,
    InstrumentCapabilities,
    SDKCommandRequest,
)
from .profile_schema import SweepConfig
from .command_handler import CommandHandler
from .scalar_chunker import (
    CHUNK_TRIGGER_MS,
    CHUNK_WINDOW_MS,
    ScalarChunker,
    clamp_sample_period,
)
from .sdk_executor import SDKExecutor
from .waveform_assembly import (
    build_vector_data,
    compose_block_scaling,
    parse_preamble,
    populate_point_vectors,
    vector_data_from_block,
)

logger = logging.getLogger(__name__)


def _parse_channels_param(params: Optional[Dict[str, Any]]) -> list:
    """Parse the additive multi-channel ``channels`` parameter (doc §3.5).

    Request parameters are a ``map<string,string>``, so multi-channel
    acquisition is expressed as a comma-separated list, e.g.
    ``channels: "CHANnel1,CHANnel2"``.  Absent/empty → ``[]`` (the
    single-channel behavior, byte-identical to a request without the
    parameter).
    """
    if not params:
        return []
    raw = str(params.get("channels", "") or "")
    return [c.strip() for c in raw.split(",") if c.strip()]

# ---------------------------------------------------------------------------
# ProxySDKCall module allowlist — only these prefixes may be dynamically
# imported via the fallback path.  The connected-client path (which calls
# methods on already-instantiated SDK objects) is NOT affected.
# ---------------------------------------------------------------------------
_PROXY_SDK_ALLOWED_MODULE_PREFIXES: tuple[str, ...] = (
    "galois_edge.sdk_wrappers.",
    "galois_edge.vendor.",
)


def _is_module_allowed(module_name: str) -> bool:
    """Return True if *module_name* matches the ProxySDKCall allowlist."""
    return any(
        module_name.startswith(prefix)
        for prefix in _PROXY_SDK_ALLOWED_MODULE_PREFIXES
    )


# ---------------------------------------------------------------------------
# Connection type detection
# ---------------------------------------------------------------------------

def _detect_connection_type(address: str) -> edge_pb2.ConnectionType:
    """Infer the ConnectionType enum from a VISA resource string."""
    upper = address.upper()
    if upper.startswith("GPIB"):
        return edge_pb2.CONNECTION_TYPE_GPIB
    if upper.startswith("USB"):
        return edge_pb2.CONNECTION_TYPE_USB
    if upper.startswith("TCPIP"):
        return edge_pb2.CONNECTION_TYPE_LAN
    if upper.startswith("ASRL"):
        return edge_pb2.CONNECTION_TYPE_SERIAL
    return edge_pb2.CONNECTION_TYPE_UNSPECIFIED


# ---------------------------------------------------------------------------
# Protobuf helper builders
# ---------------------------------------------------------------------------

def _build_instrument_proto(
    instrument_id: str,
    visa_address: str,
    idn_response: str,
    is_connected: bool,
    caps: Optional[InstrumentCapabilities] = None,
) -> edge_pb2.Instrument:
    """Build an Instrument protobuf message from local data."""
    manufacturer = ""
    model = ""
    serial_number = ""
    firmware = ""

    # Parse *IDN? response: manufacturer, model, serial, firmware
    if idn_response:
        parts = [p.strip() for p in idn_response.split(",")]
        if len(parts) >= 1:
            manufacturer = parts[0]
        if len(parts) >= 2:
            model = parts[1]
        if len(parts) >= 3:
            serial_number = parts[2]
        if len(parts) >= 4:
            firmware = parts[3]

    profile_name = ""
    instrument_class = ""
    capabilities = []

    if caps is not None:
        manufacturer = caps.manufacturer or manufacturer
        model = caps.model or model
        profile_name = caps.profile_key
        instrument_class = caps.instrument_class

        # Build capability messages for each enabled command
        cap_dict = caps.to_capability_dict()
        for cmd in cap_dict.get("commands", []):
            params = [_param_dict_to_proto(p) for p in cmd.get("parameters", [])]
            capabilities.append(edge_pb2.CommandCapability(
                name=cmd.get("name", ""),
                description=cmd.get("description", ""),
                type=cmd.get("type", ""),
                parameters=params,
                returns_data=bool(cmd.get("return_type")),
                is_dangerous=cmd.get("is_dangerous", False),
                return_type=cmd.get("return_type", ""),
                unit=cmd.get("unit", ""),
                is_streamable=cmd.get("is_streamable", False),
            ))

    return edge_pb2.Instrument(
        id=instrument_id,
        address=visa_address,
        connection_type=_detect_connection_type(visa_address),
        idn_string=idn_response,
        manufacturer=manufacturer,
        model=model,
        serial_number=serial_number,
        firmware=firmware,
        profile_name=profile_name,
        instrument_class=instrument_class,
        is_connected=is_connected,
        capabilities=capabilities,
    )


_PARAM_TYPE_MAP = {
    "string": edge_pb2.PARAMETER_TYPE_STRING,
    "float": edge_pb2.PARAMETER_TYPE_NUMBER,
    "int": edge_pb2.PARAMETER_TYPE_NUMBER,
    "bool": edge_pb2.PARAMETER_TYPE_BOOLEAN,
    "enum": edge_pb2.PARAMETER_TYPE_ENUM,
}


def _param_dict_to_proto(p: dict) -> edge_pb2.CommandParameter:
    """Convert a parameter dict from to_capability_dict() into protobuf."""
    return edge_pb2.CommandParameter(
        name=p.get("name", ""),
        description=p.get("description", ""),
        type=_PARAM_TYPE_MAP.get(p.get("type", "string"), edge_pb2.PARAMETER_TYPE_UNSPECIFIED),
        required=p.get("required", False),
        default_value=str(p["default"]) if p.get("default") is not None else "",
        enum_values=p.get("options") or [],
        unit=p.get("unit", ""),
    )


def _build_capabilities_proto(
    caps: InstrumentCapabilities,
) -> edge_pb2.InstrumentCapabilities:
    """Convert an InstrumentCapabilities record into protobuf."""
    cap_dict = caps.to_capability_dict()

    commands = []
    for cmd in cap_dict.get("commands", []):
        params = [_param_dict_to_proto(p) for p in cmd.get("parameters", [])]
        commands.append(edge_pb2.CommandCapability(
            name=cmd.get("name", ""),
            description=cmd.get("description", ""),
            type=cmd.get("type", ""),
            parameters=params,
            returns_data=bool(cmd.get("return_type")),
            is_dangerous=cmd.get("is_dangerous", False),
            return_type=cmd.get("return_type", ""),
            unit=cmd.get("unit", ""),
            is_streamable=cmd.get("is_streamable", False),
        ))

    sequences = []
    for seq in cap_dict.get("sequences", []):
        seq_param_names = [p.get("name", "") for p in seq.get("parameters", [])]
        sequences.append(edge_pb2.SequenceCapability(
            name=seq.get("name", ""),
            description=seq.get("description", ""),
            params=seq_param_names,
        ))

    settings = cap_dict.get("settings", {})
    settings_map = {k: str(v) for k, v in settings.items()}

    return edge_pb2.InstrumentCapabilities(
        instrument_id=cap_dict.get("instrument_id", ""),
        has_profile=cap_dict.get("has_profile", False),
        profile_key=cap_dict.get("profile_key", ""),
        manufacturer=cap_dict.get("manufacturer", ""),
        model=cap_dict.get("model", ""),
        instrument_class=cap_dict.get("instrument_class", ""),
        commands=commands,
        sequences=sequences,
        settings=settings_map,
    )


# ---------------------------------------------------------------------------
# google.protobuf.Value helpers (for ProxySDKCall)
# ---------------------------------------------------------------------------

def _value_to_python(v: Any) -> Any:
    """Convert a google.protobuf.Value to a Python object."""
    kind = v.WhichOneof("kind")
    if kind == "null_value":
        return None
    if kind == "number_value":
        return v.number_value
    if kind == "string_value":
        return v.string_value
    if kind == "bool_value":
        return v.bool_value
    if kind == "list_value":
        return [_value_to_python(item) for item in v.list_value.values]
    if kind == "struct_value":
        return {
            k: _value_to_python(sv) for k, sv in v.struct_value.fields.items()
        }
    return None


def _python_to_value(obj: Any) -> Any:
    """Convert a Python object to google.protobuf.Value."""
    from google.protobuf import struct_pb2

    v = struct_pb2.Value()
    if obj is None:
        v.null_value = 0
    elif isinstance(obj, bool):
        v.bool_value = obj
    elif isinstance(obj, (int, float)):
        v.number_value = float(obj)
    elif isinstance(obj, str):
        v.string_value = obj
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            v.list_value.values.append(_python_to_value(item))
    elif isinstance(obj, dict):
        for k, val in obj.items():
            v.struct_value.fields[str(k)].CopyFrom(_python_to_value(val))
    else:
        v.string_value = str(obj)
    return v


# ---------------------------------------------------------------------------
# Streaming helpers (seq / chunks — work order §3.6, §5, §7)
# ---------------------------------------------------------------------------


def _build_chunk_point(
    stream_id: str,
    unit: str,
    chunks: list,
    timestamp_ms: int,
    seq: int,
) -> edge_pb2.MeasurementDataPoint:
    """Build a chunk-bearing MeasurementDataPoint from ScalarChunker.take().

    Exclusivity (§7.3): chunk-bearing points carry no value/values (and
    no vectors) — the chunks ARE the payload. One seq per point.
    """
    point = edge_pb2.MeasurementDataPoint(
        stream_id=stream_id,
        timestamp_ms=timestamp_ms,
        unit=unit,
        error="",
        status="ok",
        seq=seq,
    )
    for c in chunks:
        point.chunks.add(
            field=c["field"],
            t0_ms=c["t0_ms"],
            dt_ms=c["dt_ms"],
            n=c["n"],
            y_data=c["y_data"],
            y_dtype=c["y_dtype"],
            y_scale=c["y_scale"],
            y_offset=c["y_offset"],
            t_data=c.get("t_data", b""),
        )
    return point


def _driver_stream_fields(result: Any) -> tuple[float, Dict[str, float]]:
    """Map a protocol driver execute_command() result onto (value, values{})."""
    if isinstance(result, dict):
        values: Dict[str, float] = {}
        for k, v in result.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            values[str(k)] = float(v)
        primary = next(iter(values.values()), 0.0)
        return primary, values
    if isinstance(result, (int, float)) and not isinstance(result, bool):
        return float(result), {}
    try:
        return float(str(result).strip()), {}
    except (TypeError, ValueError):
        return 0.0, {}


# ---------------------------------------------------------------------------
# Servicer implementation
# ---------------------------------------------------------------------------

class EdgeDaemonServicer(edge_pb2_grpc.EdgeDaemonServiceServicer):
    """Async gRPC servicer implementing all EdgeDaemonService RPCs.

    Blocking VISA calls are dispatched to a thread pool to avoid
    blocking the asyncio event loop.
    """

    def __init__(
        self,
        instrument_manager: InstrumentManager,
        command_handler: CommandHandler,
        edge_id: str,
        capability_manager: Optional[CapabilityManager] = None,
        sdk_executor: Optional[SDKExecutor] = None,
        max_workers: int = 10,
        io_executor: Optional[ThreadPoolExecutor] = None,
        driver_registry: Optional[Any] = None,
    ) -> None:
        self._instruments = instrument_manager
        self._handler = command_handler
        self._edge_id = edge_id
        self._capability_manager = capability_manager
        self._sdk_executor = sdk_executor
        self._driver_registry = driver_registry
        # Set by main after the loader is constructed. Bound late because
        # loading the bundled profiles can take minutes on slow storage,
        # and deploy/bind must work during that window.
        self._profile_loader: Optional[Any] = None
        # Use shared single-thread executor for instrument I/O if provided
        # (serializes all GPIB/VISA access — linux-gpib is not thread-safe).
        self._executor = io_executor or ThreadPoolExecutor(max_workers=max_workers)
        self._start_time = time.time()

        # Active measurement streams: stream_id -> asyncio.Task
        self._active_streams: Dict[str, asyncio.Task] = {}

        # Sweep state tracking
        self._active_sweeps: Dict[str, asyncio.Task] = {}
        self._sweep_states: Dict[str, dict] = {}  # sweep_id -> state dict
        self._sweeping_instruments: Set[str] = set()  # instrument reservation gate
        self._sweep_cancel_flags: Dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------
    # Core SCPI
    # ------------------------------------------------------------------

    async def SendCommand(
        self,
        request: edge_pb2.SendCommandRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.SendCommandResponse:
        """Execute a raw SCPI command on an instrument."""
        command_id = request.command_id
        scpi_command = request.scpi_command
        instrument_id = request.instrument_id
        timeout_ms = request.timeout_ms or 5000

        logger.info(
            "SendCommand %s: '%s' -> %s",
            command_id, scpi_command, instrument_id,
        )

        start = time.time()

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                self._executor,
                self._handler.execute_command,
                scpi_command,
                instrument_id,
                timeout_ms,
                command_id,
            )

            elapsed_ms = int((time.time() - start) * 1000)

            if result["success"]:
                return edge_pb2.SendCommandResponse(
                    command_id=command_id,
                    response=result["response"],
                    error="",
                    status="completed",
                    execution_time_ms=elapsed_ms,
                )
            else:
                return edge_pb2.SendCommandResponse(
                    command_id=command_id,
                    response="",
                    error=result["error"],
                    status="error",
                    execution_time_ms=elapsed_ms,
                )

        except Exception as exc:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.error("SendCommand %s exception: %s", command_id, exc)
            return edge_pb2.SendCommandResponse(
                command_id=command_id,
                response="",
                error=str(exc),
                status="error",
                execution_time_ms=elapsed_ms,
            )

    async def StreamCommands(
        self,
        request_iterator: AsyncIterator[edge_pb2.SendCommandRequest],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[edge_pb2.SendCommandResponse]:
        """Bidirectional streaming of SCPI commands."""
        async for request in request_iterator:
            response = await self.SendCommand(request, context)
            yield response

    # ------------------------------------------------------------------
    # Instrument discovery
    # ------------------------------------------------------------------

    async def ListInstruments(
        self,
        request: edge_pb2.ListInstrumentsRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.ListInstrumentsResponse:
        """Return all instruments currently known to the daemon.

        Returns cached state only — does NOT trigger a fresh resource scan.
        This avoids blocking on the single-thread I/O executor while
        background discovery is running.
        """
        logger.info("ListInstruments request (filter=%s)", request.filter)

        try:
            # Read cached capability records (pure Python dict — no C library calls)
            instruments = []
            if self._capability_manager:
                for visa_address, caps in self._capability_manager.all_instruments.items():
                    idn = caps.idn_response if caps else ""
                    instruments.append(
                        _build_instrument_proto(
                            instrument_id=visa_address,
                            visa_address=visa_address,
                            idn_response=idn or "",
                            is_connected=self._instruments.is_connected(visa_address),
                            caps=caps,
                        )
                    )

            logger.info("ListInstruments: %d instrument(s)", len(instruments))
            return edge_pb2.ListInstrumentsResponse(
                instruments=instruments,
                edge_id=self._edge_id,
            )

        except Exception as exc:
            logger.error("ListInstruments error: %s", exc)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return edge_pb2.ListInstrumentsResponse(edge_id=self._edge_id)

    async def GetInstrument(
        self,
        request: edge_pb2.GetInstrumentRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.Instrument:
        """Return details for a single instrument."""
        instrument_id = request.instrument_id
        logger.info("GetInstrument: %s", instrument_id)

        connected = self._instruments.is_connected(instrument_id)
        if not connected:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Instrument not found: {instrument_id}")
            return edge_pb2.Instrument()

        idn = ""
        try:
            loop = asyncio.get_running_loop()
            idn = await loop.run_in_executor(
                self._executor,
                self._instruments.identify,
                instrument_id,
            )
        except Exception:
            pass

        caps = None
        if self._capability_manager:
            caps = self._capability_manager.get_instrument_caps(instrument_id)

        return _build_instrument_proto(
            instrument_id=instrument_id,
            visa_address=instrument_id,
            idn_response=idn,
            is_connected=True,
            caps=caps,
        )

    async def ScanInstruments(
        self,
        request: edge_pb2.ScanInstrumentsRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.ScanInstrumentsResponse:
        """Trigger a full rescan and return discovered instruments."""
        logger.info("ScanInstruments: triggering rescan")

        try:
            loop = asyncio.get_running_loop()
            resources = await loop.run_in_executor(
                self._executor,
                self._instruments.rescan_all,
            )

            instruments = []
            for visa_address in resources:
                connected = self._instruments.is_connected(visa_address)
                idn = ""
                if connected:
                    try:
                        idn = await loop.run_in_executor(
                            self._executor,
                            self._instruments.identify,
                            visa_address,
                        )
                    except Exception:
                        pass

                caps = None
                if self._capability_manager:
                    caps = self._capability_manager.get_instrument_caps(
                        visa_address
                    )

                instruments.append(
                    _build_instrument_proto(
                        instrument_id=visa_address,
                        visa_address=visa_address,
                        idn_response=idn,
                        is_connected=connected,
                        caps=caps,
                    )
                )

            logger.info("ScanInstruments: found %d", len(instruments))
            return edge_pb2.ScanInstrumentsResponse(instruments=instruments)

        except Exception as exc:
            logger.error("ScanInstruments error: %s", exc)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(exc))
            return edge_pb2.ScanInstrumentsResponse()

    # ------------------------------------------------------------------
    # Profile-based commands
    # ------------------------------------------------------------------

    async def GetCapabilities(
        self,
        request: edge_pb2.GetCapabilitiesRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.GetCapabilitiesResponse:
        """Return command capabilities for one or all instruments."""
        logger.info(
            "GetCapabilities: instrument_id=%s, class=%s",
            request.instrument_id,
            request.instrument_class,
        )

        if not self._capability_manager:
            return edge_pb2.GetCapabilitiesResponse(
                capabilities=[],
                edge_id=self._edge_id,
            )

        capabilities = []

        if request.instrument_id:
            caps = self._capability_manager.get_instrument_caps(
                request.instrument_id
            )
            if caps:
                capabilities.append(_build_capabilities_proto(caps))
        elif request.instrument_class:
            for caps in self._capability_manager.find_by_class(
                request.instrument_class
            ):
                capabilities.append(_build_capabilities_proto(caps))
        else:
            all_caps = self._capability_manager.get_all_capabilities_list()
            for cap_dict in all_caps:
                # We need the raw InstrumentCapabilities objects for the
                # protobuf builder; get_all_capabilities_list returns dicts,
                # so we'll iterate the internal instruments directly.
                pass
            # Iterate raw records instead
            capabilities = []
            for inst_id in list(
                self._capability_manager._instruments.keys()
            ):
                caps = self._capability_manager.get_instrument_caps(inst_id)
                if caps:
                    capabilities.append(_build_capabilities_proto(caps))

        return edge_pb2.GetCapabilitiesResponse(
            capabilities=capabilities,
            edge_id=self._edge_id,
        )

    async def _execute_vector_command(
        self,
        loop: asyncio.AbstractEventLoop,
        dispatch: str,
        instrument_id: str,
        command_id: str,
        timeout_ms: int,
        cmd_config: Any,
        start: float,
    ) -> edge_pb2.ExecuteCommandResponse:
        """Handle a vector/binary query command and return a VectorData response.

        This is called from ``ExecuteCommand`` when ``returns.type == "vector"``.
        It reads IEEE 488.2 binary block data, optionally fetches x-axis
        metadata via separate SCPI queries, and packs everything into a
        ``VectorData`` protobuf message.
        """
        import struct as _struct

        returns = cmd_config.returns
        format_str = returns.format or "ieee_binary"

        # Determine datatype from format hint
        if "float32" in format_str:
            datatype = 'f'
            dtype_label = "float32"
            struct_fmt_char = 'f'
        elif "int16" in format_str:
            datatype = 'h'
            dtype_label = "int16"
            struct_fmt_char = 'h'
        else:
            # Default: float64
            datatype = 'd'
            dtype_label = "float64"
            struct_fmt_char = 'd'

        is_big_endian = "big_endian" in format_str

        # Execute the binary query
        result = await loop.run_in_executor(
            self._executor,
            lambda: self._handler.execute_binary_query(
                scpi_cmd=dispatch,
                instrument_id=instrument_id,
                datatype=datatype,
                is_big_endian=is_big_endian,
                timeout_ms=timeout_ms,
            ),
        )

        if not result["success"]:
            elapsed_ms = int((time.time() - start) * 1000)
            return edge_pb2.ExecuteCommandResponse(
                command_id=command_id,
                success=False,
                data="",
                error_message=result.get("error", "Binary query failed"),
                execution_time_ms=elapsed_ms,
                scpi_command=dispatch,
            )

        y_values = result["data"]

        # Fetch x-axis metadata if queries are defined
        x_start = 0.0
        x_increment = 1.0

        if returns.x_start_query:
            x_result = await loop.run_in_executor(
                self._executor,
                lambda: self._handler.execute_command(
                    scpi_cmd=returns.x_start_query,
                    instrument_id=instrument_id,
                    force_query=True,
                    timeout_ms=timeout_ms,
                ),
            )
            if x_result.get("success"):
                try:
                    x_start = float(x_result["response"])
                except (ValueError, KeyError):
                    pass

        if returns.x_increment_query:
            x_result = await loop.run_in_executor(
                self._executor,
                lambda: self._handler.execute_command(
                    scpi_cmd=returns.x_increment_query,
                    instrument_id=instrument_id,
                    force_query=True,
                    timeout_ms=timeout_ms,
                ),
            )
            if x_result.get("success"):
                try:
                    x_increment = float(x_result["response"])
                except (ValueError, KeyError):
                    pass

        # Pack y-values into bytes. The samples were already decoded to
        # Python numbers by execute_binary_query (instrument byte order
        # handled there); the wire is ALWAYS little-endian (doc §3.0).
        y_data = _struct.pack(
            f'<{len(y_values)}{struct_fmt_char}',
            *y_values,
        )

        vector_data = edge_pb2.VectorData(
            y_data=y_data,
            y_dtype=dtype_label,
            y_length=len(y_values),
            x_start=x_start,
            x_increment=x_increment,
            x_unit=returns.x_unit or "",
            y_unit=returns.unit or "",
            x_name=returns.x_name or "",
            # Pre-scaled physical samples: y_scale is EXPLICITLY 1.0 —
            # never the proto3 zero-default (doc §3.0).
            y_scale=1.0,
            y_offset=0.0,
        )

        elapsed_ms = int((time.time() - start) * 1000)
        return edge_pb2.ExecuteCommandResponse(
            command_id=command_id,
            success=True,
            data="",
            vector_data=vector_data,
            error_message="",
            execution_time_ms=elapsed_ms,
            scpi_command=dispatch,
        )

    async def _execute_waveform_assembly(
        self,
        loop: asyncio.AbstractEventLoop,
        instrument_id: str,
        command_id: str,
        timeout_ms: int,
        cmd_config: Any,
        params: Optional[Dict[str, Any]],
        start: float,
    ) -> edge_pb2.ExecuteCommandResponse:
        """Execute a waveform assembly command.

        This handles the full SCPI oscilloscope waveform acquisition
        pipeline: set source channel, set format, query preamble, query
        binary data, decode, scale, and package as VectorData.

        Delegates to ``_execute_waveform_assembly_for_stream`` so the
        one-shot and streaming paths share one (reference-composed,
        doc §2.4) scaling pipeline; failures become ``success:false``
        responses (doc §2.2 rule 4), never a crash.
        """
        wf_config = cmd_config.waveform_assembly
        scpi_label = f"{wf_config.preamble_query} + {wf_config.data_query}"

        try:
            vector_data = await self._execute_waveform_assembly_for_stream(
                loop=loop,
                instrument_id=instrument_id,
                timeout_ms=timeout_ms,
                cmd_config=cmd_config,
                params=params,
            )
        except Exception as exc:
            elapsed_ms = int((time.time() - start) * 1000)
            return edge_pb2.ExecuteCommandResponse(
                command_id=command_id,
                success=False,
                data="",
                error_message=str(exc),
                execution_time_ms=elapsed_ms,
                scpi_command=scpi_label,
            )

        elapsed_ms = int((time.time() - start) * 1000)
        return edge_pb2.ExecuteCommandResponse(
            command_id=command_id,
            success=True,
            data="",
            vector_data=vector_data,
            error_message="",
            execution_time_ms=elapsed_ms,
            scpi_command=scpi_label,
        )

    async def _execute_waveform_assembly_for_stream(
        self,
        loop: asyncio.AbstractEventLoop,
        instrument_id: str,
        timeout_ms: int,
        cmd_config: Any,
        params: Optional[Dict[str, Any]],
        channel_label: str = "",
    ) -> edge_pb2.VectorData:
        """Execute waveform assembly and return just the VectorData.

        Used by StreamMeasurement to produce VectorData for each poll
        iteration (one call per channel for multi-channel frames — the
        preamble is re-read after every source switch, doc §3.5) and by
        ``_execute_waveform_assembly`` for one-shots.  Raises on failure.
        """
        import struct as _struct

        wf_config = cmd_config.waveform_assembly
        channel = "CHANnel1"
        if params and "channel" in params:
            channel = params["channel"]

        # Set source
        source_cmd = wf_config.source_command.format(channel=channel)
        await loop.run_in_executor(
            self._executor,
            lambda: self._handler.execute_command(
                scpi_cmd=source_cmd,
                instrument_id=instrument_id,
                timeout_ms=timeout_ms,
            ),
        )

        # Set format
        await loop.run_in_executor(
            self._executor,
            lambda: self._handler.execute_command(
                scpi_cmd=wf_config.format_command,
                instrument_id=instrument_id,
                timeout_ms=timeout_ms,
            ),
        )

        # Query preamble
        preamble_result = await loop.run_in_executor(
            self._executor,
            lambda: self._handler.execute_command(
                scpi_cmd=wf_config.preamble_query,
                instrument_id=instrument_id,
                timeout_ms=timeout_ms,
                force_query=True,
            ),
        )
        if not preamble_result["success"]:
            raise RuntimeError(
                f"Preamble query failed: {preamble_result.get('error', '')}"
            )

        preamble_str = preamble_result["response"].strip()
        preamble = parse_preamble(preamble_str)

        # Query binary data
        if wf_config.data_format == "word":
            datatype = 'h'
        elif wf_config.data_format == "float32":
            datatype = 'f'
        else:
            datatype = 'B'

        data_result = await loop.run_in_executor(
            self._executor,
            lambda: self._handler.execute_binary_query(
                scpi_cmd=wf_config.data_query,
                instrument_id=instrument_id,
                datatype=datatype,
                is_big_endian=wf_config.big_endian,
                timeout_ms=timeout_ms,
            ),
        )
        if not data_result["success"]:
            raise RuntimeError(
                f"Waveform data query failed: {data_result.get('error', '')}"
            )

        # Reference-point composition (doc §2.4) via the same helper the
        # byte-safe block path uses — never hand-rolled arithmetic:
        #   x_start  = xorigin - xreference * xincrement
        #   y_offset = yorigin - yreference * yincrement
        scaling = compose_block_scaling({
            "x_start": preamble.xorigin,
            "x_increment": preamble.xincrement,
            "x_reference": preamble.xreference,
            "y_scale": preamble.yincrement,
            "y_offset": preamble.yorigin,
            "y_reference": preamble.yreference,
        })

        raw_samples = data_result["data"]
        voltages = [
            sample * scaling["y_scale"] + scaling["y_offset"]
            for sample in raw_samples
        ]

        y_data = _struct.pack(f'<{len(voltages)}d', *voltages)

        return build_vector_data(
            y_data=y_data,
            y_dtype="float64",
            x_start=scaling["x_start"],
            x_increment=scaling["x_increment"],
            x_unit=wf_config.x_unit,
            y_unit=wf_config.y_unit,
            x_name="Time",
            # Samples are pre-scaled physical values: y_scale is
            # EXPLICITLY 1.0 (never the proto3 zero-default — doc §3.0).
            y_scale=1.0,
            y_offset=0.0,
            channel=channel_label,
        )

    # ------------------------------------------------------------------
    # IEEE 488.2 definite-length block path (returns.type == binary)
    # ------------------------------------------------------------------

    async def _read_ieee_block_vector(
        self,
        loop: asyncio.AbstractEventLoop,
        dispatch: str,
        instrument_id: str,
        timeout_ms: int,
        returns_cfg: Any,
        binary_config: Any,
        preamble_scpi: Optional[str],
        channel: str,
        command_id: Optional[str] = None,
    ) -> tuple:
        """Run one byte-safe block read and build its ``VectorData``.

        Routes through ``CommandHandler.execute_binary_block_query`` →
        ``InstrumentManager.query_raw`` — binary never touches the text
        ``query()``/``execute_command`` path (doc §2.1).

        Returns:
            ``(vector, None)`` on success, ``(None, error_message)`` on
            any failure (malformed block, preamble error, transport
            error) — never raises for a bad read (doc §2.2 rule 4).
        """
        result = await loop.run_in_executor(
            self._executor,
            lambda: self._handler.execute_binary_block_query(
                scpi_cmd=dispatch,
                instrument_id=instrument_id,
                binary_config=binary_config,
                preamble_scpi=preamble_scpi,
                timeout_ms=timeout_ms,
                command_id=command_id,
            ),
        )
        if not result["success"]:
            return None, result.get("error") or "Binary block query failed"

        try:
            vector = vector_data_from_block(
                result["block"],
                x_unit=returns_cfg.x_unit or "s",
                y_unit=returns_cfg.unit or "V",
                x_name=returns_cfg.x_name or "Time",
                channel=channel,
            )
        except ValueError as exc:
            # Never emit a partially valid vector (doc §2.2 rule 4).
            return None, str(exc)
        return vector, None

    async def _read_ieee_block_frame(
        self,
        loop: asyncio.AbstractEventLoop,
        dispatch: str,
        instrument_id: str,
        timeout_ms: int,
        returns_cfg: Any,
        binary_config: Any,
        preamble_scpi: Optional[str],
        channels: list,
        source_scpi: Dict[str, str],
        fallback_channel: str,
    ) -> tuple:
        """Read one tick's worth of channels for an ``ieee_block`` command.

        For multi-channel frames (doc §3.5) each channel is selected via
        its resolved ``binary.source_command`` SCPI, then the preamble +
        block are read for THAT channel — per-channel preambles, never a
        shared one. With no ``channels`` parameter this is a single
        read, byte-identical to the single-channel behavior.

        Returns:
            ``(vectors, None)`` on success, ``([], error_message)`` on
            the first failed channel.
        """
        vectors = []
        for ch in (channels or [None]):
            if ch is not None and source_scpi.get(ch):
                scpi = source_scpi[ch]
                src_result = await loop.run_in_executor(
                    self._executor,
                    lambda s=scpi: self._handler.execute_command(
                        scpi_cmd=s,
                        instrument_id=instrument_id,
                        timeout_ms=timeout_ms,
                    ),
                )
                if not src_result["success"]:
                    return [], (
                        f"Source select '{scpi}' failed: "
                        f"{src_result.get('error', '')}"
                    )
            vector, error = await self._read_ieee_block_vector(
                loop=loop,
                dispatch=dispatch,
                instrument_id=instrument_id,
                timeout_ms=timeout_ms,
                returns_cfg=returns_cfg,
                binary_config=binary_config,
                preamble_scpi=preamble_scpi,
                channel=ch if ch is not None else fallback_channel,
            )
            if error is not None:
                return [], error
            vectors.append(vector)
        return vectors, None

    async def _execute_ieee_block_command(
        self,
        loop: asyncio.AbstractEventLoop,
        dispatch: str,
        instrument_id: str,
        command_id: str,
        timeout_ms: int,
        cmd_config: Any,
        params: Optional[Dict[str, Any]],
        caps: Any,
        start: float,
    ) -> edge_pb2.ExecuteCommandResponse:
        """One-shot ``returns: {type: binary, format: ieee_block}`` command.

        Malformed blocks produce ``success:false`` + ``error_message``
        (doc §2.2 rule 4); the response carries ``vector_data`` with the
        §2.4 reference-point composition applied by the command handler.
        """
        returns_cfg = cmd_config.returns
        binary_config = returns_cfg.effective_binary
        profile = caps.profile if caps else None

        channels = _parse_channels_param(params)
        if len(channels) > 1:
            # ExecuteCommandResponse carries a single vector_data (field
            # 7); multi-channel frames (vectors[], doc §3.5) exist only
            # on MeasurementDataPoint. Fail loudly instead of silently
            # dropping channels.
            elapsed_ms = int((time.time() - start) * 1000)
            return edge_pb2.ExecuteCommandResponse(
                command_id=command_id,
                success=False,
                data="",
                error_message=(
                    "multi-channel acquisition ('channels' parameter) is "
                    "stream-only: ExecuteCommandResponse carries a single "
                    "vector_data — use StreamMeasurement for vectors[] "
                    "frames"
                ),
                execution_time_ms=elapsed_ms,
                scpi_command=dispatch,
            )

        channel = ""
        if params:
            channel = str(params.get("channel") or params.get("source") or "")

        preamble_scpi = None
        if binary_config.preamble_command and profile is not None:
            preamble_scpi = profile.resolve_scpi_ref(
                binary_config.preamble_command, params
            )

        if channels:
            channel = channels[0]
            if binary_config.source_command and profile is not None:
                source_scpi = profile.resolve_source_ref(
                    binary_config.source_command, channel
                )
                src_result = await loop.run_in_executor(
                    self._executor,
                    lambda: self._handler.execute_command(
                        scpi_cmd=source_scpi,
                        instrument_id=instrument_id,
                        timeout_ms=timeout_ms,
                    ),
                )
                if not src_result["success"]:
                    elapsed_ms = int((time.time() - start) * 1000)
                    return edge_pb2.ExecuteCommandResponse(
                        command_id=command_id,
                        success=False,
                        data="",
                        error_message=(
                            f"Source select '{source_scpi}' failed: "
                            f"{src_result.get('error', '')}"
                        ),
                        execution_time_ms=elapsed_ms,
                        scpi_command=dispatch,
                    )

        vector, error = await self._read_ieee_block_vector(
            loop=loop,
            dispatch=dispatch,
            instrument_id=instrument_id,
            timeout_ms=timeout_ms,
            returns_cfg=returns_cfg,
            binary_config=binary_config,
            preamble_scpi=preamble_scpi,
            channel=channel,
            command_id=command_id,
        )

        elapsed_ms = int((time.time() - start) * 1000)
        if error is not None:
            return edge_pb2.ExecuteCommandResponse(
                command_id=command_id,
                success=False,
                data="",
                error_message=error,
                execution_time_ms=elapsed_ms,
                scpi_command=dispatch,
            )
        return edge_pb2.ExecuteCommandResponse(
            command_id=command_id,
            success=True,
            data="",
            vector_data=vector,
            error_message="",
            execution_time_ms=elapsed_ms,
            scpi_command=dispatch,
        )

    def _apply_response_processing(
        self,
        raw_response: str,
        instrument_id: str,
        command_name: str,
    ) -> str:
        """Apply response_parser from profile ReturnConfig to raw instrument response."""
        if not self._capability_manager:
            return raw_response
        caps = self._capability_manager.get_instrument_caps(instrument_id)
        if not caps:
            return raw_response
        cmd = caps.get_command(command_name)
        if cmd and cmd.returns:
            return cmd.returns.parse_response(raw_response)
        return raw_response

    async def ExecuteCommand(
        self,
        request: edge_pb2.ExecuteCommandRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.ExecuteCommandResponse:
        """Execute a named command from an instrument's profile."""
        command_id = request.command_id
        instrument_id = request.instrument_id
        command_name = request.command_name
        timeout_ms = request.timeout_ms or 5000

        logger.info(
            "ExecuteCommand %s: '%s' -> %s",
            command_id, command_name, instrument_id,
        )

        start = time.time()

        if not self._capability_manager:
            return edge_pb2.ExecuteCommandResponse(
                command_id=command_id,
                success=False,
                data="",
                error_message="Profile system not available",
                execution_time_ms=0,
                scpi_command="",
            )

        # Check if this is a protocol driver instrument (Modbus, etc.)
        if self._capability_manager:
            protocol_driver = self._capability_manager.get_protocol_driver(instrument_id)
            if protocol_driver is not None:
                try:
                    params = dict(request.parameters) if request.parameters else {}
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(
                        self._executor,
                        protocol_driver.execute_command,
                        command_name,
                        params,
                    )
                    elapsed_ms = int((time.time() - start) * 1000)
                    return edge_pb2.ExecuteCommandResponse(
                        command_id=command_id,
                        success=True,
                        data=json.dumps(result) if not isinstance(result, str) else result,
                        error_message="",
                        execution_time_ms=elapsed_ms,
                        scpi_command=f"[modbus:{command_name}]",
                    )
                except Exception as exc:
                    elapsed_ms = int((time.time() - start) * 1000)
                    logger.error(
                        "Protocol driver command '%s' failed for %s: %s",
                        command_name, instrument_id, exc,
                    )
                    return edge_pb2.ExecuteCommandResponse(
                        command_id=command_id,
                        success=False,
                        data="",
                        error_message=str(exc),
                        execution_time_ms=elapsed_ms,
                        scpi_command="",
                    )

        # Resolve the command to SCPI string or SDKCommandRequest
        params = dict(request.parameters) if request.parameters else None
        dispatch = self._capability_manager.resolve_command(
            instrument_id=instrument_id,
            command_name=command_name,
            params=params,
            is_query=request.is_query,
        )

        if dispatch is None:
            elapsed_ms = int((time.time() - start) * 1000)
            return edge_pb2.ExecuteCommandResponse(
                command_id=command_id,
                success=False,
                data="",
                error_message=(
                    f"Command '{command_name}' not found or disabled "
                    f"for {instrument_id}"
                ),
                execution_time_ms=elapsed_ms,
                scpi_command="",
            )

        # Look up force_query from profile
        caps = self._capability_manager.get_instrument_caps(instrument_id)
        cmd_config = caps.get_command(command_name) if caps else None
        profile_force_query = cmd_config.force_query if cmd_config else False

        # Safety interlock: reject commands that require sweep
        if cmd_config and cmd_config.requires_sweep:
            elapsed_ms = int((time.time() - start) * 1000)
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            msg = (
                f"Command '{command_name}' requires sweep for safety. "
                f"Use StartSweep RPC with an explicit sweep_rate instead of ExecuteCommand."
            )
            context.set_details(msg)
            return edge_pb2.ExecuteCommandResponse(
                command_id=command_id,
                success=False,
                data="",
                error_message=msg,
                execution_time_ms=elapsed_ms,
                scpi_command="",
            )

        # Reservation gate: reject writes if instrument is sweeping
        if instrument_id in self._sweeping_instruments:
            is_query = request.is_query or (cmd_config.force_query if cmd_config else False)
            if not is_query:
                elapsed_ms = int((time.time() - start) * 1000)
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                msg = f"Instrument '{instrument_id}' is currently sweeping. Write commands are blocked."
                context.set_details(msg)
                return edge_pb2.ExecuteCommandResponse(
                    command_id=command_id,
                    success=False,
                    data="",
                    error_message=msg,
                    execution_time_ms=elapsed_ms,
                    scpi_command="",
                )

        try:
            loop = asyncio.get_running_loop()

            if isinstance(dispatch, SDKCommandRequest):
                # SDK path
                if not self._sdk_executor:
                    raise ValueError("SDK executor not available")
                result = await loop.run_in_executor(
                    self._executor,
                    lambda: self._sdk_executor.execute(
                        instrument_id=instrument_id,
                        command_name=dispatch.command_name,
                        params=dispatch.params,
                        sdk_call=dispatch.sdk_call,
                        is_query=dispatch.is_query,
                    ),
                )
                sdk_method = (
                    dispatch.sdk_call.method
                    or dispatch.sdk_call.getter
                    or ""
                )
                scpi_label = f"SDK:{sdk_method}"
            else:
                # SCPI path -- dispatch is the formatted SCPI string
                scpi_label = dispatch

                # --- Waveform assembly path (SCPI oscilloscopes) ---
                if cmd_config and cmd_config.waveform_assembly:
                    return await self._execute_waveform_assembly(
                        loop=loop,
                        instrument_id=instrument_id,
                        command_id=command_id,
                        timeout_ms=timeout_ms,
                        cmd_config=cmd_config,
                        params=params,
                        start=start,
                    )

                # --- IEEE 488.2 definite-length block path ---
                # returns.type == binary must go through the raw byte
                # path: the text query() path corrupts arbitrary bytes
                # at decode and terminates early on any 0x0A payload
                # byte (doc §2.1).
                if (
                    cmd_config
                    and cmd_config.returns
                    and cmd_config.returns.is_ieee_block
                ):
                    return await self._execute_ieee_block_command(
                        loop=loop,
                        dispatch=dispatch,
                        instrument_id=instrument_id,
                        command_id=command_id,
                        timeout_ms=timeout_ms,
                        cmd_config=cmd_config,
                        params=params,
                        caps=caps,
                        start=start,
                    )

                # --- Vector / binary query path ---
                if (
                    cmd_config
                    and cmd_config.returns
                    and cmd_config.returns.type == "vector"
                ):
                    return await self._execute_vector_command(
                        loop=loop,
                        dispatch=dispatch,
                        instrument_id=instrument_id,
                        command_id=command_id,
                        timeout_ms=timeout_ms,
                        cmd_config=cmd_config,
                        start=start,
                    )

                # --- Normal scalar SCPI path ---
                result = await loop.run_in_executor(
                    self._executor,
                    lambda: self._handler.execute_command(
                        scpi_cmd=dispatch,
                        instrument_id=instrument_id,
                        timeout_ms=timeout_ms,
                        command_id=command_id,
                        force_query=request.is_query or profile_force_query,
                    ),
                )

            elapsed_ms = int((time.time() - start) * 1000)

            if result["success"]:
                # Apply response parser from profile
                response_data = self._apply_response_processing(
                    result["response"], instrument_id, command_name
                )
                return edge_pb2.ExecuteCommandResponse(
                    command_id=command_id,
                    success=True,
                    data=response_data,
                    error_message="",
                    execution_time_ms=elapsed_ms,
                    scpi_command=str(scpi_label),
                )
            else:
                return edge_pb2.ExecuteCommandResponse(
                    command_id=command_id,
                    success=False,
                    data="",
                    error_message=result["error"],
                    execution_time_ms=elapsed_ms,
                    scpi_command=str(scpi_label),
                )

        except Exception as exc:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.error("ExecuteCommand %s exception: %s", command_id, exc)
            return edge_pb2.ExecuteCommandResponse(
                command_id=command_id,
                success=False,
                data="",
                error_message=str(exc),
                execution_time_ms=elapsed_ms,
                scpi_command="",
            )

    async def ExecuteSequence(
        self,
        request: edge_pb2.ExecuteSequenceRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.ExecuteSequenceResponse:
        """Run a multi-step measurement sequence on an instrument."""
        sequence_id = request.sequence_id
        instrument_id = request.instrument_id
        sequence_name = request.sequence_name
        timeout_ms = request.timeout_ms or 30000

        logger.info(
            "ExecuteSequence %s: '%s' -> %s",
            sequence_id, sequence_name, instrument_id,
        )

        start = time.time()

        if not self._capability_manager:
            return edge_pb2.ExecuteSequenceResponse(
                sequence_id=sequence_id,
                result="",
                error="Profile system not available",
                status="error",
                execution_time_ms=0,
                steps_executed=[],
            )

        # Get the instrument capabilities
        caps = self._capability_manager.get_instrument_caps(instrument_id)
        if caps is None:
            elapsed_ms = int((time.time() - start) * 1000)
            return edge_pb2.ExecuteSequenceResponse(
                sequence_id=sequence_id,
                result="",
                error=f"Instrument not found: {instrument_id}",
                status="error",
                execution_time_ms=elapsed_ms,
                steps_executed=[],
            )

        seq_config = caps.get_sequence(sequence_name)
        if seq_config is None:
            elapsed_ms = int((time.time() - start) * 1000)
            return edge_pb2.ExecuteSequenceResponse(
                sequence_id=sequence_id,
                result="",
                error=f"Sequence '{sequence_name}' not found or disabled",
                status="error",
                execution_time_ms=elapsed_ms,
                steps_executed=[],
            )

        # Build params from request
        params = dict(request.parameters) if request.parameters else {}

        steps_executed = []
        captured_values: Dict[str, str] = {}
        final_result = ""
        loop = asyncio.get_running_loop()

        try:
            for step in seq_config.steps:
                # Determine per-step timeout
                step_count = len(seq_config.steps) or 1
                step_timeout = timeout_ms // step_count

                if step.command:
                    cmd = caps.get_command(step.command)
                    if not cmd:
                        raise ValueError(
                            f"Command '{step.command}' not found in profile"
                        )

                    # Substitute parameters in step args
                    step_args: Dict[str, Any] = {}
                    if step.args:
                        for k, v in step.args.items():
                            val = v
                            for pk, pv in params.items():
                                val = val.replace(f"{{{pk}}}", str(pv))
                            for ck, cv in captured_values.items():
                                val = val.replace(f"{{{ck}}}", str(cv))
                            step_args[k] = val

                    if cmd.is_sdk_command:
                        if not self._sdk_executor:
                            raise ValueError("SDK executor not available")
                        result = await loop.run_in_executor(
                            self._executor,
                            lambda c=cmd, sa=step_args: self._sdk_executor.execute(
                                instrument_id=instrument_id,
                                command_name=step.command,
                                params=sa or None,
                                sdk_call=c.sdk_call,
                                is_query=True,
                            ),
                        )
                        steps_executed.append(
                            f"SDK:{cmd.sdk_call.method or step.command}"
                        )
                    else:
                        scpi_str = cmd.format_scpi(step_args or None)
                        result = await loop.run_in_executor(
                            self._executor,
                            lambda s=scpi_str, t=step_timeout: self._handler.execute_command(
                                scpi_cmd=s,
                                instrument_id=instrument_id,
                                timeout_ms=t,
                            ),
                        )
                        steps_executed.append(scpi_str)
                elif step.scpi:
                    # Direct SCPI
                    scpi_cmd = step.scpi
                    for pk, pv in params.items():
                        scpi_cmd = scpi_cmd.replace(f"{{{pk}}}", str(pv))
                    for ck, cv in captured_values.items():
                        scpi_cmd = scpi_cmd.replace(f"{{{ck}}}", str(cv))

                    result = await loop.run_in_executor(
                        self._executor,
                        lambda s=scpi_cmd, t=step_timeout: self._handler.execute_command(
                            scpi_cmd=s,
                            instrument_id=instrument_id,
                            timeout_ms=t,
                        ),
                    )
                    steps_executed.append(scpi_cmd)
                else:
                    continue

                if not result["success"]:
                    raise ValueError(f"Step failed: {result['error']}")

                # Capture result if step specifies capture_as
                capture_key = getattr(step, "capture", None) or getattr(
                    step, "capture_as", None
                )
                if capture_key:
                    captured_values[capture_key] = result["response"]

            # Determine final result
            returns_key = getattr(seq_config, "returns", None)
            if returns_key and returns_key in captured_values:
                final_result = captured_values[returns_key]

            elapsed_ms = int((time.time() - start) * 1000)
            return edge_pb2.ExecuteSequenceResponse(
                sequence_id=sequence_id,
                result=final_result,
                error="",
                status="completed",
                execution_time_ms=elapsed_ms,
                steps_executed=steps_executed,
            )

        except Exception as exc:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.error("ExecuteSequence %s error: %s", sequence_id, exc)
            return edge_pb2.ExecuteSequenceResponse(
                sequence_id=sequence_id,
                result="",
                error=str(exc),
                status="error",
                execution_time_ms=elapsed_ms,
                steps_executed=steps_executed,
            )

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def StreamMeasurement(
        self,
        request: edge_pb2.StreamMeasurementRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[edge_pb2.MeasurementDataPoint]:
        """Stream continuous measurements from an instrument.

        Polls a profile command at ``interval_ms`` and yields
        MeasurementDataPoint messages until the client cancels.

        Every yielded point carries ``seq`` (work order §3.6): a
        per-stream monotonic counter starting at 1, incremented by
        exactly 1 for EVERY point — including ``status:"error"`` points
        (errors are data; gap detectors must not fire because errors
        were unsequenced). The counter resets per StreamMeasurement
        call; 0 is reserved for unsequenced (pre-seq) daemons and is
        never emitted here.

        Chunk-capable hardware-clocked sources (work order §7): when the
        requested ``interval_ms < 100`` and the instrument's protocol
        driver exposes ``open_hw_stream``, the interval is reinterpreted
        as the sample period (1 ms floor) and samples are coalesced into
        ScalarChunk blocks. Ordinary polled commands keep per-point
        emission and the 10 ms poll floor regardless of the requested
        interval — at ``interval_ms >= 100`` chunks are never emitted
        (the negotiation-free back-compat rule, §7.2).
        """
        stream_id = request.stream_id
        instrument_id = request.instrument_id
        command_name = request.command_name
        interval_ms = max(request.interval_ms, 10)  # poll floor (doc §5)

        # Per-stream monotonic sequence counter (§3.6). Local to this
        # call, so it resets with every StreamMeasurement invocation.
        seq = 0

        logger.info(
            "StreamMeasurement %s: '%s' -> %s every %dms",
            stream_id, command_name, instrument_id, interval_ms,
        )

        # Validate profile command exists and is streamable
        if not self._capability_manager:
            seq += 1
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                error="Profile system not available",
                status="error",
                seq=seq,
            )
            return

        params = dict(request.parameters) if request.parameters else None

        # --- Protocol-driver instruments (CAN / I2C / SPI / OPC-UA /
        # synthetic DAQ) dispatch through the driver, not the profile
        # command table. Chunk-capable hardware-clocked commands take the
        # chunked path only below the §7.2 trigger; everything else is
        # ordinary per-point polling at the 10 ms floor.
        protocol_driver = self._capability_manager.get_protocol_driver(
            instrument_id
        )
        if protocol_driver is not None:
            hw_source = None
            if 0 < request.interval_ms < CHUNK_TRIGGER_MS:
                hw_source = self._open_hw_source(
                    protocol_driver, command_name, params
                )
            self._active_streams[stream_id] = asyncio.current_task()
            try:
                if hw_source is not None:
                    point_gen = self._stream_chunked(
                        context, hw_source, stream_id, request.interval_ms,
                    )
                else:
                    point_gen = self._stream_driver_polled(
                        context, protocol_driver, stream_id,
                        command_name, params, interval_ms,
                    )
                async for point in point_gen:
                    yield point
            finally:
                self._active_streams.pop(stream_id, None)
            return

        caps = self._capability_manager.get_instrument_caps(instrument_id)
        if caps is None:
            seq += 1
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                error=f"Instrument not found: {instrument_id}",
                status="error",
                seq=seq,
            )
            return

        cmd = caps.get_command(command_name)
        if cmd is None:
            seq += 1
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                error=f"Command '{command_name}' not found or disabled",
                status="error",
                seq=seq,
            )
            return

        if not cmd.streamable:
            seq += 1
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                error=f"Command '{command_name}' is not streamable",
                status="error",
                seq=seq,
            )
            return

        # Extract unit and field info from command returns
        unit = ""
        if cmd.returns and cmd.returns.unit:
            unit = cmd.returns.unit

        has_fields = (
            cmd.returns
            and cmd.returns.fields
            and len(cmd.returns.fields) > 0
        )
        separator = ","
        if cmd.returns and cmd.returns.separator:
            separator = cmd.returns.separator

        # Resolve the SCPI/SDK dispatch once
        dispatch = self._capability_manager.resolve_command(
            instrument_id=instrument_id,
            command_name=command_name,
            params=params,
            is_query=True,
        )
        if dispatch is None:
            seq += 1
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                error=f"Failed to resolve command '{command_name}'",
                status="error",
                seq=seq,
            )
            return

        is_sdk = isinstance(dispatch, SDKCommandRequest)
        is_waveform_assembly = bool(cmd.waveform_assembly)
        stream_params = dict(request.parameters) if request.parameters else None
        interval_s = interval_ms / 1000.0
        loop = asyncio.get_running_loop()

        # Multi-channel frames (doc §3.5): the additive `channels`
        # parameter requests one frame of N per-channel vectors per tick.
        stream_channels = _parse_channels_param(stream_params)

        # --- IEEE 488.2 block commands (returns.type == binary) take
        # the byte-safe path: query_raw → decode_ieee_block, never the
        # text query() path (doc §2.1).
        is_ieee_block = bool(
            not is_sdk
            and not is_waveform_assembly
            and cmd.returns
            and cmd.returns.is_ieee_block
        )
        block_binary_config = None
        block_preamble_scpi: Optional[str] = None
        block_source_scpi: Dict[str, str] = {}
        block_fallback_channel = ""
        if is_ieee_block:
            block_binary_config = cmd.returns.effective_binary
            profile = caps.profile
            if block_binary_config.preamble_command and profile is not None:
                block_preamble_scpi = profile.resolve_scpi_ref(
                    block_binary_config.preamble_command, stream_params
                )
            if stream_params:
                block_fallback_channel = str(
                    stream_params.get("channel")
                    or stream_params.get("source")
                    or ""
                )
            if stream_channels:
                if (
                    len(stream_channels) > 1
                    and not block_binary_config.source_command
                ):
                    # Without a per-channel source selector the daemon
                    # cannot read distinct channels (or their distinct
                    # preambles) within one tick.
                    seq += 1
                    yield edge_pb2.MeasurementDataPoint(
                        stream_id=stream_id,
                        error=(
                            f"Command '{command_name}' has no "
                            "binary.source_command; multi-channel frames "
                            "need a per-channel source selector (doc §3.5)"
                        ),
                        status="error",
                        seq=seq,
                    )
                    return
                if block_binary_config.source_command and profile is not None:
                    block_source_scpi = {
                        ch: profile.resolve_source_ref(
                            block_binary_config.source_command, ch
                        )
                        for ch in stream_channels
                    }

        # Register the stream
        self._active_streams[stream_id] = asyncio.current_task()

        try:
            while not context.cancelled():
                loop_start = time.time()

                try:
                    # --- Waveform assembly path (SCPI oscilloscopes) ---
                    if is_waveform_assembly:
                        if stream_channels:
                            # Multi-channel frame (doc §3.5): one source
                            # switch + preamble + block read PER channel,
                            # one MeasurementDataPoint per tick.
                            frame_vectors = []
                            for ch in stream_channels:
                                per_params = dict(stream_params or {})
                                per_params["channel"] = ch
                                frame_vectors.append(
                                    await self._execute_waveform_assembly_for_stream(
                                        loop=loop,
                                        instrument_id=instrument_id,
                                        timeout_ms=5000,
                                        cmd_config=cmd,
                                        params=per_params,
                                        channel_label=ch,
                                    )
                                )
                            ts_ms = int(time.time() * 1000)
                            seq += 1
                            point = edge_pb2.MeasurementDataPoint(
                                stream_id=stream_id,
                                value=0.0,
                                timestamp_ms=ts_ms,
                                unit=unit,
                                error="",
                                status="ok",
                                seq=seq,
                            )
                            # vectors[] + field-8 first-channel back-compat
                            populate_point_vectors(point, frame_vectors)
                            yield point
                        else:
                            vector_data = await self._execute_waveform_assembly_for_stream(
                                loop=loop,
                                instrument_id=instrument_id,
                                timeout_ms=5000,
                                cmd_config=cmd,
                                params=stream_params,
                            )
                            ts_ms = int(time.time() * 1000)
                            seq += 1
                            yield edge_pb2.MeasurementDataPoint(
                                stream_id=stream_id,
                                value=0.0,
                                timestamp_ms=ts_ms,
                                unit=unit,
                                error="",
                                status="ok",
                                vector_data=vector_data,
                                seq=seq,
                            )

                    # --- IEEE block path (returns.type == binary) ---
                    elif is_ieee_block:
                        frame_vectors, block_error = await self._read_ieee_block_frame(
                            loop=loop,
                            dispatch=dispatch,
                            instrument_id=instrument_id,
                            timeout_ms=5000,
                            returns_cfg=cmd.returns,
                            binary_config=block_binary_config,
                            preamble_scpi=block_preamble_scpi,
                            channels=stream_channels,
                            source_scpi=block_source_scpi,
                            fallback_channel=block_fallback_channel,
                        )
                        ts_ms = int(time.time() * 1000)
                        seq += 1
                        if block_error is not None:
                            # Malformed blocks are sequenced error
                            # points; the stream continues (doc §2.2
                            # rule 4, §3.6).
                            yield edge_pb2.MeasurementDataPoint(
                                stream_id=stream_id,
                                value=0.0,
                                timestamp_ms=ts_ms,
                                unit=unit,
                                error=block_error,
                                status="error",
                                seq=seq,
                            )
                        else:
                            point = edge_pb2.MeasurementDataPoint(
                                stream_id=stream_id,
                                value=0.0,
                                timestamp_ms=ts_ms,
                                unit=unit,
                                error="",
                                status="ok",
                                seq=seq,
                            )
                            # vectors[] only for multi-channel frames;
                            # field 8 always carries the first channel.
                            populate_point_vectors(point, frame_vectors)
                            yield point

                    elif is_sdk:
                        if not self._sdk_executor:
                            raise ValueError("SDK executor not available")
                        result = await loop.run_in_executor(
                            self._executor,
                            lambda: self._sdk_executor.execute(
                                instrument_id=instrument_id,
                                command_name=dispatch.command_name,
                                params=dispatch.params,
                                sdk_call=dispatch.sdk_call,
                                is_query=True,
                            ),
                        )
                    else:
                        result = await loop.run_in_executor(
                            self._executor,
                            lambda: self._handler.execute_command(
                                scpi_cmd=dispatch,
                                instrument_id=instrument_id,
                                timeout_ms=5000,
                                force_query=True,
                            ),
                        )

                    if not is_waveform_assembly and not is_ieee_block:
                        ts_ms = int(time.time() * 1000)

                        if result["success"]:
                            raw = result["response"].strip()
                            # Apply response parser before float conversion
                            raw = self._apply_response_processing(
                                raw, instrument_id, command_name
                            )
                            values_map: Dict[str, float] = {}

                            if has_fields:
                                parts = raw.split(separator)
                                for i, field_def in enumerate(
                                    cmd.returns.fields
                                ):
                                    if i < len(parts):
                                        fname = (
                                            field_def.get("name", f"field_{i}")
                                            if isinstance(field_def, dict)
                                            else getattr(
                                                field_def, "name", f"field_{i}"
                                            )
                                        )
                                        try:
                                            values_map[fname] = float(
                                                parts[i].strip()
                                            )
                                        except ValueError:
                                            values_map[fname] = 0.0

                            # Primary value
                            try:
                                primary = float(
                                    raw.split(separator)[0].strip()
                                )
                            except (ValueError, IndexError):
                                primary = 0.0

                            seq += 1
                            yield edge_pb2.MeasurementDataPoint(
                                stream_id=stream_id,
                                value=primary,
                                timestamp_ms=ts_ms,
                                unit=unit,
                                error="",
                                status="ok",
                                values=values_map,
                                seq=seq,
                            )
                        else:
                            # Error points are sequenced like any point
                            # (§3.6) and the stream continues (§5).
                            seq += 1
                            yield edge_pb2.MeasurementDataPoint(
                                stream_id=stream_id,
                                value=0.0,
                                timestamp_ms=ts_ms,
                                unit=unit,
                                error=result["error"],
                                status="error",
                                seq=seq,
                            )

                except Exception as exc:
                    seq += 1
                    yield edge_pb2.MeasurementDataPoint(
                        stream_id=stream_id,
                        value=0.0,
                        timestamp_ms=int(time.time() * 1000),
                        unit=unit,
                        error=str(exc),
                        status="error",
                        seq=seq,
                    )

                # Overrun policy (doc §5): sleep only for the remainder
                # of the interval — slow reads skip ticks (poll late)
                # rather than queueing a backlog, and timestamp_ms above
                # is always the wall-clock of the actual read.
                elapsed = time.time() - loop_start
                remaining = max(0, interval_s - elapsed)
                if remaining > 0:
                    await asyncio.sleep(remaining)

        except asyncio.CancelledError:
            pass
        finally:
            self._active_streams.pop(stream_id, None)
            seq += 1
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                value=0.0,
                timestamp_ms=int(time.time() * 1000),
                unit=unit,
                error="",
                status="stopped",
                seq=seq,
            )
            logger.info("StreamMeasurement %s stopped", stream_id)

    @staticmethod
    def _open_hw_source(
        driver: Any,
        command_name: str,
        params: Optional[Dict[str, Any]],
    ) -> Optional[Any]:
        """Resolve a chunk-capable hardware-clocked source (work order §7).

        Duck-typed: a protocol driver advertises chunk capability by
        exposing ``open_hw_stream(command_name, params) -> source | None``
        (contract in ``hw_stream.py``). Ordinary polled commands have no
        source and keep per-point emission regardless of the requested
        interval (§7.2).
        """
        opener = getattr(driver, "open_hw_stream", None)
        if opener is None:
            return None
        try:
            return opener(command_name, params)
        except Exception as exc:
            logger.warning(
                "open_hw_stream('%s') failed: %s — falling back to "
                "per-point polling", command_name, exc,
            )
            return None

    async def _stream_chunked(
        self,
        context: grpc.aio.ServicerContext,
        source: Any,
        stream_id: str,
        requested_interval_ms: float,
    ) -> AsyncIterator[edge_pb2.MeasurementDataPoint]:
        """Chunked emission for hardware-clocked sources (work order §7).

        ``interval_ms`` is reinterpreted as the SAMPLE PERIOD (1 ms
        daemon floor, §7.2); the actual ``dt_ms`` comes from the source's
        ``start()`` readback, never the request (§7.3). The loop wakes
        every ~50 ms window, drains the source FIFO, and emits one
        chunk-bearing point per full window. A FIFO overflow yields a
        sequenced ``status:"error"`` point and the next chunk starts a
        fresh ``t0_ms`` — ``dt_ms`` is never stretched (§7.4).
        """
        loop = asyncio.get_running_loop()
        seq = 0
        unit = str(getattr(source, "unit", "") or "")
        period_ms = clamp_sample_period(requested_interval_ms)

        try:
            actual_ms = float(
                await loop.run_in_executor(self._executor, source.start, period_ms)
            )
            if actual_ms <= 0:
                raise ValueError(
                    f"source readback returned invalid period {actual_ms!r}"
                )
        except Exception as exc:
            seq += 1
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                timestamp_ms=int(time.time() * 1000),
                unit=unit,
                error=f"Failed to start hardware acquisition: {exc}",
                status="error",
                seq=seq,
            )
            return

        # t0: hardware tick 0 mapped onto daemon wall-clock captured at
        # acquisition start (§7.3).
        chunker = ScalarChunker(dt_ms=actual_ms, t0_ms=time.time() * 1000.0)
        window_s = CHUNK_WINDOW_MS / 1000.0
        logger.info(
            "StreamMeasurement %s: chunked hardware-clocked emission "
            "(period %.3f ms readback for %.3f ms request, ~%d samples/chunk)",
            stream_id, actual_ms, period_ms, chunker.target_n,
        )

        try:
            while not context.cancelled():
                tick_start = time.time()
                try:
                    block = await loop.run_in_executor(
                        self._executor, source.read
                    )
                    timestamp_ms = int(time.time() * 1000)  # actual read time
                    if block is not None and getattr(block, "overflow", False):
                        # Flush, report the gap as a sequenced error
                        # point, then rebase: fresh t0_ms, dt unchanged.
                        if chunker.pending:
                            seq += 1
                            yield _build_chunk_point(
                                stream_id, unit, chunker.take(),
                                timestamp_ms, seq,
                            )
                        seq += 1
                        yield edge_pb2.MeasurementDataPoint(
                            stream_id=stream_id,
                            timestamp_ms=timestamp_ms,
                            unit=unit,
                            error="hardware FIFO overflow: samples dropped",
                            status="error",
                            seq=seq,
                        )
                        chunker.rebase(time.time() * 1000.0)
                    if block is not None:
                        for value, values_map in block.rows():
                            if chunker.add(value, values_map):
                                seq += 1
                                yield _build_chunk_point(
                                    stream_id, unit, chunker.take(),
                                    timestamp_ms, seq,
                                )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    timestamp_ms = int(time.time() * 1000)
                    if chunker.pending:
                        seq += 1
                        yield _build_chunk_point(
                            stream_id, unit, chunker.take(), timestamp_ms, seq,
                        )
                    seq += 1
                    yield edge_pb2.MeasurementDataPoint(
                        stream_id=stream_id,
                        timestamp_ms=timestamp_ms,
                        unit=unit,
                        error=str(exc),
                        status="error",
                        seq=seq,
                    )
                    # A failed read is a gap in the sample clock: the
                    # next chunk starts a fresh t0_ms (§7.4).
                    chunker.rebase(time.time() * 1000.0)

                # Overrun policy (§5/§7.4): skip ticks — sleep only for
                # the remainder of the window, never queue a backlog.
                elapsed = time.time() - tick_start
                remaining = window_s - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)

        except asyncio.CancelledError:
            pass
        finally:
            # Flush the partial window so buffered samples are not lost.
            if chunker.pending:
                seq += 1
                yield _build_chunk_point(
                    stream_id, unit, chunker.take(),
                    int(time.time() * 1000), seq,
                )
            try:
                source.stop()
            except Exception as exc:
                logger.debug("hw source stop() failed: %s", exc)
            seq += 1
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                timestamp_ms=int(time.time() * 1000),
                unit=unit,
                error="",
                status="stopped",
                seq=seq,
            )
            logger.info("StreamMeasurement %s stopped", stream_id)

    async def _stream_driver_polled(
        self,
        context: grpc.aio.ServicerContext,
        driver: Any,
        stream_id: str,
        command_name: str,
        params: Optional[Dict[str, Any]],
        interval_ms: int,
    ) -> AsyncIterator[edge_pb2.MeasurementDataPoint]:
        """Per-point polling for protocol-driver instruments.

        Doc §7.2: ordinary polled commands keep per-point emission and
        the 10 ms poll floor regardless of the requested interval — this
        is also the >= 100 ms half of the negotiation-free rule for
        chunk-capable commands (old clouds reset sub-100 requests to
        1000 ms and land here; no chunk is ever emitted on this path).
        """
        loop = asyncio.get_running_loop()
        seq = 0
        interval_s = interval_ms / 1000.0

        try:
            while not context.cancelled():
                loop_start = time.time()
                try:
                    result = await loop.run_in_executor(
                        self._executor,
                        driver.execute_command,
                        command_name,
                        params or {},
                    )
                    primary, values_map = _driver_stream_fields(result)
                    seq += 1
                    yield edge_pb2.MeasurementDataPoint(
                        stream_id=stream_id,
                        value=primary,
                        timestamp_ms=int(time.time() * 1000),
                        unit="",
                        error="",
                        status="ok",
                        values=values_map,
                        seq=seq,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    seq += 1
                    yield edge_pb2.MeasurementDataPoint(
                        stream_id=stream_id,
                        value=0.0,
                        timestamp_ms=int(time.time() * 1000),
                        unit="",
                        error=str(exc),
                        status="error",
                        seq=seq,
                    )

                # Overrun policy (doc §5): skip ticks, never queue.
                elapsed = time.time() - loop_start
                remaining = max(0, interval_s - elapsed)
                if remaining > 0:
                    await asyncio.sleep(remaining)

        except asyncio.CancelledError:
            pass
        finally:
            seq += 1
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                value=0.0,
                timestamp_ms=int(time.time() * 1000),
                unit="",
                error="",
                status="stopped",
                seq=seq,
            )
            logger.info("StreamMeasurement %s stopped", stream_id)

    async def StopStream(
        self,
        request: edge_pb2.StopStreamRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.StopStreamResponse:
        """Terminate an active measurement stream."""
        stream_id = request.stream_id
        logger.info("StopStream: %s", stream_id)

        task = self._active_streams.pop(stream_id, None)
        if task and not task.done():
            task.cancel()
            return edge_pb2.StopStreamResponse(success=True)

        return edge_pb2.StopStreamResponse(success=False)

    # ------------------------------------------------------------------
    # Status & health
    # ------------------------------------------------------------------

    async def GetStatus(
        self,
        request: edge_pb2.GetStatusRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.EdgeStatus:
        """Return daemon health and resource usage."""
        uptime = int(time.time() - self._start_time)
        instrument_count = len(self._instruments.list_resources())

        # Lightweight system metrics
        cpu_usage = 0.0
        memory_usage = 0.0
        try:
            import os
            # On Unix, get resident set size
            if hasattr(os, "getpid"):
                try:
                    with open(f"/proc/{os.getpid()}/statm", "r") as f:
                        pages = int(f.read().split()[1])
                        memory_usage = pages * 4096 / (1024 * 1024)  # MB
                except (FileNotFoundError, IndexError):
                    pass
        except Exception:
            pass

        return edge_pb2.EdgeStatus(
            edge_id=self._edge_id,
            hostname=socket.gethostname(),
            status=edge_pb2.EDGE_STATUS_CODE_ONLINE,
            instrument_count=instrument_count,
            uptime_seconds=uptime,
            version="1.0.0",
            os_info=f"{platform.system()} {platform.release()}",
            cpu_usage=cpu_usage,
            memory_usage=memory_usage,
        )

    async def Ping(
        self,
        request: edge_pb2.PingRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.PingResponse:
        """Lightweight health-check."""
        from google.protobuf import timestamp_pb2

        ts = timestamp_pb2.Timestamp()
        ts.GetCurrentTime()
        return edge_pb2.PingResponse(timestamp=ts)

    # ------------------------------------------------------------------
    # Registration (used by Go supervisor)
    # ------------------------------------------------------------------

    async def RegisterEdge(
        self,
        request: edge_pb2.RegisterEdgeRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.RegisterEdgeResponse:
        """Handle edge registration acknowledgement."""
        logger.info("RegisterEdge: edge_id=%s", request.edge_id)
        return edge_pb2.RegisterEdgeResponse(
            success=True,
            message="Edge registration acknowledged",
            assigned_edge_id=request.edge_id,
        )

    async def Heartbeat(
        self,
        request: edge_pb2.HeartbeatRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.HeartbeatResponse:
        """Handle heartbeat, return server timestamp."""
        return edge_pb2.HeartbeatResponse(
            acknowledged=True,
            server_timestamp_ms=int(time.time() * 1000),
        )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def GetWebcamSnapshot(
        self,
        request: edge_pb2.GetWebcamSnapshotRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.GetWebcamSnapshotResponse:
        """Fetch a JPEG frame from a LAN camera."""
        logger.info("GetWebcamSnapshot: %s", request.camera_url)

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    request.camera_url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        image_data = await resp.read()
                        return edge_pb2.GetWebcamSnapshotResponse(
                            image_data=image_data,
                            timestamp_ms=int(time.time() * 1000),
                            content_type=resp.content_type or "image/jpeg",
                        )
                    else:
                        return edge_pb2.GetWebcamSnapshotResponse(
                            error=f"HTTP {resp.status}",
                            timestamp_ms=int(time.time() * 1000),
                        )
        except ImportError:
            return edge_pb2.GetWebcamSnapshotResponse(
                error="aiohttp not available",
                timestamp_ms=int(time.time() * 1000),
            )
        except Exception as exc:
            logger.error("GetWebcamSnapshot error: %s", exc)
            return edge_pb2.GetWebcamSnapshotResponse(
                error=str(exc),
                timestamp_ms=int(time.time() * 1000),
            )

    # ------------------------------------------------------------------
    # RPyC-style SDK relay (Path B)
    # ------------------------------------------------------------------

    async def ProxySDKCall(
        self,
        request: edge_pb2.ProxySDKCallRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.ProxySDKCallResponse:
        """Forward an arbitrary vendor SDK method call to a local SDK.

        Dynamically imports the requested module, finds the method, and
        invokes it with the provided arguments.
        """
        call_id = request.call_id
        module_name = request.module
        method_name = request.method
        instrument_id = request.instrument_id

        logger.info(
            "ProxySDKCall %s: %s.%s -> %s",
            call_id, module_name, method_name, instrument_id,
        )

        start = time.time()

        # If an SDK executor has a connected client for this instrument,
        # invoke via that client. Otherwise, try dynamic import.
        if self._sdk_executor and self._sdk_executor.is_connected(
            instrument_id
        ):
            # Convert protobuf Value args to Python
            args = [_value_to_python(a) for a in request.args]
            kwargs = {
                k: _value_to_python(v) for k, v in request.kwargs.items()
            }

            try:
                loop = asyncio.get_running_loop()
                # The SDK executor's client is the live SDK object
                entry = self._sdk_executor._clients.get(instrument_id)
                if entry is None:
                    raise ValueError(
                        f"SDK client not found: {instrument_id}"
                    )

                def _call():
                    with entry.lock:
                        fn = getattr(entry.client, method_name)
                        return fn(*args, **kwargs)

                result = await loop.run_in_executor(self._executor, _call)
                elapsed_ms = int((time.time() - start) * 1000)

                return edge_pb2.ProxySDKCallResponse(
                    call_id=call_id,
                    success=True,
                    result=_python_to_value(result),
                    error_message="",
                    execution_time_ms=elapsed_ms,
                )

            except Exception as exc:
                elapsed_ms = int((time.time() - start) * 1000)
                return edge_pb2.ProxySDKCallResponse(
                    call_id=call_id,
                    success=False,
                    error_message=str(exc),
                    execution_time_ms=elapsed_ms,
                )

        # Fallback: dynamic import for instruments not managed by executor
        # ---  Security: validate module and method names  ---
        if not _is_module_allowed(module_name):
            elapsed_ms = int((time.time() - start) * 1000)
            logger.warning(
                "ProxySDKCall %s BLOCKED: module '%s' is not in the "
                "allowed modules list",
                call_id, module_name,
            )
            return edge_pb2.ProxySDKCallResponse(
                call_id=call_id,
                success=False,
                error_message=(
                    f"Module '{module_name}' is not in the allowed modules "
                    f"list. Only galois_edge.sdk_wrappers.* and "
                    f"galois_edge.vendor.* modules are permitted."
                ),
                execution_time_ms=elapsed_ms,
            )

        if method_name.startswith("_"):
            elapsed_ms = int((time.time() - start) * 1000)
            logger.warning(
                "ProxySDKCall %s BLOCKED: private method '%s' is not "
                "callable via RPC",
                call_id, method_name,
            )
            return edge_pb2.ProxySDKCallResponse(
                call_id=call_id,
                success=False,
                error_message=(
                    f"Method '{method_name}' is private and cannot be "
                    f"called via ProxySDKCall. Only public methods are "
                    f"permitted."
                ),
                execution_time_ms=elapsed_ms,
            )

        try:
            loop = asyncio.get_running_loop()
            args = [_value_to_python(a) for a in request.args]
            kwargs = {
                k: _value_to_python(v) for k, v in request.kwargs.items()
            }

            def _dynamic_call():
                mod = importlib.import_module(module_name)
                fn = getattr(mod, method_name)
                return fn(*args, **kwargs)

            result = await loop.run_in_executor(
                self._executor, _dynamic_call
            )
            elapsed_ms = int((time.time() - start) * 1000)

            return edge_pb2.ProxySDKCallResponse(
                call_id=call_id,
                success=True,
                result=_python_to_value(result),
                error_message="",
                execution_time_ms=elapsed_ms,
            )

        except Exception as exc:
            elapsed_ms = int((time.time() - start) * 1000)
            logger.error("ProxySDKCall %s error: %s", call_id, exc)
            return edge_pb2.ProxySDKCallResponse(
                call_id=call_id,
                success=False,
                error_message=str(exc),
                execution_time_ms=elapsed_ms,
            )

    # ------------------------------------------------------------------
    # Sweep / Ramp RPCs
    # ------------------------------------------------------------------

    async def StartSweep(
        self,
        request: edge_pb2.StartSweepRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.StartSweepResponse:
        """Begin a long-running sweep/ramp on the edge daemon."""
        instrument_id = request.instrument_id
        command_name = request.command_name
        target_value = request.target_value
        sweep_rate = request.sweep_rate

        logger.info(
            "StartSweep: '%s' on %s -> target=%f rate=%f",
            command_name, instrument_id, target_value, sweep_rate,
        )

        # Validate: capability manager must exist
        if not self._capability_manager:
            return edge_pb2.StartSweepResponse(
                accepted=False,
                error="Profile system not available",
            )

        # Validate: instrument and command must exist with sweep config
        caps = self._capability_manager.get_instrument_caps(instrument_id)
        if not caps:
            return edge_pb2.StartSweepResponse(
                accepted=False,
                error=f"Instrument not found: {instrument_id}",
            )

        cmd = caps.get_command(command_name)
        if not cmd or not cmd.sweep:
            return edge_pb2.StartSweepResponse(
                accepted=False,
                error=f"Command '{command_name}' has no sweep configuration",
            )

        # Check if instrument is already sweeping
        if instrument_id in self._sweeping_instruments:
            return edge_pb2.StartSweepResponse(
                accepted=False,
                error=f"Instrument '{instrument_id}' is already sweeping",
            )

        # Generate sweep_id
        sweep_id = f"{instrument_id}:{command_name}:{uuid.uuid4().hex[:8]}"

        # Set up state
        self._sweep_states[sweep_id] = {
            "status": "sweeping",
            "current_value": 0.0,
            "target_value": target_value,
            "sweep_rate": sweep_rate,
            "error": "",
            "instrument_id": instrument_id,
            "command_name": command_name,
        }
        self._sweeping_instruments.add(instrument_id)
        cancel_event = asyncio.Event()
        self._sweep_cancel_flags[sweep_id] = cancel_event

        # Build and send the sweep command
        sweep_cfg = cmd.sweep
        params = {"value": str(target_value), "sweep_rate": str(sweep_rate)}
        # Add any extra parameters from request
        if request.extra_parameters:
            params.update(dict(request.extra_parameters))
        sweep_scpi = sweep_cfg.command
        for k, v in params.items():
            sweep_scpi = sweep_scpi.replace(f"{{{k}}}", v)

        timeout_ms = (
            caps.profile.settings.timeout_ms if caps.profile else 5000
        )

        try:
            # Send the sweep start command
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                self._executor,
                lambda: self._handler.execute_command(
                    sweep_scpi,
                    instrument_id,
                    timeout_ms=timeout_ms,
                ),
            )
        except Exception as exc:
            self._sweep_states[sweep_id]["status"] = "error"
            self._sweep_states[sweep_id]["error"] = str(exc)
            self._sweeping_instruments.discard(instrument_id)
            self._sweep_cancel_flags.pop(sweep_id, None)
            return edge_pb2.StartSweepResponse(
                sweep_id=sweep_id,
                accepted=False,
                error=str(exc),
            )

        # Launch the polling task
        task = asyncio.create_task(
            self._sweep_poll_loop(
                sweep_id, sweep_cfg, instrument_id, timeout_ms, cancel_event,
            )
        )
        self._active_sweeps[sweep_id] = task

        return edge_pb2.StartSweepResponse(
            sweep_id=sweep_id,
            accepted=True,
        )

    async def _sweep_poll_loop(
        self,
        sweep_id: str,
        sweep_cfg: SweepConfig,
        instrument_id: str,
        timeout_ms: int,
        cancel_event: asyncio.Event,
    ) -> None:
        """Poll the instrument until sweep completes, errors, or is cancelled."""
        poll_interval = sweep_cfg.poll_interval_ms / 1000.0

        try:
            while True:
                # Check for cancellation
                if cancel_event.is_set():
                    # Fire stop command
                    if sweep_cfg.stop_command:
                        try:
                            loop = asyncio.get_running_loop()
                            await loop.run_in_executor(
                                self._executor,
                                lambda: self._handler.execute_command(
                                    sweep_cfg.stop_command,
                                    instrument_id,
                                    timeout_ms=timeout_ms,
                                ),
                            )
                        except Exception:
                            pass
                    self._sweep_states[sweep_id]["status"] = "aborted"
                    return

                # Poll check_command
                try:
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(
                        self._executor,
                        lambda: self._handler.execute_command(
                            sweep_cfg.check_command,
                            instrument_id,
                            force_query=True,
                            timeout_ms=timeout_ms,
                        ),
                    )
                    response_str = (
                        result.get("response", "")
                        if isinstance(result, dict)
                        else str(result)
                    )

                    # Check if sweep is complete
                    if sweep_cfg.check_idle_match:
                        if re.search(sweep_cfg.check_idle_match, response_str):
                            self._sweep_states[sweep_id]["status"] = "completed"
                            return

                except Exception as exc:
                    self._sweep_states[sweep_id]["status"] = "error"
                    self._sweep_states[sweep_id]["error"] = str(exc)
                    return

                # Wait before next poll
                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            # Task was cancelled externally -- fire stop command
            if sweep_cfg.stop_command:
                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        self._executor,
                        lambda: self._handler.execute_command(
                            sweep_cfg.stop_command,
                            instrument_id,
                            timeout_ms=timeout_ms,
                        ),
                    )
                except Exception:
                    pass
            self._sweep_states[sweep_id]["status"] = "aborted"
            raise

        finally:
            # Clean up reservation
            self._sweeping_instruments.discard(instrument_id)
            self._active_sweeps.pop(sweep_id, None)
            self._sweep_cancel_flags.pop(sweep_id, None)

    async def GetSweepStatus(
        self,
        request: edge_pb2.GetSweepStatusRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.SweepStatusResponse:
        """Return the current state of a sweep."""
        state = self._sweep_states.get(request.sweep_id)
        if not state:
            return edge_pb2.SweepStatusResponse(
                sweep_id=request.sweep_id,
                status="not_found",
                error="Sweep not found",
            )
        return edge_pb2.SweepStatusResponse(
            sweep_id=request.sweep_id,
            status=state["status"],
            current_value=state.get("current_value", 0.0),
            target_value=state.get("target_value", 0.0),
            sweep_rate=state.get("sweep_rate", 0.0),
            error=state.get("error", ""),
        )

    async def StopSweep(
        self,
        request: edge_pb2.StopSweepRequest,
        context: grpc.aio.ServicerContext,
    ) -> edge_pb2.StopSweepResponse:
        """Abort or hold an active sweep."""
        sweep_id = request.sweep_id

        # Support wildcard "*" to stop all sweeps
        if sweep_id == "*":
            for sid, cancel_event in list(self._sweep_cancel_flags.items()):
                cancel_event.set()
            return edge_pb2.StopSweepResponse(
                success=True,
                status="stopping_all",
            )

        cancel_event = self._sweep_cancel_flags.get(sweep_id)
        if not cancel_event:
            return edge_pb2.StopSweepResponse(
                success=False,
                status="not_found",
            )

        cancel_event.set()

        # Wait briefly for the poll loop to process the cancellation
        task = self._active_sweeps.get(sweep_id)
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        return edge_pb2.StopSweepResponse(
            success=True,
            status="holding",
        )

    # ------------------------------------------------------------------
    # Driver profile management
    # ------------------------------------------------------------------

    def set_profile_loader(self, loader: Any) -> None:
        """Attach the instrument ProfileLoader used by deploy and bind."""
        self._profile_loader = loader

    #: The cloud overloads DeployProfile's ``protocol`` field to carry a
    #: bind instruction: ``instrument:bind:<visa_address>``. Deploy and
    #: bind are the same RPC, told apart by this prefix.
    #:
    #: Without a branch for it the prefix was treated as a protocol name,
    #: so a bind tried to create a directory literally called
    #: ``instrument:bind:TCPIP::host::5027::SOCKET`` and no instrument was
    #: ever bound. The deploy half shipped and the bind half did not,
    #: which surfaced far away as an instrument registered "with no
    #: matching profile" and an empty command catalog.
    _BIND_PREFIX = "instrument:bind:"

    #: Protocol value the cloud sends for instrument profiles, as opposed
    #: to the protocol driver profiles the driver registry owns.
    _INSTRUMENT_PROTOCOL = "instrument"

    async def DeployProfile(self, request, context):
        """Write a YAML profile to disk, or bind one to an instrument.

        Three shapes, distinguished by ``protocol``:

        * ``instrument:bind:<visa>`` — bind an already-deployed instrument
          profile to a live instrument, so its named commands become
          callable by a sequence.
        * ``instrument`` — an instrument profile, matched against ``*IDN?``
          by ProfileLoader. Written to the daemon's dynamic profile dir.
        * anything else — a protocol driver profile for the driver
          registry, written under ``<driver_profile_dir>/<protocol>/``.
        """
        import os

        profile_name = request.profile_name
        profile_yaml = request.profile_yaml
        protocol = request.protocol or "modbus"

        if protocol.startswith(self._BIND_PREFIX):
            return await self._bind_instrument_profile(
                profile_name, protocol[len(self._BIND_PREFIX):]
            )

        if not profile_name or not profile_yaml:
            return edge_pb2.DeployProfileResponse(
                success=False,
                error_message="profile_name and profile_yaml are required",
            )

        if protocol == self._INSTRUMENT_PROTOCOL:
            return self._write_instrument_profile(profile_name, profile_yaml)

        # Determine target directory from config or driver_registry
        if self._driver_registry is not None:
            profile_dir = self._driver_registry.profiles_dir
        else:
            profile_dir = os.path.join(os.path.expanduser("~"), ".config", "galois-edge", "profiles")

        protocol_dir = os.path.join(profile_dir, protocol)
        os.makedirs(protocol_dir, exist_ok=True)

        file_path = os.path.join(protocol_dir, f"{profile_name}.yaml")
        try:
            with open(file_path, "w") as f:
                f.write(profile_yaml)
            logger.info("Deployed driver profile: %s → %s", profile_name, file_path)
        except Exception as exc:
            logger.error("Failed to write profile %s: %s", profile_name, exc)
            return edge_pb2.DeployProfileResponse(
                success=False,
                error_message=f"Failed to write profile: {exc}",
            )

        # Reload driver registry
        register_count = 0
        if self._driver_registry is not None:
            self._driver_registry.reload()
            for p in self._driver_registry.list_profiles():
                if p["name"] == profile_name:
                    register_count = p.get("register_count", 0)
                    break

        return edge_pb2.DeployProfileResponse(
            success=True,
            register_count=register_count,
        )

    def _write_instrument_profile(self, profile_name: str, profile_yaml: str):
        """Write an instrument profile where ProfileLoader will find it.

        Not under driver_profile_dir: that holds protocol driver profiles
        for the driver registry, and these are instrument profiles matched
        against *IDN?. Two registries. Writing to the wrong one produces a
        file that is correct, present, and never read.
        """
        import os

        loader = self._profile_loader
        dynamic_dir = getattr(loader, "dynamic_dir", None) if loader else None
        if dynamic_dir is None:
            return edge_pb2.DeployProfileResponse(
                success=False,
                error_message=(
                    "this daemon has no dynamic profile directory configured; "
                    "set DYNAMIC_PROFILE_DIR"
                ),
            )

        try:
            os.makedirs(dynamic_dir, exist_ok=True)
            file_path = os.path.join(str(dynamic_dir), f"{profile_name}.yaml")
            with open(file_path, "w", encoding="utf-8") as fh:
                fh.write(profile_yaml)
        except Exception as exc:
            logger.error("Failed to write instrument profile %s: %s", profile_name, exc)
            return edge_pb2.DeployProfileResponse(
                success=False,
                error_message=f"Failed to write profile: {exc}",
            )

        # Parse and register in memory rather than reloading everything.
        # A full reload rescans the bundled tree, which takes minutes on a
        # slow SD card and would drop every profile in the meantime.
        try:
            import yaml as _yaml

            from galois_edge.profile_schema import profile_from_dict

            profile = profile_from_dict(_yaml.safe_load(profile_yaml))
            loader.add_profile(profile)
        except Exception as exc:
            logger.error("Deployed profile %s does not parse: %s", profile_name, exc)
            return edge_pb2.DeployProfileResponse(
                success=False,
                error_message=f"Profile written but does not parse: {exc}",
            )

        logger.info("Deployed instrument profile: %s -> %s", profile_name, file_path)
        return edge_pb2.DeployProfileResponse(success=True, register_count=0)

    async def _bind_instrument_profile(self, profile_name: str, visa_address: str):
        """Bind a deployed profile to a live instrument.

        Binding is what turns a deployed file into callable commands: the
        capability manager keys instruments by VISA address and a sequence
        can only issue a NAMED command resolved through the bound profile.
        Deploy without bind leaves the catalog empty.
        """
        loader = self._profile_loader
        if loader is None:
            return edge_pb2.DeployProfileResponse(
                success=False, error_message="no profile loader on this daemon"
            )
        if not self._capability_manager:
            return edge_pb2.DeployProfileResponse(
                success=False, error_message="no capability manager on this daemon"
            )

        profile = None
        for candidate in loader.profiles.values():
            if candidate.profile_key.lower() == profile_name.lower():
                profile = candidate
                break
        if profile is None:
            return edge_pb2.DeployProfileResponse(
                success=False,
                error_message=(
                    f"profile {profile_name!r} is not loaded; deploy it before binding"
                ),
            )

        caps = self._capability_manager.get_instrument_caps(visa_address)
        idn = caps.idn_response if caps else ""
        self._capability_manager.register_instrument(
            instrument_id=visa_address,
            visa_address=visa_address,
            idn_response=idn,
            profile=profile,
        )
        command_count = len(profile.commands or {})
        logger.info(
            "Bound profile %s to %s (%d commands)",
            profile_name, visa_address, command_count,
        )
        return edge_pb2.DeployProfileResponse(
            success=True, register_count=command_count
        )

    async def RemoveProfile(self, request, context):
        """Remove a deployed driver profile from disk and reload."""
        import os

        profile_name = request.profile_name
        protocol = request.protocol or "modbus"

        if self._driver_registry is not None:
            profile_dir = self._driver_registry.profiles_dir
        else:
            return edge_pb2.RemoveProfileResponse(
                success=False,
                error_message="No driver registry available",
            )

        file_path = os.path.join(profile_dir, protocol, f"{profile_name}.yaml")
        if not os.path.exists(file_path):
            return edge_pb2.RemoveProfileResponse(
                success=False,
                error_message=f"Profile not found: {profile_name}",
            )

        try:
            os.remove(file_path)
            logger.info("Removed driver profile: %s", file_path)
        except Exception as exc:
            return edge_pb2.RemoveProfileResponse(
                success=False,
                error_message=str(exc),
            )

        self._driver_registry.reload()
        return edge_pb2.RemoveProfileResponse(success=True)

    async def ListProfiles(self, request, context):
        """Return all driver profiles installed on this daemon."""
        profiles = []
        if self._driver_registry is not None:
            for p in self._driver_registry.list_profiles():
                # Check if any instrument is using this profile
                active = any(
                    True
                    for inst in self._driver_registry._instances.values()
                    if hasattr(inst, "profile")
                    and inst.profile.get("identity", {}).get("model") == p.get("model")
                )
                profiles.append(edge_pb2.DriverProfileSummary(
                    name=p.get("name", ""),
                    protocol=p.get("protocol", ""),
                    manufacturer=p.get("manufacturer", ""),
                    model=p.get("model", ""),
                    description=p.get("description", ""),
                    register_count=p.get("register_count", 0),
                    active=active,
                ))
        return edge_pb2.ListProfilesResponse(profiles=profiles)

    async def _connect_instrument_impl(
        self,
        *,
        profile_name: str,
        profile_yaml: str,
        protocol: str,
        instrument_id: str,
        transport_uri: str,
        connection_kwargs: dict,
    ) -> tuple[bool, str, int, list]:
        """Shared implementation of the deploy + connect path.

        Returns ``(success, error_message, register_count, commands)`` so
        the wrapper RPC implementations can build the protocol-specific
        response message.
        """
        import os

        if not instrument_id or not transport_uri:
            return False, "instrument_id and transport_uri are required", 0, []
        if self._driver_registry is None:
            return False, "Driver registry not available", 0, []

        # Step 1: deploy profile if YAML provided
        if profile_yaml and profile_name:
            profile_dir = self._driver_registry.profiles_dir
            protocol_dir = os.path.join(profile_dir, protocol or "modbus")
            os.makedirs(protocol_dir, exist_ok=True)
            file_path = os.path.join(protocol_dir, f"{profile_name}.yaml")
            try:
                with open(file_path, "w") as f:
                    f.write(profile_yaml)
                self._driver_registry.reload()
                logger.info("Deployed profile %s for ConnectInstrument", profile_name)
            except Exception as exc:
                return False, f"Failed to deploy profile: {exc}", 0, []

        if not profile_name:
            return False, "profile_name is required", 0, []

        try:
            driver = self._driver_registry.instantiate(
                profile_name=profile_name,
                instrument_id=instrument_id,
                transport_uri=transport_uri,
                **connection_kwargs,
            )
            driver.connect()

            if self._capability_manager is not None:
                self._capability_manager.register_protocol_driver(
                    instrument_id, driver
                )

            caps = driver.get_capabilities()
            register_count = caps.get("registers", 0) or 0
            commands_obj = caps.get("commands", [])
            commands = (
                list(commands_obj) if not isinstance(commands_obj, int) else []
            )
            logger.info(
                "Connected %s instrument: %s (%s @ %s, %d registers/commands)",
                protocol,
                instrument_id,
                profile_name,
                transport_uri,
                register_count
                if isinstance(commands_obj, int)
                else len(commands),
            )
            return True, "", register_count, commands

        except Exception as exc:
            logger.error("ConnectInstrument failed: %s", exc)
            return False, str(exc), 0, []

    async def ConnectModbusInstrument(self, request, context):
        """Legacy protocol-specific connect.  Routes to ConnectInstrument."""
        protocol = request.protocol or "modbus"
        connection_kwargs: dict = {}
        # Modbus is the only protocol the legacy RPC carried a typed
        # parameter for.  Strip it for non-Modbus protocols (the runtime
        # already does this); for Modbus, preserve the existing default.
        if protocol == "modbus":
            connection_kwargs["slave_id"] = request.slave_id or 1

        ok, err, reg_count, commands = await self._connect_instrument_impl(
            profile_name=request.profile_name,
            profile_yaml=request.profile_yaml,
            protocol=protocol,
            instrument_id=request.instrument_id,
            transport_uri=request.transport_uri,
            connection_kwargs=connection_kwargs,
        )
        if not ok:
            return edge_pb2.ConnectModbusInstrumentResponse(
                success=False, error_message=err,
            )
        return edge_pb2.ConnectModbusInstrumentResponse(
            success=True,
            instrument_id=request.instrument_id,
            register_count=reg_count,
            commands=commands,
        )

    async def ConnectInstrument(self, request, context):
        """Generic deploy + connect across all protocol drivers."""
        protocol = request.protocol or "modbus"

        # Translate the connection_params Struct into kwargs for the
        # driver registry.  Empty Struct → empty dict.
        connection_kwargs: dict = {}
        if request.HasField("connection_params"):
            from google.protobuf.json_format import MessageToDict
            connection_kwargs = MessageToDict(request.connection_params)

        ok, err, reg_count, commands = await self._connect_instrument_impl(
            profile_name=request.profile_name,
            profile_yaml=request.profile_yaml,
            protocol=protocol,
            instrument_id=request.instrument_id,
            transport_uri=request.transport_uri,
            connection_kwargs=connection_kwargs,
        )
        if not ok:
            return edge_pb2.ConnectInstrumentResponse(
                success=False, error_message=err,
            )
        return edge_pb2.ConnectInstrumentResponse(
            success=True,
            instrument_id=request.instrument_id,
            register_count=reg_count,
            commands=commands,
        )

    async def DisconnectInstrument(self, request, context):
        """Disconnect a protocol-driver instrument."""
        instrument_id = request.instrument_id
        if not instrument_id:
            return edge_pb2.DisconnectInstrumentResponse(
                success=False, error_message="instrument_id required"
            )

        if self._driver_registry is None:
            return edge_pb2.DisconnectInstrumentResponse(
                success=False, error_message="Driver registry not available"
            )

        driver = self._driver_registry.get_instance(instrument_id)
        if driver is None:
            return edge_pb2.DisconnectInstrumentResponse(
                success=False, error_message=f"Instrument not found: {instrument_id}"
            )

        try:
            driver.disconnect()
            if self._capability_manager is not None:
                self._capability_manager.unregister_instrument(instrument_id)
            logger.info("Disconnected instrument: %s", instrument_id)
            return edge_pb2.DisconnectInstrumentResponse(success=True)
        except Exception as exc:
            return edge_pb2.DisconnectInstrumentResponse(
                success=False, error_message=str(exc)
            )


# ---------------------------------------------------------------------------
# Bearer-token auth interceptor
# ---------------------------------------------------------------------------

# Full method names that bypass auth (health probes must work without a token).
# The proto package is galois.edge.v1 — do NOT abbreviate to /edge.EdgeDaemonService/...
_AUTH_EXEMPT_METHODS: frozenset[str] = frozenset({
    "/galois.edge.v1.EdgeDaemonService/Ping",
})


def _extract_bearer_token(metadata: Any) -> str:
    """Extract the bearer token from gRPC invocation metadata.

    Accepts both ``Authorization: Bearer <token>`` and a bare ``<token>``.
    Returns an empty string when the header is absent.
    """
    if not metadata:
        return ""
    for key, value in metadata:
        if key.lower() == "authorization":
            val = value.strip()
            # Accept both "Bearer <token>" and bare "<token>".
            if val.lower().startswith("bearer "):
                return val[7:]
            return val
    return ""


class BearerTokenInterceptor(grpc_aio.ServerInterceptor):
    """gRPC server interceptor that enforces bearer-token authentication.

    Activated only when ``inbound_auth_token`` is non-empty. When the token
    is empty the interceptor should not be installed at all (see
    ``GRPCServer.__init__``).

    Exempt methods (see ``_AUTH_EXEMPT_METHODS``) bypass the check so that
    health probes can work without distributing the token to every probe tool.

    The comparison uses ``hmac.compare_digest`` to prevent timing oracles.

    Implements ``grpc.aio.ServerInterceptor`` via ``intercept_service``, which
    wraps each matched handler's callable with an auth-checking layer.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    async def intercept_service(
        self,
        continuation: Callable,
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        """Look up the handler, then wrap its callable with auth logic."""
        method_name: str = handler_call_details.method  # type: ignore[attr-defined]

        # Delegate to the next interceptor / handler registry first.
        handler: Optional[grpc.RpcMethodHandler] = await continuation(handler_call_details)

        # Allow explicitly exempt methods unconditionally (no wrapping needed).
        if method_name in _AUTH_EXEMPT_METHODS:
            return handler  # type: ignore[return-value]

        if handler is None:
            return handler  # type: ignore[return-value]

        # Determine which callable field is set on the handler namedtuple.
        # A single handler will have exactly one of the four callable fields
        # populated; the others are None.
        token = self._token

        def _make_auth_wrapper(original_fn: Callable, streaming: bool) -> Callable:
            """Return a wrapper coroutine / async generator that validates the token."""

            if not streaming:
                async def _auth_unary(request_or_iterator: Any, context: grpc_aio.ServicerContext) -> Any:
                    provided = _extract_bearer_token(context.invocation_metadata())
                    if not hmac.compare_digest(provided, token):
                        logger.debug("auth check failed for method %s", method_name)
                        await context.abort(
                            grpc.StatusCode.UNAUTHENTICATED,
                            "authentication required",
                        )
                        return None
                    return await original_fn(request_or_iterator, context)

                return _auth_unary
            else:
                async def _auth_streaming(request_or_iterator: Any, context: grpc_aio.ServicerContext) -> Any:  # type: ignore[return]
                    provided = _extract_bearer_token(context.invocation_metadata())
                    if not hmac.compare_digest(provided, token):
                        logger.debug("auth check failed for method %s", method_name)
                        await context.abort(
                            grpc.StatusCode.UNAUTHENTICATED,
                            "authentication required",
                        )
                        return
                    async for item in original_fn(request_or_iterator, context):
                        yield item

                return _auth_streaming

        # Wrap whichever callable field is populated.
        # grpc.method_handlers returns a namedtuple; we rebuild it with the
        # wrapped callable in the appropriate slot.
        if handler.unary_unary is not None:
            return handler._replace(unary_unary=_make_auth_wrapper(handler.unary_unary, False))
        if handler.unary_stream is not None:
            return handler._replace(unary_stream=_make_auth_wrapper(handler.unary_stream, True))
        if handler.stream_unary is not None:
            return handler._replace(stream_unary=_make_auth_wrapper(handler.stream_unary, False))
        if handler.stream_stream is not None:
            return handler._replace(stream_stream=_make_auth_wrapper(handler.stream_stream, True))

        return handler  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Server lifecycle wrapper
# ---------------------------------------------------------------------------

class GRPCServer:
    """Manages gRPC server lifecycle: start, stop, wait."""

    def __init__(
        self,
        instrument_manager: InstrumentManager,
        command_handler: CommandHandler,
        edge_id: str,
        port: int = 50052,
        max_workers: int = 10,
        capability_manager: Optional[CapabilityManager] = None,
        sdk_executor: Optional[SDKExecutor] = None,
        io_executor: Optional[ThreadPoolExecutor] = None,
        driver_registry: Optional[Any] = None,
        inbound_auth_token: str = "",
    ) -> None:
        self._port = port
        self._edge_id = edge_id
        self._inbound_auth_token = inbound_auth_token

        self._servicer = EdgeDaemonServicer(
            instrument_manager=instrument_manager,
            command_handler=command_handler,
            edge_id=edge_id,
            capability_manager=capability_manager,
            sdk_executor=sdk_executor,
            max_workers=max_workers,
            io_executor=io_executor,
            driver_registry=driver_registry,
        )

        self._server: Optional[grpc_aio.Server] = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def servicer(self) -> EdgeDaemonServicer:
        return self._servicer

    async def start(self) -> bool:
        """Start the gRPC server on 127.0.0.1.

        Returns True on success, False on failure.
        """
        try:
            # Install the bearer-token interceptor only when a token is set,
            # leaving zero overhead on the common unauthenticated path.
            interceptors = []
            if self._inbound_auth_token:
                interceptors = [BearerTokenInterceptor(self._inbound_auth_token)]
                logger.info("gRPC bearer-token authentication enabled")
            else:
                logger.debug("gRPC bearer-token authentication disabled (INBOUND_AUTH_TOKEN not set)")

            self._server = grpc_aio.server(
                interceptors=interceptors,
                options=[
                    ("grpc.max_send_message_length", 50 * 1024 * 1024),
                    ("grpc.max_receive_message_length", 50 * 1024 * 1024),
                    ("grpc.keepalive_time_ms", 30000),
                    ("grpc.keepalive_timeout_ms", 10000),
                    ("grpc.keepalive_permit_without_calls", True),
                    ("grpc.http2.max_pings_without_data", 0),
                ],
            )

            edge_pb2_grpc.add_EdgeDaemonServiceServicer_to_server(
                self._servicer, self._server,
            )

            listen_addr = f"127.0.0.1:{self._port}"
            self._server.add_insecure_port(listen_addr)

            await self._server.start()
            logger.info(
                "gRPC server started on %s (edge_id=%s)",
                listen_addr, self._edge_id,
            )
            return True

        except Exception as exc:
            logger.error("Failed to start gRPC server: %s", exc)
            return False

    async def stop(self, grace_period: float = 5.0) -> None:
        """Stop the gRPC server gracefully."""
        if self._server is not None:
            await self._server.stop(grace_period)
            logger.info("gRPC server stopped")

    async def wait_for_termination(self) -> None:
        """Block until the server terminates."""
        if self._server is not None:
            await self._server.wait_for_termination()
