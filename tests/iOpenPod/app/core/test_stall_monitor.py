"""A frozen window leaves a record of what the interface thread was doing."""

import logging
import time

import pytest
from tests.iOpenPod.GUI.application_shell_test_support import APPLICATION

from iOpenPod.app.core import stall_monitor
from iOpenPod.app.core.stall_monitor import StallMonitor


def _busy_for(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.01)


def test_stall_is_logged_once_with_the_busy_stack(
    caplog: pytest.LogCaptureFixture,
) -> None:
    monitor = StallMonitor(threshold=0.2, poll=0.05)
    monitor.start()
    try:
        with caplog.at_level(logging.WARNING, logger=stall_monitor.__name__):
            # Responsive: the heartbeat runs, nothing is reported.
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline:
                APPLICATION.processEvents()
                time.sleep(0.01)
            assert not caplog.records

            _busy_for(0.6)  # the interface thread handles no events
            stalls = [
                record.getMessage()
                for record in caplog.records
                if "not responding" in record.getMessage()
            ]
            assert len(stalls) == 1  # one report per stall, not one per poll
            assert "_busy_for" in stalls[0]  # it names where the time went

            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline:
                APPLICATION.processEvents()
                time.sleep(0.01)
            assert any(
                "responsive again" in record.getMessage() for record in caplog.records
            )
    finally:
        monitor.stop()
