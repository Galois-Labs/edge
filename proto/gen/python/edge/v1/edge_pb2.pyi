import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ConnectionType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CONNECTION_TYPE_UNSPECIFIED: _ClassVar[ConnectionType]
    CONNECTION_TYPE_GPIB: _ClassVar[ConnectionType]
    CONNECTION_TYPE_USB: _ClassVar[ConnectionType]
    CONNECTION_TYPE_LAN: _ClassVar[ConnectionType]
    CONNECTION_TYPE_SERIAL: _ClassVar[ConnectionType]

class ParameterType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PARAMETER_TYPE_UNSPECIFIED: _ClassVar[ParameterType]
    PARAMETER_TYPE_STRING: _ClassVar[ParameterType]
    PARAMETER_TYPE_NUMBER: _ClassVar[ParameterType]
    PARAMETER_TYPE_BOOLEAN: _ClassVar[ParameterType]
    PARAMETER_TYPE_ENUM: _ClassVar[ParameterType]

class EdgeStatusCode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    EDGE_STATUS_CODE_UNSPECIFIED: _ClassVar[EdgeStatusCode]
    EDGE_STATUS_CODE_ONLINE: _ClassVar[EdgeStatusCode]
    EDGE_STATUS_CODE_OFFLINE: _ClassVar[EdgeStatusCode]
    EDGE_STATUS_CODE_DEGRADED: _ClassVar[EdgeStatusCode]
CONNECTION_TYPE_UNSPECIFIED: ConnectionType
CONNECTION_TYPE_GPIB: ConnectionType
CONNECTION_TYPE_USB: ConnectionType
CONNECTION_TYPE_LAN: ConnectionType
CONNECTION_TYPE_SERIAL: ConnectionType
PARAMETER_TYPE_UNSPECIFIED: ParameterType
PARAMETER_TYPE_STRING: ParameterType
PARAMETER_TYPE_NUMBER: ParameterType
PARAMETER_TYPE_BOOLEAN: ParameterType
PARAMETER_TYPE_ENUM: ParameterType
EDGE_STATUS_CODE_UNSPECIFIED: EdgeStatusCode
EDGE_STATUS_CODE_ONLINE: EdgeStatusCode
EDGE_STATUS_CODE_OFFLINE: EdgeStatusCode
EDGE_STATUS_CODE_DEGRADED: EdgeStatusCode

class Instrument(_message.Message):
    __slots__ = ("id", "address", "connection_type", "idn_string", "manufacturer", "model", "serial_number", "firmware", "profile_name", "instrument_class", "is_connected", "capabilities")
    ID_FIELD_NUMBER: _ClassVar[int]
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_TYPE_FIELD_NUMBER: _ClassVar[int]
    IDN_STRING_FIELD_NUMBER: _ClassVar[int]
    MANUFACTURER_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    SERIAL_NUMBER_FIELD_NUMBER: _ClassVar[int]
    FIRMWARE_FIELD_NUMBER: _ClassVar[int]
    PROFILE_NAME_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_CLASS_FIELD_NUMBER: _ClassVar[int]
    IS_CONNECTED_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    id: str
    address: str
    connection_type: ConnectionType
    idn_string: str
    manufacturer: str
    model: str
    serial_number: str
    firmware: str
    profile_name: str
    instrument_class: str
    is_connected: bool
    capabilities: _containers.RepeatedCompositeFieldContainer[CommandCapability]
    def __init__(self, id: _Optional[str] = ..., address: _Optional[str] = ..., connection_type: _Optional[_Union[ConnectionType, str]] = ..., idn_string: _Optional[str] = ..., manufacturer: _Optional[str] = ..., model: _Optional[str] = ..., serial_number: _Optional[str] = ..., firmware: _Optional[str] = ..., profile_name: _Optional[str] = ..., instrument_class: _Optional[str] = ..., is_connected: bool = ..., capabilities: _Optional[_Iterable[_Union[CommandCapability, _Mapping]]] = ...) -> None: ...

