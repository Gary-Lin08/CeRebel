"""Getting glasses recordings into a form the API accepts, and noticing new ones."""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterable, Iterator

# The glasses' encoder writes bare Annex-B streams (factory_*.h264); the API wants a container.
RAW_STREAM_SUFFIXES = frozenset({".h264", ".264", ".h265", ".265", ".hevc"})
VIDEO_PATTERNS = ("*.mp4", "*.mov", "*.m4v", "*.webm", "*.h264", "*.264", "*.h265", "*.265", "*.hevc")


def prepare_clip(path: str | Path, *, fps: float = 30.0, out_dir: str | Path | None = None, ffmpeg: str = "ffmpeg") -> Path:
    """Return a file the API accepts: containers as they are, raw H.264/H.265 wrapped into MP4.

    A raw stream has no timing, so `fps` sets it. Use the rate the glasses actually saved
    (the timestamp CSV of a recording shows it); the server rejects clips under 24 fps.
    """
    path = Path(path)
    if path.suffix.lower() not in RAW_STREAM_SUFFIXES:
        return path
    exe = shutil.which(ffmpeg) or (ffmpeg if Path(ffmpeg).is_file() else None)
    if exe is None:
        raise RuntimeError("ffmpeg is needed to wrap raw .h264/.h265 recordings into MP4; install it or pass ffmpeg=")
    dest_dir = Path(out_dir) if out_dir else path.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (path.stem + ".mp4")
    cmd = [
        exe, "-y", "-loglevel", "error",
        "-framerate", f"{float(fps):g}",
        "-i", str(path),
        "-c", "copy", "-movflags", "+faststart",
        str(dest),
    ]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if done.returncode != 0 or not dest.is_file():
        raise RuntimeError(f"ffmpeg could not wrap {path.name}: {done.stderr.strip()[:300]}")
    return dest


class FolderWatcher:
    """Report video files that appear in a folder, once their size has stopped changing.

    Files already present when the watcher starts are ignored unless `include_existing` is set.
    """

    def __init__(
        self,
        folder: str | Path,
        *,
        patterns: Iterable[str] = VIDEO_PATTERNS,
        settle_s: float = 2.0,
        include_existing: bool = False,
    ) -> None:
        self.folder = Path(folder)
        self.patterns = tuple(patterns)
        self.settle_s = settle_s
        self._sizes: dict[Path, tuple[int, float]] = {}
        self._done: set[Path] = set() if include_existing else set(self._candidates())

    def _candidates(self) -> list[Path]:
        if not self.folder.is_dir():
            return []
        return sorted(
            p for p in self.folder.iterdir()
            if p.is_file() and any(fnmatch.fnmatch(p.name.lower(), pat) for pat in self.patterns)
        )

    def poll(self) -> list[Path]:
        now = time.monotonic()
        ready: list[Path] = []
        for path in self._candidates():
            if path in self._done:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            seen = self._sizes.get(path)
            if seen is None or seen[0] != size:
                self._sizes[path] = (size, now)
                continue
            if size > 0 and now - seen[1] >= self.settle_s:
                self._done.add(path)
                self._sizes.pop(path, None)
                ready.append(path)
        return ready

    def watch(self, *, interval: float = 1.0, stop: threading.Event | None = None) -> Iterator[Path]:
        stop = stop or threading.Event()
        while not stop.is_set():
            yield from self.poll()
            stop.wait(interval)
