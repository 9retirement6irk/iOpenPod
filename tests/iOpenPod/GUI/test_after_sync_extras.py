"""After a Sync, notes, calendars and contacts wait for the iPod rather than
being dropped when something else holds it for a moment."""

import logging
from pathlib import Path

import pytest
from tests.iOpenPod.app.services.test_library_resources import build_device
from tests.iOpenPod.GUI.application_shell_test_support import APPLICATION, build_context

from iOpenPod.app.host_media_library import HostMediaCacheStats, HostMediaLibrary
from iOpenPod.app.sync_execution import SyncExecutionResult, SyncExecutionStatus
from iOpenPod.GUI import main_window as main_window_module
from iOpenPod.GUI.main_window import MainWindow
from iPodDB.library import LibrarySnapshot


def test_extras_wait_for_a_busy_ipod_and_are_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    device = build_device(tmp_path)
    context = build_context()
    window = MainWindow(context, auto_discover=False)
    try:
        connected = window._connected  # pyright: ignore[reportPrivateUsage]
        devices = context.device_controller
        devices._active_ipod = device.active  # pyright: ignore[reportPrivateUsage]
        started: list[bool] = []

        def publish() -> bool:
            started.append(devices.device_writes_allowed)
            return True

        monkeypatch.setattr(type(connected), "has_extras", property(lambda _s: True))
        monkeypatch.setattr(connected, "publish_for_sync", publish)
        monkeypatch.setattr(connected, "trim_cache", lambda: False)
        monkeypatch.setattr(main_window_module, "_EXTRAS_WAIT_MS", 10)
        # Something else (saving play counts, scrobbling) holds the iPod.
        assert devices.begin_exclusive_operation()
        window._review_host = HostMediaLibrary(  # pyright: ignore[reportPrivateUsage]
            LibrarySnapshot(), (), (), HostMediaCacheStats()
        )
        with caplog.at_level(logging.INFO, logger=main_window_module.__name__):
            window._sync_finished(  # pyright: ignore[reportPrivateUsage]
                SyncExecutionResult(SyncExecutionStatus.PARTIAL)
            )
            APPLICATION.processEvents()
            assert started == []  # waiting, not dropped

            devices.finish_exclusive_operation()
            for _ in range(200):
                APPLICATION.processEvents()
                if started:
                    break
                APPLICATION.thread().msleep(5)
        assert started == [True]
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "Sync finished: status=partial host_sync=True extras_set_up=True" in text
            for text in messages
        )
        assert any("wait for the iPod to be free" in text for text in messages)
        assert any("started=True" in text for text in messages)
    finally:
        window.close()
        device.coordinator.close()
