"""Byte transports: serial (pyserial) and TCP.

The client only needs four operations: open, close, write, and
"read whatever is available, waiting at most N seconds for the first
byte". All transport-level failures are converted to typed exceptions
from errors.py so the client's reconnect logic has one thing to catch.
"""

from __future__ import annotations

import logging
import socket
from abc import ABC, abstractmethod

from .errors import TransportClosedError, TransportError

log = logging.getLogger(__name__)

DEFAULT_TCP_PORT = 6785  # Campbell PakBus/TCP service port


class Transport(ABC):
    """Abstract byte-stream transport."""

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @abstractmethod
    def read_available(self, timeout: float) -> bytes:
        """Return at least one byte if any arrive within ``timeout``.

        Returns ``b""`` on timeout (caller decides whether that is an
        error). Raises TransportError if the link is broken.
        """

    def describe(self) -> str:
        return type(self).__name__


class SerialTransport(Transport):
    """pyserial-based transport (CR6 USB virtual COM port or RS-232).

    The CR6's USB interface enumerates as a CDC virtual COM port; the
    baud rate setting is then irrelevant, but it matters for real
    RS-232 links.
    """

    def __init__(self, port: str, baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self._serial = None

    def open(self) -> None:
        import serial  # imported here so tests never need the package
        try:
            self._serial = serial.Serial(
                port=self.port, baudrate=self.baudrate,
                bytesize=8, parity="N", stopbits=1,
                timeout=0.05,          # granular reads; we loop ourselves
                write_timeout=2.0)
        except (serial.SerialException, OSError, ValueError) as exc:
            self._serial = None
            raise TransportError(
                f"cannot open serial port {self.port}: {exc}") from exc
        log.info("opened %s at %d baud", self.port, self.baudrate)

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception as exc:  # port may already be gone (unplug)
                log.warning("error closing %s: %s", self.port, exc)
            self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def write(self, data: bytes) -> None:
        import serial
        if not self.is_open:
            raise TransportClosedError(f"serial port {self.port} not open")
        try:
            self._serial.write(data)
        except (serial.SerialException, OSError) as exc:
            raise TransportError(
                f"write failed on {self.port}: {exc}") from exc

    def read_available(self, timeout: float) -> bytes:
        import serial
        if not self.is_open:
            raise TransportClosedError(f"serial port {self.port} not open")
        try:
            self._serial.timeout = timeout
            first = self._serial.read(1)
            if not first:
                return b""
            pending = self._serial.in_waiting
            return first + (self._serial.read(pending) if pending else b"")
        except (serial.SerialException, OSError) as exc:
            raise TransportError(
                f"read failed on {self.port} (cable unplugged?): "
                f"{exc}") from exc

    def describe(self) -> str:
        return f"serial {self.port} @ {self.baudrate}"


class TcpTransport(Transport):
    """TCP transport for Ethernet-connected loggers (default port 6785).

    Implemented now; validated against real hardware in a later
    milestone (the bench CR6 has no IP configured).
    """

    def __init__(self, host: str, port: int = DEFAULT_TCP_PORT,
                 connect_timeout: float = 5.0):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._sock: socket.socket | None = None

    def open(self) -> None:
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            self._sock = None
            raise TransportError(
                f"cannot connect to {self.host}:{self.port}: {exc}") from exc
        log.info("connected to %s:%d", self.host, self.port)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError as exc:
                log.warning("error closing socket: %s", exc)
            self._sock = None

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def write(self, data: bytes) -> None:
        if self._sock is None:
            raise TransportClosedError("TCP transport not open")
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise TransportError(f"TCP write failed: {exc}") from exc

    def read_available(self, timeout: float) -> bytes:
        if self._sock is None:
            raise TransportClosedError("TCP transport not open")
        try:
            self._sock.settimeout(timeout)
            first = self._sock.recv(1)
            if not first:
                raise TransportError("TCP connection closed by peer")
            self._sock.settimeout(0.0)  # drain whatever else arrived
            chunks = [first]
            try:
                while True:
                    more = self._sock.recv(4096)
                    if not more:
                        break
                    chunks.append(more)
            except (BlockingIOError, TimeoutError):
                pass
            return b"".join(chunks)
        except socket.timeout:
            return b""
        except OSError as exc:
            raise TransportError(f"TCP read failed: {exc}") from exc

    def describe(self) -> str:
        return f"tcp {self.host}:{self.port}"
