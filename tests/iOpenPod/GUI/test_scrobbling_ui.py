"""Manual scrobbling entry point, persisted option, and real sign-in UI signals."""

from pathlib import Path

from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QDialog, QLineEdit, QPlainTextEdit, QPushButton, QWidget
from tests.iOpenPod.app.test_library_write_controller import wait_for
from tests.iOpenPod.app.test_scrobbling import MemoryCredentials
from tests.iOpenPod.app.test_scrobbling_clients import TransportStub
from tests.iOpenPod.GUI.application_shell_test_support import APPLICATION, build_context

from iOpenPod.app.scrobbling.clients import LastFmClient, ListenBrainzClient
from iOpenPod.app.scrobbling.controller import ScrobbleController
from iOpenPod.app.scrobbling.models import ScrobbleResult, Service
from iOpenPod.app.scrobbling.queue import ScrobbleQueue
from iOpenPod.app.scrobbling.service import ScrobbleService
from iOpenPod.app.scrobbling.settings import (
    LASTFM_USERNAME,
    LISTENBRAINZ_USERNAME,
    SCROBBLE_DURING_SYNC,
)
from iOpenPod.GUI.main_window import MainWindow
from iOpenPod.GUI.widgets.app_combo_box import AppComboBox
from iOpenPod.GUI.widgets.scrobbling_settings import ScrobblingSettings
from iOpenPod.GUI.widgets.sidebar import Sidebar
from iOpenPod.GUI.widgets.sync_settings import SyncSettings
from storage import AtomicHostFile


def test_manual_report_uses_a_short_status_and_scrollable_details() -> None:
    context = build_context()
    window = MainWindow(context, auto_discover=False)
    controller = window.findChild(ScrobbleController)
    assert controller is not None
    result = ScrobbleResult(
        3,
        42,
        issues=("Last.fm report\n" + "Rejected Track details\n" * 60,),
        adjusted=3,
        notices=(
            "Last.fm submission dates adjusted; original playback dates retained.",
        ),
    )
    try:
        controller._completed(result, "")  # pyright: ignore[reportPrivateUsage]
        status = context.status.current_status
        assert status is not None and status.source == "scrobbling"
        assert status.message == result.summary
        assert status.action is not None and status.action.key == "details"
        context.status.request_action("scrobbling", "details")
        APPLICATION.processEvents()
        dialog = window.findChild(QDialog, "scrobbleReport")
        assert dialog is not None and dialog.isVisible()
        report = dialog.findChild(QPlainTextEdit, "scrobbleReportText")
        assert report is not None and report.isReadOnly()
        assert "Rejected Track details" in report.toPlainText()
        assert "Last.fm submission dates adjusted" in report.toPlainText()
        assert "3 dates adjusted" in status.message
        assert report.verticalScrollBar().maximum() > 0
        assert dialog.height() <= 650
        dialog.close()
    finally:
        window.close()
        context.shutdown()
        APPLICATION.processEvents()


def test_maintenance_button_is_an_action_and_sync_option_persists() -> None:
    context = build_context()
    parent = QWidget()
    try:
        sidebar = Sidebar(parent)
        button = sidebar.findChild(QPushButton, "scrobbleNow")
        assert button is not None and button.text() == "Scrobble now"
        assert not button.isEnabled() and not button.isCheckable()
        requests = QSignalSpy(sidebar.scrobbleRequested)
        sidebar.set_scrobble_available(True)
        button.click()
        assert requests.count() == 1
        sync = SyncSettings(context.settings, parent)
        combo = sync.findChild(AppComboBox, "scrobbleDuringSync")
        assert combo is not None and combo.currentData() is True
        combo.setCurrentIndex(combo.findData(False))
        assert context.settings.get(SCROBBLE_DURING_SYNC) is False
        rebuilt = SyncSettings(context.settings, parent)
        saved = rebuilt.findChild(AppComboBox, "scrobbleDuringSync")
        assert saved is not None and saved.currentData() is False
    finally:
        parent.close()
        context.shutdown()


