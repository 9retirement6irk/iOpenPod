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

    def _watch(self) -> None:
        reported_beat = 0.0
        while not self._stopped.wait(self._poll):
            beat = self._beat
            stalled = monotonic() - beat
            if stalled < self._threshold:
                if reported_beat and beat != reported_beat:
                    logger.warning(
                        "Interface responsive again after about %.1fs",
                        beat - reported_beat,
                    )
                    reported_beat = 0.0
                continue
            if beat == reported_beat:
                continue  # already reported this stall
            reported_beat = beat
            frame = sys._current_frames().get(self._interface_thread)
            stack = (
                "".join(traceback.format_stack(frame)[-_MAX_FRAMES:])
                if frame is not None
                else "(interface thread stack unavailable)\n"
            )
            logger.warning(
                "Interface not responding for %.1fs; it is busy in:\n%s",
                stalled,
                stack.rstrip(),
            )


__all__ = ["StallMonitor"]
