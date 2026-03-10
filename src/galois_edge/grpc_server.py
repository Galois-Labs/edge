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
import importlib
import json
import logging
import platform
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Dict, Optional

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
from .command_handler import CommandHandler
from .sdk_executor import SDKExecutor

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._instruments = instrument_manager
        self._handler = command_handler
        self._edge_id = edge_id
        self._capability_manager = capability_manager
        self._sdk_executor = sdk_executor
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._start_time = time.time()

        # Active measurement streams: stream_id -> asyncio.Task
        self._active_streams: Dict[str, asyncio.Task] = {}

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
        """Return all instruments currently known to the daemon."""
        logger.info("ListInstruments request (filter=%s)", request.filter)

        try:
            loop = asyncio.get_running_loop()
            resources = await loop.run_in_executor(
                self._executor,
                self._instruments.list_resources,
            )

            instruments = []
            for visa_address in resources:
                # Attempt identification
                idn = ""
                connected = self._instruments.is_connected(visa_address)

                if connected:
                    try:
                        idn = await loop.run_in_executor(
                            self._executor,
                            self._instruments.identify,
                            visa_address,
                        )
                    except Exception as exc:
                        logger.debug(
                            "Could not identify %s: %s", visa_address, exc
                        )

                # Get capability record if available
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
                result = await loop.run_in_executor(
                    self._executor,
                    lambda: self._handler.execute_command(
                        scpi_cmd=dispatch,
                        instrument_id=instrument_id,
                        timeout_ms=timeout_ms,
                        command_id=command_id,
                        force_query=request.is_query,
                    ),
                )

            elapsed_ms = int((time.time() - start) * 1000)

            if result["success"]:
                return edge_pb2.ExecuteCommandResponse(
                    command_id=command_id,
                    success=True,
                    data=result["response"],
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
        """
        stream_id = request.stream_id
        instrument_id = request.instrument_id
        command_name = request.command_name
        interval_ms = max(request.interval_ms, 10)  # floor at 10 ms

        logger.info(
            "StreamMeasurement %s: '%s' -> %s every %dms",
            stream_id, command_name, instrument_id, interval_ms,
        )

        # Validate profile command exists and is streamable
        if not self._capability_manager:
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                error="Profile system not available",
                status="error",
            )
            return

        caps = self._capability_manager.get_instrument_caps(instrument_id)
        if caps is None:
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                error=f"Instrument not found: {instrument_id}",
                status="error",
            )
            return

        cmd = caps.get_command(command_name)
        if cmd is None:
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                error=f"Command '{command_name}' not found or disabled",
                status="error",
            )
            return

        if not cmd.streamable:
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                error=f"Command '{command_name}' is not streamable",
                status="error",
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
            is_query=True,
        )
        if dispatch is None:
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                error=f"Failed to resolve command '{command_name}'",
                status="error",
            )
            return

        is_sdk = isinstance(dispatch, SDKCommandRequest)
        interval_s = interval_ms / 1000.0
        loop = asyncio.get_running_loop()

        # Register the stream
        self._active_streams[stream_id] = asyncio.current_task()

        try:
            while not context.cancelled():
                loop_start = time.time()

                try:
                    if is_sdk:
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

                    ts_ms = int(time.time() * 1000)

                    if result["success"]:
                        raw = result["response"].strip()
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

                        yield edge_pb2.MeasurementDataPoint(
                            stream_id=stream_id,
                            value=primary,
                            timestamp_ms=ts_ms,
                            unit=unit,
                            error="",
                            status="ok",
                            values=values_map,
                        )
                    else:
                        yield edge_pb2.MeasurementDataPoint(
                            stream_id=stream_id,
                            value=0.0,
                            timestamp_ms=ts_ms,
                            unit=unit,
                            error=result["error"],
                            status="error",
                        )

                except Exception as exc:
                    yield edge_pb2.MeasurementDataPoint(
                        stream_id=stream_id,
                        value=0.0,
                        timestamp_ms=int(time.time() * 1000),
                        unit=unit,
                        error=str(exc),
                        status="error",
                    )

                # Sleep for the remaining interval
                elapsed = time.time() - loop_start
                remaining = max(0, interval_s - elapsed)
                if remaining > 0:
                    await asyncio.sleep(remaining)

        except asyncio.CancelledError:
            pass
        finally:
            self._active_streams.pop(stream_id, None)
            yield edge_pb2.MeasurementDataPoint(
                stream_id=stream_id,
                value=0.0,
                timestamp_ms=int(time.time() * 1000),
                unit=unit,
                error="",
                status="stopped",
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
    ) -> None:
        self._port = port
        self._edge_id = edge_id

        self._servicer = EdgeDaemonServicer(
            instrument_manager=instrument_manager,
            command_handler=command_handler,
            edge_id=edge_id,
            capability_manager=capability_manager,
            sdk_executor=sdk_executor,
            max_workers=max_workers,
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
            self._server = grpc_aio.server(
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