def test_service_sign_in_credentials_are_masked_validated_and_disconnected(
    tmp_path: Path,
) -> None:
    context = build_context()
    credentials = MemoryCredentials()
    credentials.values.clear()
    service = ScrobbleService(
        context.device_coordinator,
        ScrobbleQueue(AtomicHostFile(tmp_path / "queue.json")),
        credentials,
    )
    lf = LastFmClient(
        TransportStub(
            {"token": "auth-token"},
            {"session": {"name": "alice", "key": "private-session"}},
        )
    )
    lb = ListenBrainzClient(TransportStub({"valid": True, "user_name": "alice"}))
    controller = ScrobbleController(
        service,
        credentials,
        context.settings,
        context.device_controller,
        lastfm=lf,
        listenbrainz=lb,
    )
    widget = ScrobblingSettings(context.settings, controller)
    widget.show()
    try:
        # Avoid opening a real browser; observe the auth URL on the controller.
        controller.authorizationRequested.disconnect()
        authorization = QSignalSpy(controller.authorizationRequested)
        key = widget.findChild(QLineEdit, "lastfmApiKey")
        secret = widget.findChild(QLineEdit, "lastfmApiSecret")
        token = widget.findChild(QLineEdit, "listenbrainzToken")
        assert key is not None and secret is not None and token is not None
        for control in (key, secret, token):
            assert control.echoMode() is QLineEdit.EchoMode.Password
        key.setText("api-key")
        secret.setText("shared-secret")
        button = widget.findChild(QPushButton, "connectLastfm")
        assert button is not None
        button.click()
        wait_for(lambda: not controller.busy)
        assert authorization.count() == 1 and controller.awaiting_authorization
        finish = widget.findChild(QPushButton, "finishLastfm")
        assert finish is not None and finish.isVisible()
        finish.click()
        wait_for(lambda: not controller.busy)
        assert context.settings.get(LASTFM_USERNAME) == "alice"
        assert credentials.values[Service.LASTFM].token == "private-session"
        assert key.text() == secret.text() == ""
        token.setText("private-token")
        connect = widget.findChild(QPushButton, "connectListenbrainz")
        assert connect is not None
        connect.click()
        wait_for(lambda: not controller.busy)
        assert context.settings.get(LISTENBRAINZ_USERNAME) == "alice"
        assert credentials.values[Service.LISTENBRAINZ].token == "private-token"
        assert token.text() == ""
        disconnect = widget.findChild(QPushButton, "disconnectListenbrainz")
        assert disconnect is not None
        disconnect.click()
        wait_for(lambda: not controller.busy)
        assert context.settings.get(LISTENBRAINZ_USERNAME) == ""
        assert Service.LISTENBRAINZ not in credentials.values
        assert context.settings.get(LASTFM_USERNAME) == "alice"
    finally:
        controller.shutdown()
        widget.close()
        context.shutdown()
        APPLICATION.processEvents()


def test_plex_plays_and_stars_switch_follows_the_chosen_server(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QCheckBox

    from iOpenPod.app.scrobbling.models import Account, Credentials
    from iOpenPod.app.scrobbling.settings import (
        PLEX_SYNC,
        PLEX_USERNAME,
        configured_accounts,
    )

    context = build_context()
    credentials = MemoryCredentials()
    credentials.values.clear()
    service = ScrobbleService(
        context.device_coordinator,
        ScrobbleQueue(AtomicHostFile(tmp_path / "queue.json")),
        credentials,
    )
    controller = ScrobbleController(
        service, credentials, context.settings, context.device_controller
    )
    plex = Credentials("will", "server-token", "http://plex:32400", "srv")
    chosen: list[Credentials | None] = [None]
    controller.set_plex_source(lambda: chosen[0])
    widget = ScrobblingSettings(context.settings, controller)
    widget.show()
    try:
        switch = widget.findChild(QCheckBox, "plexScrobbling")
        assert switch is not None and switch.isChecked()  # on by default
        # No server chosen yet: nothing to send to.
        controller.ensure_plex()
        assert configured_accounts(context.settings) == ()

        # A server chosen on the Plex page is picked up for plays.
        chosen[0] = plex
        controller.ensure_plex()
        assert credentials.values[Service.PLEX] == plex
        assert configured_accounts(context.settings) == (Account(Service.PLEX, "will"),)

        # Switching it off in Settings → Sync leaves Plex out of every run.
        switch.click()
        wait_for(lambda: not controller.busy)
        assert context.settings.get(PLEX_SYNC) is False
        assert configured_accounts(context.settings) == ()
        assert context.settings.get(PLEX_USERNAME) == "will"

        # On again: back in, without signing in to Plex again.
        switch.click()
        wait_for(lambda: not controller.busy)
        assert configured_accounts(context.settings) == (Account(Service.PLEX, "will"),)
    finally:
        widget.close()
        controller.shutdown()
        context.shutdown()