class CommandCapability(_message.Message):
    __slots__ = ("name", "description", "type", "parameters", "returns_data", "is_dangerous", "return_type", "unit", "is_streamable")
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    RETURNS_DATA_FIELD_NUMBER: _ClassVar[int]
    IS_DANGEROUS_FIELD_NUMBER: _ClassVar[int]
    RETURN_TYPE_FIELD_NUMBER: _ClassVar[int]
    UNIT_FIELD_NUMBER: _ClassVar[int]
    IS_STREAMABLE_FIELD_NUMBER: _ClassVar[int]
    name: str
    description: str
    type: str
    parameters: _containers.RepeatedCompositeFieldContainer[CommandParameter]
    returns_data: bool
    is_dangerous: bool
    return_type: str
    unit: str
    is_streamable: bool
    def __init__(self, name: _Optional[str] = ..., description: _Optional[str] = ..., type: _Optional[str] = ..., parameters: _Optional[_Iterable[_Union[CommandParameter, _Mapping]]] = ..., returns_data: bool = ..., is_dangerous: bool = ..., return_type: _Optional[str] = ..., unit: _Optional[str] = ..., is_streamable: bool = ...) -> None: ...

class CommandParameter(_message.Message):
    __slots__ = ("name", "description", "type", "required", "default_value", "enum_values", "unit")
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_VALUE_FIELD_NUMBER: _ClassVar[int]
    ENUM_VALUES_FIELD_NUMBER: _ClassVar[int]
    UNIT_FIELD_NUMBER: _ClassVar[int]
    name: str
    description: str
    type: ParameterType
    required: bool
    default_value: str
    enum_values: _containers.RepeatedScalarFieldContainer[str]
    unit: str
    def __init__(self, name: _Optional[str] = ..., description: _Optional[str] = ..., type: _Optional[_Union[ParameterType, str]] = ..., required: bool = ..., default_value: _Optional[str] = ..., enum_values: _Optional[_Iterable[str]] = ..., unit: _Optional[str] = ...) -> None: ...

class SequenceCapability(_message.Message):
    __slots__ = ("name", "description", "params")
    NAME_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    PARAMS_FIELD_NUMBER: _ClassVar[int]
    name: str
    description: str
    params: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, name: _Optional[str] = ..., description: _Optional[str] = ..., params: _Optional[_Iterable[str]] = ...) -> None: ...

class InstrumentCapabilities(_message.Message):
    __slots__ = ("instrument_id", "has_profile", "profile_key", "manufacturer", "model", "instrument_class", "commands", "sequences", "settings")
    class SettingsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    INSTRUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    HAS_PROFILE_FIELD_NUMBER: _ClassVar[int]
    PROFILE_KEY_FIELD_NUMBER: _ClassVar[int]
    MANUFACTURER_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_CLASS_FIELD_NUMBER: _ClassVar[int]
    COMMANDS_FIELD_NUMBER: _ClassVar[int]
    SEQUENCES_FIELD_NUMBER: _ClassVar[int]
    SETTINGS_FIELD_NUMBER: _ClassVar[int]
    instrument_id: str
    has_profile: bool
    profile_key: str
    manufacturer: str
    model: str
    instrument_class: str
    commands: _containers.RepeatedCompositeFieldContainer[CommandCapability]
    sequences: _containers.RepeatedCompositeFieldContainer[SequenceCapability]
    settings: _containers.ScalarMap[str, str]
    def __init__(self, instrument_id: _Optional[str] = ..., has_profile: bool = ..., profile_key: _Optional[str] = ..., manufacturer: _Optional[str] = ..., model: _Optional[str] = ..., instrument_class: _Optional[str] = ..., commands: _Optional[_Iterable[_Union[CommandCapability, _Mapping]]] = ..., sequences: _Optional[_Iterable[_Union[SequenceCapability, _Mapping]]] = ..., settings: _Optional[_Mapping[str, str]] = ...) -> None: ...

