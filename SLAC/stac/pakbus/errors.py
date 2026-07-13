"""Typed exceptions for the STAC PakBus/BMP5 stack.

Every failure mode in the library raises one of these. Nothing in the
library uses a bare ``except`` and no malformed packet is silently
dropped: framing-level rejects are counted by the deframer and logged
with raw bytes, everything else raises.
"""

from __future__ import annotations


class StacError(Exception):
    """Base class for all STAC pakbus errors."""


# ---------------------------------------------------------------- transport

class TransportError(StacError):
    """The underlying byte transport failed (port gone, socket reset...)."""


class TransportTimeoutError(TransportError):
    """No (or not enough) data arrived within the allowed time."""


class TransportClosedError(TransportError):
    """Operation attempted on a transport that is not open."""


# ------------------------------------------------------------------ framing

class FramingError(StacError):
    """A byte sequence could not be interpreted as a PakBus frame.

    Note: the incremental deframer *counts and logs* bad frames rather
    than raising, so a corrupted packet can never abort a receive loop.
    This exception is used by the strict one-shot helpers (tests, fixture
    tooling).
    """

    def __init__(self, message: str, raw: bytes = b""):
        super().__init__(message)
        self.raw = raw


class SignatureError(FramingError):
    """Frame signature did not verify to zero. Frame must be discarded."""


# ----------------------------------------------------------------- protocol

class ProtocolError(StacError):
    """The peer sent something structurally valid but protocol-wrong."""


class UnexpectedMessageError(ProtocolError):
    """A response arrived whose MsgType we cannot map to a transaction."""

    def __init__(self, message: str, msg_type: int | None = None,
                 tran_nbr: int | None = None, raw: bytes = b""):
        super().__init__(message)
        self.msg_type = msg_type
        self.tran_nbr = tran_nbr
        self.raw = raw


class TransactionTimeoutError(ProtocolError):
    """No matching response arrived before the transaction deadline."""


class DeliveryFailureError(ProtocolError):
    """PakCtrl Delivery Failure (MsgType 0x81) received from the network.

    Failure codes (BMP5 Transparent Commands sec. 2.2.1):
      0x01 unreachable destination
      0x02 unreachable higher-level protocol
      0x03 queue overflow
      0x04 unimplemented command or MsgType
      0x05 malformed message
      0x06 link failed
    """

    CODES = {
        0x01: "destination unreachable",
        0x02: "unreachable higher-level protocol",
        0x03: "queue overflow (timed out or out of resources)",
        0x04: "unimplemented command or MsgType",
        0x05: "malformed message",
        0x06: "link failed",
    }

    def __init__(self, err_code: int, raw: bytes = b""):
        text = self.CODES.get(err_code, "unknown failure code")
        super().__init__(f"delivery failure 0x{err_code:02x}: {text}")
        self.err_code = err_code
        self.raw = raw


class ResponseError(ProtocolError):
    """A BMP5/PakCtrl response carried a non-zero RespCode."""

    def __init__(self, operation: str, resp_code: int,
                 description: str = ""):
        text = description or f"response code 0x{resp_code:02x}"
        super().__init__(f"{operation}: {text}")
        self.operation = operation
        self.resp_code = resp_code


class PermissionDeniedError(ResponseError):
    """RespCode 0x01: security code rejected by the datalogger."""

    def __init__(self, operation: str):
        super().__init__(operation, 0x01, "permission denied "
                         "(check security code)")


class InvalidTableDefsError(ResponseError):
    """Collect Data RespCode 0x07: our table definition signature is stale.

    The datalogger program changed (or table defs were never fetched).
    The caller must re-fetch table definitions and retry.
    """

    def __init__(self, operation: str = "collect"):
        super().__init__(operation, 0x07,
                         "table definitions are stale; re-fetch .TDF")


# --------------------------------------------------------------- table defs

class TableDefsError(StacError):
    """Table definition file (.TDF) could not be parsed."""


class UnknownTableError(StacError):
    """A table name was requested that the datalogger does not report."""


class UnknownFieldError(StacError):
    """A field name was requested that the table does not contain."""


class ValueTypeError(StacError):
    """A value cannot be encoded/decoded as the requested PakBus type."""
