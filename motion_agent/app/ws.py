"""Minimal RFC 6455 WebSocket, standard library only.

Server side: upgrade a BaseHTTPRequestHandler connection (the live voice coach).
Client side: connect to a wss:// speech service from the server. Text and binary
messages, fragmentation, ping/pong and the close handshake; no extensions.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
from typing import Any
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class WebSocketClosed(Exception):
    pass


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


class WebSocket:
    """One open connection. recv() from a single thread; send_* from any thread."""

    def __init__(self, sock: socket.socket, reader: Any, *, client: bool) -> None:
        self.sock = sock
        self.reader = reader  # buffered reader: may already hold bytes read past the handshake
        self.client = client  # clients mask what they send; servers must not
        self.closed = False
        self._send_lock = threading.Lock()

    # ------------------------------------------------------------------ send
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        mask_bit = 0x80 if self.client else 0
        n = len(payload)
        if n < 126:
            header.append(mask_bit | n)
        elif n < 65536:
            header.append(mask_bit | 126)
            header += struct.pack("!H", n)
        else:
            header.append(mask_bit | 127)
            header += struct.pack("!Q", n)
        if self.client:
            mask = os.urandom(4)
            header += mask
            payload = _xor(payload, mask)
        with self._send_lock:
            if self.closed and opcode != OP_CLOSE:
                raise WebSocketClosed("socket closed")
            self.sock.sendall(bytes(header) + payload)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self._send_frame(OP_BINARY, bytes(data))

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:120])
        except OSError:
            pass
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ recv
    def _read_exact(self, n: int) -> bytes:
        data = self.reader.read(n) if n else b""
        if n and (not data or len(data) < n):
            raise WebSocketClosed("connection dropped")
        return data

    def _read_frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._read_exact(2)
        fin, opcode, masked, n = bool(b1 & 0x80), b1 & 0x0F, bool(b2 & 0x80), b2 & 0x7F
        if b1 & 0x70:
            raise WebSocketClosed("extensions are not supported")
        if n == 126:
            (n,) = struct.unpack("!H", self._read_exact(2))
        elif n == 127:
            (n,) = struct.unpack("!Q", self._read_exact(8))
        if n > MAX_MESSAGE_BYTES:
            raise WebSocketClosed("frame too large")
        if not self.client and not masked:
            raise WebSocketClosed("client frames must be masked")
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(n)
        if masked:
            payload = _xor(payload, mask)
        return fin, opcode, payload

    def recv(self) -> tuple[int, bytes] | None:
        """Next complete message as (OP_TEXT | OP_BINARY, payload), or None once closed."""
        if self.closed:
            return None
        message_op, parts, size = None, [], 0
        while True:
            try:
                fin, opcode, payload = self._read_frame()
            except (WebSocketClosed, OSError, ValueError):
                self.closed = True
                return None
            if opcode == OP_PING:
                try:
                    self._send_frame(OP_PONG, payload)
                except (WebSocketClosed, OSError):
                    pass
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1000
                self.close(code)
                return None
            if opcode in (OP_TEXT, OP_BINARY):
                message_op, parts, size = opcode, [payload], len(payload)
            elif opcode == OP_CONT and message_op is not None:
                parts.append(payload)
                size += len(payload)
            else:
                self.close(1002)
                return None
            if size > MAX_MESSAGE_BYTES:
                self.close(1009)
                return None
            if fin:
                return message_op, b"".join(parts)


def _xor(data: bytes, mask: bytes) -> bytes:
    if not data:
        return b""
    n = len(data)
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


def server_upgrade(handler: Any) -> WebSocket | None:
    """Answer a GET Upgrade request on a BaseHTTPRequestHandler. Returns None after a 400."""
    headers = handler.headers
    key = (headers.get("Sec-WebSocket-Key") or "").strip()
    upgrade = (headers.get("Upgrade") or "").lower()
    connection = (headers.get("Connection") or "").lower()
    version = (headers.get("Sec-WebSocket-Version") or "").strip()
    if upgrade != "websocket" or "upgrade" not in connection or not key or version != "13":
        handler.send_response(400)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return None
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept_key(key))
    handler.end_headers()
    handler.wfile.flush()
    handler.close_connection = True
    return WebSocket(handler.connection, handler.rfile, client=False)


def connect(url: str, headers: dict[str, str] | None = None, timeout: float = 10.0) -> WebSocket:
    """Open a client connection to ws:// or wss://. Raises OSError / WebSocketClosed on failure."""
    parsed = urlparse(url)
    secure = parsed.scheme == "wss"
    host = parsed.hostname or ""
    port = parsed.port or (443 if secure else 80)
    raw = socket.create_connection((host, port), timeout=timeout)
    sock: socket.socket = raw
    if secure:
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    path = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {parsed.netloc}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
    reader = sock.makefile("rb")
    status = reader.readline().decode("latin-1").strip()
    response_headers: dict[str, str] = {}
    while True:
        line = reader.readline().decode("latin-1")
        if line in ("\r\n", "\n", ""):
            break
        name, _, value = line.partition(":")
        response_headers[name.strip().lower()] = value.strip()
    if " 101 " not in status + " ":
        sock.close()
        raise WebSocketClosed(f"handshake refused: {status[:60]}")
    if response_headers.get("sec-websocket-accept") != accept_key(key):
        sock.close()
        raise WebSocketClosed("bad handshake accept key")
    sock.settimeout(None)
    return WebSocket(sock, reader, client=True)