class SendCommandRequest(_message.Message):
    __slots__ = ("command_id", "scpi_command", "instrument_id", "timeout_ms")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    SCPI_COMMAND_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    scpi_command: str
    instrument_id: str
    timeout_ms: int
    def __init__(self, command_id: _Optional[str] = ..., scpi_command: _Optional[str] = ..., instrument_id: _Optional[str] = ..., timeout_ms: _Optional[int] = ...) -> None: ...

class SendCommandResponse(_message.Message):
    __slots__ = ("command_id", "response", "error", "status", "execution_time_ms")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    response: str
    error: str
    status: str
    execution_time_ms: int
    def __init__(self, command_id: _Optional[str] = ..., response: _Optional[str] = ..., error: _Optional[str] = ..., status: _Optional[str] = ..., execution_time_ms: _Optional[int] = ...) -> None: ...

class ListInstrumentsRequest(_message.Message):
    __slots__ = ("filter",)
    FILTER_FIELD_NUMBER: _ClassVar[int]
    filter: str
    def __init__(self, filter: _Optional[str] = ...) -> None: ...

class ListInstrumentsResponse(_message.Message):
    __slots__ = ("instruments", "edge_id")
    INSTRUMENTS_FIELD_NUMBER: _ClassVar[int]
    EDGE_ID_FIELD_NUMBER: _ClassVar[int]
    instruments: _containers.RepeatedCompositeFieldContainer[Instrument]
    edge_id: str
    def __init__(self, instruments: _Optional[_Iterable[_Union[Instrument, _Mapping]]] = ..., edge_id: _Optional[str] = ...) -> None: ...

class GetInstrumentRequest(_message.Message):
    __slots__ = ("instrument_id",)
    INSTRUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    instrument_id: str
    def __init__(self, instrument_id: _Optional[str] = ...) -> None: ...

class ScanInstrumentsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ScanInstrumentsResponse(_message.Message):
    __slots__ = ("instruments",)
    INSTRUMENTS_FIELD_NUMBER: _ClassVar[int]
    instruments: _containers.RepeatedCompositeFieldContainer[Instrument]
    def __init__(self, instruments: _Optional[_Iterable[_Union[Instrument, _Mapping]]] = ...) -> None: ...

class GetCapabilitiesRequest(_message.Message):
    __slots__ = ("instrument_id", "instrument_class")
    INSTRUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_CLASS_FIELD_NUMBER: _ClassVar[int]
    instrument_id: str
    instrument_class: str
    def __init__(self, instrument_id: _Optional[str] = ..., instrument_class: _Optional[str] = ...) -> None: ...

class GetCapabilitiesResponse(_message.Message):
    __slots__ = ("capabilities", "edge_id")
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    EDGE_ID_FIELD_NUMBER: _ClassVar[int]
    capabilities: _containers.RepeatedCompositeFieldContainer[InstrumentCapabilities]
    edge_id: str
    def __init__(self, capabilities: _Optional[_Iterable[_Union[InstrumentCapabilities, _Mapping]]] = ..., edge_id: _Optional[str] = ...) -> None: ...

class ExecuteCommandRequest(_message.Message):
    __slots__ = ("command_id", "instrument_id", "command_name", "parameters", "is_query", "timeout_ms")
    class ParametersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    COMMAND_NAME_FIELD_NUMBER: _ClassVar[int]
    PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    IS_QUERY_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    instrument_id: str
    command_name: str
    parameters: _containers.ScalarMap[str, str]
    is_query: bool
    timeout_ms: int
    def __init__(self, command_id: _Optional[str] = ..., instrument_id: _Optional[str] = ..., command_name: _Optional[str] = ..., parameters: _Optional[_Mapping[str, str]] = ..., is_query: bool = ..., timeout_ms: _Optional[int] = ...) -> None: ...

