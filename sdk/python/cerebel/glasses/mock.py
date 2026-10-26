"""MockGlasses: a stand-in for the glasses end of the 9080 link, for tests and desk development.

Speaks the same frames as cerebel-ar-glasses/nega-ios/Tools/mock_glasses_server.py: sends
`hello` on connect, records every frame it receives, and can send key presses.
"""

from __future__ import annotations

import socket
import threading
from typing import Any

from cerebel.glasses.protocol import FrameDecoder, ProtocolError, encode_frame


class MockGlasses:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, *, fw: str = "mock-1.0", battery: int = 80, echo: bool = False) -> None:
        self.fw = fw
        self.battery = battery
        self.echo = echo
        self.received: list[dict[str, Any]] = []
        self._cond = threading.Condition()
        self._clients: list[socket.socket] = []
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((host, port))
        self._server.listen(4)
        self.host, self.port = self._server.getsockname()[:2]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "MockGlasses":
        self._thread = threading.Thread(target=self._accept_loop, name="mock-glasses", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass
        for conn in list(self._clients):
            try:
                conn.close()
            except OSError:
                pass

    def __enter__(self) -> "MockGlasses":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def huds(self) -> list[dict[str, Any]]:
        with self._cond:
            return [m for m in self.received if m.get("type") == "hud"]

    def wait_for(self, count: int, *, kind: str = "hud", timeout: float = 5.0) -> list[dict[str, Any]]:
        """Block until `count` frames of `kind` have arrived; return them."""
        with self._cond:
            self._cond.wait_for(lambda: len([m for m in self.received if m.get("type") == kind]) >= count, timeout)
            return [m for m in self.received if m.get("type") == kind]

    def press(self, action: str) -> None:
        """Send a key event (photo, rec_start, rec_stop) to every connected host."""
        self._broadcast({"type": "key", "action": action})

    def _broadcast(self, message: dict[str, Any]) -> None:
        frame = encode_frame(message)
        for conn in list(self._clients):
            try:
                conn.sendall(frame)
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._server.accept()
            except OSError:
                return
            self._clients.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        decoder = FrameDecoder()
        try:
            conn.sendall(encode_frame({"type": "hello", "fw": self.fw, "battery": self.battery}))
            while not self._stop.is_set():
                data = conn.recv(64 * 1024)
                if not data:
                    break
                for message in decoder.feed(data):
                    with self._cond:
                        self.received.append(message)
                        self._cond.notify_all()
                    if self.echo:
                        print(f"[glasses] {message}", flush=True)
        except (OSError, ProtocolError):
            pass
        finally:
            if conn in self._clients:
                self._clients.remove(conn)
            conn.close()
