"""Standard-library HTTP plumbing: JSON calls, streamed multipart uploads and SSE parsing."""

from __future__ import annotations

import json
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator
from urllib import error as urlerror
from urllib import request as urlrequest

from cerebel.errors import CerebelError, error_for

CHUNK = 256 * 1024


def _decode(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"message": raw[:200].decode("utf-8", "replace")}


def open_request(req: urlrequest.Request, timeout: float | None):
    """Send a request; turn HTTP and network failures into CerebelError subclasses."""
    try:
        return urlrequest.urlopen(req, timeout=timeout)
    except urlerror.HTTPError as exc:
        payload = _decode(exc.read())
        raise error_for(exc.code, payload) from None
    except urlerror.URLError as exc:
        raise CerebelError(f"Could not reach the Cerebel API: {exc.reason}") from None
    except TimeoutError:
        raise CerebelError("The Cerebel API did not answer in time.") from None


def call_json(req: urlrequest.Request, timeout: float | None, *, allow_status: Iterable[int] = ()) -> dict[str, Any]:
    """Send a request and return the decoded JSON body.

    Statuses in `allow_status` return their body instead of raising (health answers 503
    with a useful payload).
    """
    allowed = set(allow_status)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return _decode(resp.read())
    except urlerror.HTTPError as exc:
        payload = _decode(exc.read())
        if exc.code in allowed and isinstance(payload, dict):
            return payload
        raise error_for(exc.code, payload) from None
    except urlerror.URLError as exc:
        raise CerebelError(f"Could not reach the Cerebel API: {exc.reason}") from None
    except TimeoutError:
        raise CerebelError("The Cerebel API did not answer in time.") from None


class MultipartFile:
    """A multipart/form-data body streamed from disk, so large videos never sit in memory."""

    def __init__(self, field: str, path: Path, *, filename: str | None = None, fields: dict[str, str] | None = None) -> None:
        self.boundary = "cerebel-" + uuid.uuid4().hex
        self.path = Path(path)
        name = filename or self.path.name
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        parts = []
        for key, value in (fields or {}).items():
            parts.append(
                f"--{self.boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
            )
        safe_name = name.replace('"', "").replace("\r", "").replace("\n", "")
        parts.append(
            (
                f"--{self.boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{safe_name}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode()
        )
        self._head = b"".join(parts)
        self._tail = f"\r\n--{self.boundary}--\r\n".encode()
        self.length = len(self._head) + os.path.getsize(self.path) + len(self._tail)

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    def __iter__(self) -> Iterator[bytes]:
        yield self._head
        with self.path.open("rb") as handle:
            yield from _chunks(handle)
        yield self._tail


def _chunks(handle: BinaryIO) -> Iterator[bytes]:
    while True:
        block = handle.read(CHUNK)
        if not block:
            return
        yield block


def iter_sse(resp) -> Iterator[dict[str, Any]]:
    """Yield the JSON object of each `data:` frame of a text/event-stream response."""
    data_lines: list[str] = []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line == "" and data_lines:
            text = "\n".join(data_lines)
            data_lines = []
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event
    if data_lines:
        try:
            event = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return
        if isinstance(event, dict):
            yield event