class VectorData(_message.Message):
    __slots__ = ("y_data", "y_dtype", "y_length", "x_start", "x_increment", "x_unit", "y_unit", "x_name", "y_scale", "y_offset")
    Y_DATA_FIELD_NUMBER: _ClassVar[int]
    Y_DTYPE_FIELD_NUMBER: _ClassVar[int]
    Y_LENGTH_FIELD_NUMBER: _ClassVar[int]
    X_START_FIELD_NUMBER: _ClassVar[int]
    X_INCREMENT_FIELD_NUMBER: _ClassVar[int]
    X_UNIT_FIELD_NUMBER: _ClassVar[int]
    Y_UNIT_FIELD_NUMBER: _ClassVar[int]
    X_NAME_FIELD_NUMBER: _ClassVar[int]
    Y_SCALE_FIELD_NUMBER: _ClassVar[int]
    Y_OFFSET_FIELD_NUMBER: _ClassVar[int]
    y_data: bytes
    y_dtype: str
    y_length: int
    x_start: float
    x_increment: float
    x_unit: str
    y_unit: str
    x_name: str
    y_scale: float
    y_offset: float
    def __init__(self, y_data: _Optional[bytes] = ..., y_dtype: _Optional[str] = ..., y_length: _Optional[int] = ..., x_start: _Optional[float] = ..., x_increment: _Optional[float] = ..., x_unit: _Optional[str] = ..., y_unit: _Optional[str] = ..., x_name: _Optional[str] = ..., y_scale: _Optional[float] = ..., y_offset: _Optional[float] = ...) -> None: ...

class ExecuteCommandResponse(_message.Message):
    __slots__ = ("command_id", "success", "data", "error_message", "execution_time_ms", "scpi_command", "vector_data")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    SCPI_COMMAND_FIELD_NUMBER: _ClassVar[int]
    VECTOR_DATA_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    success: bool
    data: str
    error_message: str
    execution_time_ms: int
    scpi_command: str
    vector_data: VectorData
    def __init__(self, command_id: _Optional[str] = ..., success: bool = ..., data: _Optional[str] = ..., error_message: _Optional[str] = ..., execution_time_ms: _Optional[int] = ..., scpi_command: _Optional[str] = ..., vector_data: _Optional[_Union[VectorData, _Mapping]] = ...) -> None: ...

class ExecuteSequenceRequest(_message.Message):
    __slots__ = ("sequence_id", "instrument_id", "sequence_name", "parameters", "timeout_ms")
    class ParametersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    SEQUENCE_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_NAME_FIELD_NUMBER: _ClassVar[int]
    PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    sequence_id: str
    instrument_id: str
    sequence_name: str
    parameters: _containers.ScalarMap[str, str]
    timeout_ms: int
    def __init__(self, sequence_id: _Optional[str] = ..., instrument_id: _Optional[str] = ..., sequence_name: _Optional[str] = ..., parameters: _Optional[_Mapping[str, str]] = ..., timeout_ms: _Optional[int] = ...) -> None: ...

class ExecuteSequenceResponse(_message.Message):
    __slots__ = ("sequence_id", "result", "error", "status", "execution_time_ms", "steps_executed")
    SEQUENCE_ID_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    STEPS_EXECUTED_FIELD_NUMBER: _ClassVar[int]
    sequence_id: str
    result: str
    error: str
    status: str
    execution_time_ms: int
    steps_executed: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, sequence_id: _Optional[str] = ..., result: _Optional[str] = ..., error: _Optional[str] = ..., status: _Optional[str] = ..., execution_time_ms: _Optional[int] = ..., steps_executed: _Optional[_Iterable[str]] = ...) -> None: ...

