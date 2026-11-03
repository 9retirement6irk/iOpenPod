"""iopenpod:// links reach the one running iOpenPod."""

import shlex
import uuid
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtGui import QFileOpenEvent
from tests.iOpenPod.app.test_library_write_controller import wait_for
from tests.iOpenPod.GUI.application_shell_test_support import APPLICATION

from iOpenPod.app import app_links
from iOpenPod.app.app_links import (
    AppLinks,
    desktop_entry,
    forward,
    is_app_link,
    link_from,
)


def test_links_are_recognised() -> None:
    assert is_app_link("iopenpod://oauth2-callback?code=1&state=2")
    assert is_app_link("IOPENPOD://oauth2-callback")
    assert not is_app_link("https://example.com")
    assert not is_app_link("iopenpod://" + "x" * 20_000)
    assert link_from(["--flag", "iopenpod://oauth2-callback?code=1"]) == (
        "iopenpod://oauth2-callback?code=1"
    )
    assert link_from(["file.mp3"]) is None


def test_a_second_launch_hands_its_link_to_the_running_app() -> None:
    name = f"iOpenPod-test-{uuid.uuid4().hex[:12]}"
    link = "iopenpod://oauth2-callback?code=c&state=s"
    assert not forward(link, name, timeout_ms=200)  # nothing running yet

    links = AppLinks(name=name)
    received: list[str] = []
    links.received.connect(received.append)
    assert links.listen()
    try:
        assert forward(link, name)
        assert forward("https://not-ours.example", name)  # delivered but ignored
        wait_for(lambda: bool(received))
        APPLICATION.processEvents()
        assert received == [link]
    finally:
        links.close()
        links.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_macos_file_open_events_deliver_links() -> None:
    links = AppLinks(name=f"iOpenPod-test-{uuid.uuid4().hex[:12]}")
    received: list[str] = []
    links.received.connect(received.append)
    url = "iopenpod://oauth2-callback?state=s&code=c"
    handled = links.eventFilter(links, QFileOpenEvent(QUrl(url)))
    assert handled and received == [url]
    assert not links.eventFilter(links, QFileOpenEvent("/tmp/song.mp3"))


def test_linux_registration_writes_a_hidden_desktop_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))
    command = ["/opt/My Apps/iOpenPod", "--x"]
    monkeypatch.setattr(app_links, "launch_command", lambda: command)
    app_links.register_scheme()
    entry = (
        tmp_path / "applications/io.github.therealsavi.iOpenPod-links.desktop"
    ).read_text()
    assert entry == desktop_entry(command)
    assert "MimeType=x-scheme-handler/iopenpod;" in entry and "NoDisplay=true" in entry
    executable = next(line for line in entry.splitlines() if line.startswith("Exec="))
    assert shlex.split(executable.removeprefix("Exec=")) == [*command, "%u"]
    app_links.register_scheme()  # unchanged entries are left alone


def test_windows_quoting_keeps_spaces_together() -> None:
    assert app_links._quote_windows(r"C:\Program Files\iOpenPod\iOpenPod.exe") == (
        r'"C:\Program Files\iOpenPod\iOpenPod.exe"'
    )


def test_the_launcher_accepts_only_iopenpod_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import iopenpod_launcher

    forwarded: list[str] = []

    def running(link: str) -> bool:
        forwarded.append(link)
        return True

    monkeypatch.setattr(app_links, "forward", running)
    assert iopenpod_launcher.main(["iopenpod://oauth2-callback?state=s&code=c"]) == 0
    assert forwarded == ["iopenpod://oauth2-callback?state=s&code=c"]
    with pytest.raises(SystemExit):
        iopenpod_launcher.main(["https://example.com"])
