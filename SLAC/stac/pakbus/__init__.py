"""STAC PakBus/BMP5 communication library for Campbell CR6 dataloggers.

Milestone 1: datalogger communication layer only. See NOTES.md for
protocol-source provenance and BLOCKERS.md for known ambiguities.
"""

from .client import (ClientStats, ClockReading, MonitorStats, PakBusClient,
                     TableMonitor)
from .errors import (DeliveryFailureError, FramingError,
                     InvalidTableDefsError, PermissionDeniedError,
                     ProtocolError, ResponseError, SignatureError, StacError,
                     TableDefsError, TransactionTimeoutError, TransportError,
                     TransportTimeoutError, UnexpectedMessageError,
                     UnknownFieldError, UnknownTableError, ValueTypeError)
from .tabledefs import FieldDef, Record, TableDef
from .transport import SerialTransport, TcpTransport, Transport

__all__ = [
    "PakBusClient", "TableMonitor", "ClientStats", "MonitorStats",
    "ClockReading",
    "Transport", "SerialTransport", "TcpTransport",
    "TableDef", "FieldDef", "Record",
    "StacError", "TransportError", "TransportTimeoutError", "FramingError",
    "SignatureError", "ProtocolError", "UnexpectedMessageError",
    "TransactionTimeoutError", "DeliveryFailureError", "ResponseError",
    "PermissionDeniedError", "InvalidTableDefsError", "TableDefsError",
    "UnknownTableError", "UnknownFieldError", "ValueTypeError",
]

__version__ = "0.1.0"