class StreamMeasurementRequest(_message.Message):
    __slots__ = ("stream_id", "instrument_id", "command_name", "interval_ms", "timeout_ms", "parameters")
    class ParametersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    COMMAND_NAME_FIELD_NUMBER: _ClassVar[int]
    INTERVAL_MS_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    stream_id: str
    instrument_id: str
    command_name: str
    interval_ms: int
    timeout_ms: int
    parameters: _containers.ScalarMap[str, str]
    def __init__(self, stream_id: _Optional[str] = ..., instrument_id: _Optional[str] = ..., command_name: _Optional[str] = ..., interval_ms: _Optional[int] = ..., timeout_ms: _Optional[int] = ..., parameters: _Optional[_Mapping[str, str]] = ...) -> None: ...

class MeasurementDataPoint(_message.Message):
    __slots__ = ("stream_id", "value", "timestamp_ms", "unit", "error", "status", "values")
    class ValuesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: float
        def __init__(self, key: _Optional[str] = ..., value: _Optional[float] = ...) -> None: ...
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_MS_FIELD_NUMBER: _ClassVar[int]
    UNIT_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    VALUES_FIELD_NUMBER: _ClassVar[int]
    stream_id: str
    value: float
    timestamp_ms: int
    unit: str
    error: str
    status: str
    values: _containers.ScalarMap[str, float]
    def __init__(self, stream_id: _Optional[str] = ..., value: _Optional[float] = ..., timestamp_ms: _Optional[int] = ..., unit: _Optional[str] = ..., error: _Optional[str] = ..., status: _Optional[str] = ..., values: _Optional[_Mapping[str, float]] = ...) -> None: ...

class StopStreamRequest(_message.Message):
    __slots__ = ("stream_id",)
    STREAM_ID_FIELD_NUMBER: _ClassVar[int]
    stream_id: str
    def __init__(self, stream_id: _Optional[str] = ...) -> None: ...

class StopStreamResponse(_message.Message):
    __slots__ = ("success",)
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    success: bool
    def __init__(self, success: bool = ...) -> None: ...

class GetStatusRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class EdgeStatus(_message.Message):
    __slots__ = ("edge_id", "hostname", "status", "instrument_count", "uptime_seconds", "version", "os_info", "cpu_usage", "memory_usage")
    EDGE_ID_FIELD_NUMBER: _ClassVar[int]
    HOSTNAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_COUNT_FIELD_NUMBER: _ClassVar[int]
    UPTIME_SECONDS_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    OS_INFO_FIELD_NUMBER: _ClassVar[int]
    CPU_USAGE_FIELD_NUMBER: _ClassVar[int]
    MEMORY_USAGE_FIELD_NUMBER: _ClassVar[int]
    edge_id: str
    hostname: str
    status: EdgeStatusCode
    instrument_count: int
    uptime_seconds: int
    version: str
    os_info: str
    cpu_usage: float
    memory_usage: float
    def __init__(self, edge_id: _Optional[str] = ..., hostname: _Optional[str] = ..., status: _Optional[_Union[EdgeStatusCode, str]] = ..., instrument_count: _Optional[int] = ..., uptime_seconds: _Optional[int] = ..., version: _Optional[str] = ..., os_info: _Optional[str] = ..., cpu_usage: _Optional[float] = ..., memory_usage: _Optional[float] = ...) -> None: ...

