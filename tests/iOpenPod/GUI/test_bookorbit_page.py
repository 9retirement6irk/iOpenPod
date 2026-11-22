"""The BookOrbit page connects with an API key and compares listening progress."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton
from tests.iOpenPod.app.connected.test_bookorbit import (
    BASE,
    KEY,
    OrbitServer,
    orbit_controller,
    orbit_track,
)
from tests.iOpenPod.app.test_library_write_controller import wait_for
from tests.iOpenPod.GUI.application_shell_test_support import APPLICATION

from iOpenPod.GUI.pages.connected_pages import BookOrbitPage

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_connecting_and_checking_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OrbitServer()
    controller = orbit_controller(tmp_path, monkeypatch, server)
    workspace = MagicMock()
    workspace.tracks = (
        orbit_track(1, 0, played=True, last_played=100),
        orbit_track(2, 1, bookmark=20_000, last_played=200),
    )
    page = BookOrbitPage(controller, workspace)
    try:
        address = page.findChild(QLineEdit, "bookorbitAddress")
        key = page.findChild(QLineEdit, "bookorbitKey")
        connect = page.findChild(QPushButton, "bookorbitConnect")
        progress = page.findChild(QPushButton, "bookorbitSyncProgress")
        assert address is not None and key is not None
        assert connect is not None and progress is not None
        assert key.echoMode() is QLineEdit.EchoMode.Password
        assert not progress.isEnabled()  # nothing to compare until connected

        address.setText(BASE)
        key.setText(KEY)
        connect.click()
        assert key.text() == ""  # never left sitting in the window
        wait_for(lambda: not controller.busy)
        APPLICATION.processEvents()
        labels = [label.text() for label in page.findChildren(QLabel)]
        assert "Connected as will" in labels
        assert any("2 audiobooks appear" in text for text in labels)
        assert connect.text() == "Reconnect"
        assert progress.isEnabled()

        # Comparing finds the iPod ahead and, once confirmed, sends it.
        confirmed: list[str] = []

        def accept(box: object) -> int:
            from PySide6.QtWidgets import QMessageBox

            assert isinstance(box, QMessageBox)
            confirmed.append(box.informativeText())
            return QMessageBox.StandardButton.Ok

        monkeypatch.setattr(
            "iOpenPod.GUI.pages.connected_pages.QMessageBox.exec", accept
        )
        progress.click()
        wait_for(lambda: not controller.busy)
        APPLICATION.processEvents()
        wait_for(lambda: not controller.busy)
        APPLICATION.processEvents()
        assert confirmed == ["Two Parts: iPod 0:01:20 → BookOrbit"]
        assert server.states[5]["positionMs"] == 20_000
        assert any(
            "Sent 1 positions to BookOrbit" in label.text()
            for label in page.findChildren(QLabel)
        )
    finally:
        page.close()
        controller.shutdown()
        APPLICATION.processEvents()
