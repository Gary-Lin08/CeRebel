"""GlassesLink: the host side of the 9080 TCP link (connect, send HUD, receive keys and audio)."""

from __future__ import annotations

import base64
import logging
import socket
import threading
from typing import Any, Callable

from cerebel.glasses.protocol import DEFAULT_PORT, FrameDecoder, ProtocolError, encode_frame, hud, ping

LOG = logging.getLogger("cerebel.glasses")

Handler = Callable[[dict[str, Any]], None]


class GlassesLink:
    """One connection to a pair of glasses.

    Callbacks run on the reader thread; keep them short (hand long work to another thread).
    `on_key` gets the action string, `on_pcm` gets (pcm_bytes, sample_rate, channels).
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        connect_timeout: float = 5.0,
        ping_interval: float = 8.0,
        on_hello: Handler | None = None,
        on_key: Callable[[str], None] | None = None,
        on_pcm: Callable[[bytes, int, int], None] | None = None,
        on_message: Handler | None = None,
        on_close: Callable[[BaseException | None], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.ping_interval = ping_interval
        self.on_hello = on_hello
        self.on_key = on_key
        self.on_pcm = on_pcm
        self.on_message = on_message
        self.on_close = on_close
        self.hello: dict[str, Any] | None = None
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._stop.is_set()

    def connect(self) -> "GlassesLink":
        sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self._stop.clear()
        reader = threading.Thread(target=self._read_loop, name="glasses-reader", daemon=True)
        reader.start()
        self._threads = [reader]
        if self.ping_interval > 0:
            pinger = threading.Thread(target=self._ping_loop, name="glasses-ping", daemon=True)
            pinger.start()
            self._threads.append(pinger)
        return self

    def close(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def __enter__(self) -> "GlassesLink":
        return self.connect() if self._sock is None else self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def send(self, message: dict[str, Any]) -> None:
        frame = encode_frame(message)
        sock = self._sock
        if sock is None:
            raise ConnectionError("glasses link is not connected")
        with self._send_lock:
            sock.sendall(frame)

    def send_hud(self, *, mode: str = "info", you: str = "", reply: str = "", status: str = "") -> None:
        self.send(hud(mode=mode, you=you, reply=reply, status=status))

    def ping(self) -> None:
        self.send(ping())

    def _ping_loop(self) -> None:
        while not self._stop.wait(self.ping_interval):
            try:
                self.ping()
            except (OSError, ConnectionError):
                return

    def _read_loop(self) -> None:
        decoder = FrameDecoder()
        error: BaseException | None = None
        try:
            while not self._stop.is_set():
                sock = self._sock
                if sock is None:
                    break
                data = sock.recv(64 * 1024)
                if not data:
                    break
                for message in decoder.feed(data):
                    self._dispatch(message)
        except (OSError, ProtocolError) as exc:
            if not self._stop.is_set():
                error = exc
                LOG.warning("glasses link closed: %s", exc)
        finally:
            was_open = not self._stop.is_set()
            self._stop.set()
            self._sock = None
            if was_open and self.on_close is not None:
                self.on_close(error)

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        try:
            if kind == "hello":
                self.hello = message
                if self.on_hello is not None:
                    self.on_hello(message)
            elif kind == "key" and self.on_key is not None:
                self.on_key(str(message.get("action") or ""))
            elif kind == "pcm" and self.on_pcm is not None:
                pcm = base64.b64decode(str(message.get("data_b64") or ""), validate=False)
                self.on_pcm(pcm, int(message.get("sr") or 16000), int(message.get("ch") or 1))
            if self.on_message is not None:
                self.on_message(message)
        except Exception:  # noqa: BLE001
            # A failing callback must not take the link down.
            LOG.exception("glasses callback failed for %s", kind)