class PingRequest(_message.Message):
    __slots__ = ("timestamp",)
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    timestamp: _timestamp_pb2.Timestamp
    def __init__(self, timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class PingResponse(_message.Message):
    __slots__ = ("timestamp",)
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    timestamp: _timestamp_pb2.Timestamp
    def __init__(self, timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class RegisterEdgeRequest(_message.Message):
    __slots__ = ("edge_id", "hostname", "instruments", "tailscale_ip", "grpc_port", "ws_port", "metadata")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    EDGE_ID_FIELD_NUMBER: _ClassVar[int]
    HOSTNAME_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENTS_FIELD_NUMBER: _ClassVar[int]
    TAILSCALE_IP_FIELD_NUMBER: _ClassVar[int]
    GRPC_PORT_FIELD_NUMBER: _ClassVar[int]
    WS_PORT_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    edge_id: str
    hostname: str
    instruments: _containers.RepeatedCompositeFieldContainer[Instrument]
    tailscale_ip: str
    grpc_port: int
    ws_port: int
    metadata: _containers.ScalarMap[str, str]
    def __init__(self, edge_id: _Optional[str] = ..., hostname: _Optional[str] = ..., instruments: _Optional[_Iterable[_Union[Instrument, _Mapping]]] = ..., tailscale_ip: _Optional[str] = ..., grpc_port: _Optional[int] = ..., ws_port: _Optional[int] = ..., metadata: _Optional[_Mapping[str, str]] = ...) -> None: ...

class RegisterEdgeResponse(_message.Message):
    __slots__ = ("success", "message", "assigned_edge_id")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ASSIGNED_EDGE_ID_FIELD_NUMBER: _ClassVar[int]
    success: bool
    message: str
    assigned_edge_id: str
    def __init__(self, success: bool = ..., message: _Optional[str] = ..., assigned_edge_id: _Optional[str] = ...) -> None: ...

class HeartbeatRequest(_message.Message):
    __slots__ = ("edge_id", "instrument_count", "cpu_usage", "memory_usage")
    EDGE_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_COUNT_FIELD_NUMBER: _ClassVar[int]
    CPU_USAGE_FIELD_NUMBER: _ClassVar[int]
    MEMORY_USAGE_FIELD_NUMBER: _ClassVar[int]
    edge_id: str
    instrument_count: int
    cpu_usage: float
    memory_usage: float
    def __init__(self, edge_id: _Optional[str] = ..., instrument_count: _Optional[int] = ..., cpu_usage: _Optional[float] = ..., memory_usage: _Optional[float] = ...) -> None: ...

class HeartbeatResponse(_message.Message):
    __slots__ = ("acknowledged", "server_timestamp_ms", "config_updates")
    class ConfigUpdatesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ACKNOWLEDGED_FIELD_NUMBER: _ClassVar[int]
    SERVER_TIMESTAMP_MS_FIELD_NUMBER: _ClassVar[int]
    CONFIG_UPDATES_FIELD_NUMBER: _ClassVar[int]
    acknowledged: bool
    server_timestamp_ms: int
    config_updates: _containers.ScalarMap[str, str]
    def __init__(self, acknowledged: bool = ..., server_timestamp_ms: _Optional[int] = ..., config_updates: _Optional[_Mapping[str, str]] = ...) -> None: ...

class GetWebcamSnapshotRequest(_message.Message):
    __slots__ = ("camera_url",)
    CAMERA_URL_FIELD_NUMBER: _ClassVar[int]
    camera_url: str
    def __init__(self, camera_url: _Optional[str] = ...) -> None: ...

class GetWebcamSnapshotResponse(_message.Message):
    __slots__ = ("image_data", "timestamp_ms", "content_type", "error")
    IMAGE_DATA_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_MS_FIELD_NUMBER: _ClassVar[int]
    CONTENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    image_data: bytes
    timestamp_ms: int
    content_type: str
    error: str
    def __init__(self, image_data: _Optional[bytes] = ..., timestamp_ms: _Optional[int] = ..., content_type: _Optional[str] = ..., error: _Optional[str] = ...) -> None: ...

class ProxySDKCallRequest(_message.Message):
    __slots__ = ("call_id", "instrument_id", "module", "method", "args", "kwargs", "timeout_ms")
    class KwargsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: _struct_pb2.Value
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ...) -> None: ...
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    INSTRUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    MODULE_FIELD_NUMBER: _ClassVar[int]
    METHOD_FIELD_NUMBER: _ClassVar[int]
    ARGS_FIELD_NUMBER: _ClassVar[int]
    KWARGS_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    instrument_id: str
    module: str
    method: str
    args: _containers.RepeatedCompositeFieldContainer[_struct_pb2.Value]
    kwargs: _containers.MessageMap[str, _struct_pb2.Value]
    timeout_ms: int
    def __init__(self, call_id: _Optional[str] = ..., instrument_id: _Optional[str] = ..., module: _Optional[str] = ..., method: _Optional[str] = ..., args: _Optional[_Iterable[_Union[_struct_pb2.Value, _Mapping]]] = ..., kwargs: _Optional[_Mapping[str, _struct_pb2.Value]] = ..., timeout_ms: _Optional[int] = ...) -> None: ...

class ProxySDKCallResponse(_message.Message):
    __slots__ = ("call_id", "success", "result", "error_message", "execution_time_ms")
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    EXECUTION_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    success: bool
    result: _struct_pb2.Value
    error_message: str
    execution_time_ms: int
    def __init__(self, call_id: _Optional[str] = ..., success: bool = ..., result: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., error_message: _Optional[str] = ..., execution_time_ms: _Optional[int] = ...) -> None: ...

class StartSweepRequest(_message.Message):
    __slots__ = ("instrument_id", "command_name", "target_value", "sweep_rate", "extra_parameters")
    class ExtraParametersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    INSTRUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    COMMAND_NAME_FIELD_NUMBER: _ClassVar[int]
    TARGET_VALUE_FIELD_NUMBER: _ClassVar[int]
    SWEEP_RATE_FIELD_NUMBER: _ClassVar[int]
    EXTRA_PARAMETERS_FIELD_NUMBER: _ClassVar[int]
    instrument_id: str
    command_name: str
    target_value: float
    sweep_rate: float
    extra_parameters: _containers.ScalarMap[str, str]
    def __init__(self, instrument_id: _Optional[str] = ..., command_name: _Optional[str] = ..., target_value: _Optional[float] = ..., sweep_rate: _Optional[float] = ..., extra_parameters: _Optional[_Mapping[str, str]] = ...) -> None: ...

class StartSweepResponse(_message.Message):
    __slots__ = ("sweep_id", "accepted", "error")
    SWEEP_ID_FIELD_NUMBER: _ClassVar[int]
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    sweep_id: str
    accepted: bool
    error: str
    def __init__(self, sweep_id: _Optional[str] = ..., accepted: bool = ..., error: _Optional[str] = ...) -> None: ...

class GetSweepStatusRequest(_message.Message):
    __slots__ = ("sweep_id",)
    SWEEP_ID_FIELD_NUMBER: _ClassVar[int]
    sweep_id: str
    def __init__(self, sweep_id: _Optional[str] = ...) -> None: ...

class SweepStatusResponse(_message.Message):
    __slots__ = ("sweep_id", "status", "current_value", "target_value", "sweep_rate", "error")
    SWEEP_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    CURRENT_VALUE_FIELD_NUMBER: _ClassVar[int]
    TARGET_VALUE_FIELD_NUMBER: _ClassVar[int]
    SWEEP_RATE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    sweep_id: str
    status: str
    current_value: float
    target_value: float
    sweep_rate: float
    error: str
    def __init__(self, sweep_id: _Optional[str] = ..., status: _Optional[str] = ..., current_value: _Optional[float] = ..., target_value: _Optional[float] = ..., sweep_rate: _Optional[float] = ..., error: _Optional[str] = ...) -> None: ...

class StopSweepRequest(_message.Message):
    __slots__ = ("sweep_id",)
    SWEEP_ID_FIELD_NUMBER: _ClassVar[int]
    sweep_id: str
    def __init__(self, sweep_id: _Optional[str] = ...) -> None: ...

class StopSweepResponse(_message.Message):
    __slots__ = ("success", "status")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    success: bool
    status: str
    def __init__(self, success: bool = ..., status: _Optional[str] = ...) -> None: ...
