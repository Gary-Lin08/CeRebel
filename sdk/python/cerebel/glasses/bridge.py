"""GlassesBridge: recording in, one fix on the waveguide out.

Runs on the phone-side or computer-side host, never on the glasses (they have one Cortex-A7
core and 128 MB of RAM; the body rebuild needs a GPU). The glasses film and display; this
module uploads, waits and decides what to show.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from cerebel.client import CerebelClient
from cerebel.errors import (
    AnalysisFailed,
    CerebelError,
    ConsentRequired,
    PaymentRequired,
    ServiceUnavailable,
    UploadRejected,
)
from cerebel.glasses.clips import FolderWatcher, prepare_clip
from cerebel.glasses.hud import HudSink, error_card, fix_card, progress_card, received_card

LOG = logging.getLogger("cerebel.glasses")


class GlassesBridge:
    def __init__(
        self,
        client: CerebelClient,
        sink: HudSink,
        *,
        sport: str = "golf",
        handedness: str = "right",
        lang: str = "zh",
        fps: float = 30.0,
        progress_every_s: float = 15.0,
        poll_interval: float = 5.0,
        on_result: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.client = client
        self.sink = sink
        self.sport = sport
        self.handedness = handedness
        self.lang = lang
        self.fps = fps
        self.progress_every_s = progress_every_s
        self.poll_interval = poll_interval
        self.on_result = on_result
        self._last_card_at = 0.0
        self._last_stage = ""

    def _show(self, card: dict[str, Any]) -> None:
        try:
            self.sink.show(card)
        except Exception:  # noqa: BLE001
            # The analysis matters more than one missed card.
            LOG.exception("could not show HUD card")

    def _progress(self, prog: dict[str, Any]) -> None:
        stage = str(prog.get("stage") or "")
        now = time.monotonic()
        # A new stage always shows; within a stage, at most one card per progress_every_s.
        if stage != self._last_stage or now - self._last_card_at >= self.progress_every_s:
            self._last_stage = stage
            self._last_card_at = now
            self._show(progress_card(prog, self.lang))

    def process_clip(self, path: str | Path) -> dict[str, Any]:
        """Analyse one recording and show the result. Returns what CerebelClient.analyze returns.

        Errors are shown on the HUD and then raised, so the caller can log or retry.
        """
        self._last_stage = ""
        self._show(received_card(self.lang))
        try:
            clip = prepare_clip(path, fps=self.fps)
            result = self.client.analyze(
                clip,
                sport=self.sport,
                handedness=self.handedness,
                on_progress=self._progress,
                poll_interval=self.poll_interval,
            )
        except ConsentRequired:
            self._show(error_card("consent", self.lang))
            raise
        except PaymentRequired:
            self._show(error_card("quota", self.lang))
            raise
        except ServiceUnavailable:
            self._show(error_card("offline", self.lang))
            raise
        except UploadRejected as exc:
            self._show(error_card("rejected", self.lang, detail=exc.message))
            raise
        except AnalysisFailed as exc:
            self._show(error_card("no_swing" if exc.stage == "no_swing" else "failed", self.lang, detail=exc.message))
            raise
        except (CerebelError, RuntimeError) as exc:
            self._show(error_card("failed", self.lang, detail=str(exc)))
            raise
        self._show(fix_card(result.get("main_fix"), self.lang))
        if self.on_result is not None:
            self.on_result(result)
        return result

    def watch(
        self,
        folder: str | Path,
        *,
        interval: float = 1.0,
        settle_s: float = 2.0,
        stop: threading.Event | None = None,
    ) -> None:
        """Process every new recording that lands in `folder` (pulled over ADB, Wi-Fi transfer,
        or saved by a phone app) until `stop` is set. One clip at a time, in arrival order.
        A file counts as complete once its size has not changed for `settle_s` seconds."""
        watcher = FolderWatcher(folder, settle_s=settle_s)
        LOG.info("watching %s for new recordings", folder)
        for path in watcher.watch(interval=interval, stop=stop):
            LOG.info("new recording %s", path.name)
            try:
                self.process_clip(path)
            except (CerebelError, RuntimeError) as exc:
                LOG.warning("recording %s not analysed: %s", path.name, exc)
