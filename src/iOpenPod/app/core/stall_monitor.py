"""Log what the interface thread was doing whenever the window stops responding.

A beach ball or "Not Responding" window means the interface thread is busy and
cannot handle input. That is invisible in the log unless something records it,
so a watcher thread notes when the interface thread stops answering and writes
its Python stack once per stall.
"""

from __future__ import annotations

import logging
import sys
import threading
import traceback
from time import monotonic

from PySide6.QtCore import QObject, QTimer

logger = logging.getLogger(__name__)

_BEAT_MS = 100
_STALL_SECONDS = 0.75
_POLL_SECONDS = 0.25
_MAX_FRAMES = 30
_RESAMPLE_SECONDS = 1.0
_MAX_SAMPLES = 12


class StallMonitor(QObject):
    """Report interface stalls longer than ``threshold`` seconds to the log."""

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        threshold: float = _STALL_SECONDS,
        poll: float = _POLL_SECONDS,
    ) -> None:
        super().__init__(parent)
        self._threshold = threshold
        self._poll = poll
        self._interface_thread = threading.get_ident()
        self._beat = monotonic()
        self._stopped = threading.Event()
        self._timer = QTimer(self)
        self._timer.setInterval(_BEAT_MS)
        self._timer.timeout.connect(self._heartbeat)
        self._watcher: threading.Thread | None = None

    def start(self) -> None:
        if self._watcher is not None:
            return
        self._beat = monotonic()
        self._timer.start()
        self._watcher = threading.Thread(
            target=self._watch, name="interface-stall-monitor", daemon=True
        )
        self._watcher.start()

    def stop(self) -> None:
        self._stopped.set()
        self._timer.stop()
        if self._watcher is not None:
            self._watcher.join(timeout=2)
            self._watcher = None

    def _heartbeat(self) -> None:
        self._beat = monotonic()

    def _stack(self) -> str:
        frame = sys._current_frames().get(self._interface_thread)
        if frame is None:
            return "(interface thread stack unavailable)"
        return "".join(traceback.format_stack(frame)[-_MAX_FRAMES:]).rstrip()

    def _watch(self) -> None:
        reported_beat = 0.0
        samples = 0
        next_sample = 0.0
        last_stack = ""
        while not self._stopped.wait(self._poll):
            beat = self._beat
            now = monotonic()
            stalled = now - beat
            if stalled < self._threshold:
                if reported_beat and beat != reported_beat:
                    logger.warning(
                        "Interface responsive again after about %.1fs",
                        beat - reported_beat,
                    )
                    reported_beat = 0.0
                continue
            if beat != reported_beat:
                reported_beat = beat
                samples = 1
                next_sample = now + _RESAMPLE_SECONDS
                last_stack = self._stack()
                logger.warning(
                    "Interface not responding for %.1fs; it is busy in:\n%s",
                    stalled,
                    last_stack,
                )
            elif samples < _MAX_SAMPLES and now >= next_sample:
                # A long stall moves through several pieces of work: sample it
                # again so the log shows where the time goes, not only the start.
                samples += 1
                next_sample = now + _RESAMPLE_SECONDS
                stack = self._stack()
                if stack != last_stack:
                    last_stack = stack
                    logger.warning(
                        "Interface still not responding after %.1fs; now in:\n%s",
                        stalled,
                        stack,
                    )


__all__ = ["StallMonitor"]
