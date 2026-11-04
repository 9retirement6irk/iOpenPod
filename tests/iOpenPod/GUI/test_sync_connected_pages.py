"""Sync with Host's CONNECTED checklists save what goes on the iPod."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QLabel, QPushButton, QTreeWidgetItem
from tests.iOpenPod.app.connected.test_sync_extras import CALENDAR, CARDS, memo
from tests.iOpenPod.GUI.application_shell_test_support import APPLICATION, build_context
from tests.iOpenPod.GUI.test_sync_media_selection import (
    _comparison,
    _library,
)

from iOpenPod.app.connected.config import (
    ConnectedConfigStore,
    ImmichPick,
    ImmichPickKind,
)
from iOpenPod.app.connected.immich import ImmichCollection
from iOpenPod.app.connected.secrets import MemorySecretStore, SecretKey
from iOpenPod.app.connected_controller import ConnectedController
from iOpenPod.app.models.sync_selection import SyncSelection
from iOpenPod.app.sync_plan import host_path_identity
from iOpenPod.GUI.navigation import PageId
from iOpenPod.GUI.presentation.artwork_provider import ArtworkPixmapProvider
from iOpenPod.GUI.sync_connected_pages import (
    CalendarsSyncPage,
    ContactsSyncPage,
    ImmichSyncPage,
    NotesSyncPage,
)
from iOpenPod.GUI.sync_workspace import SyncWorkspace
from storage import AtomicHostFile

CHECKED = Qt.CheckState.Checked
UNCHECKED = Qt.CheckState.Unchecked


@pytest.fixture
def controller(tmp_path: Path) -> Iterator[ConnectedController]:
    made = ConnectedController(
        MagicMock(),
        ConnectedConfigStore(AtomicHostFile(tmp_path / "connected.json")),
        MagicMock(),
        MagicMock(),
        secrets=MemorySecretStore(),
    )
    try:
        yield made
    finally:
        made.shutdown()


def tops(
    page: ContactsSyncPage | NotesSyncPage | ImmichSyncPage | CalendarsSyncPage,
) -> list[QTreeWidgetItem]:
    items = (page.tree.topLevelItem(i) for i in range(page.tree.topLevelItemCount()))
    return [item for item in items if item is not None]


def children(item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
    found = (item.child(i) for i in range(item.childCount()))
    return [child for child in found if child is not None]


def test_contacts_tick_by_file_or_one_by_one(
    controller: ConnectedController, tmp_path: Path
) -> None:
    upload = tmp_path / "People.vcf"
    upload.write_text(CARDS)
    controller.add_contacts_file(upload)
    page = ContactsSyncPage(controller)
    page.reload()
    (people,) = tops(page)
    assert people.text(0) == "People" and people.checkState(0) is CHECKED
    ada, grace = children(people)
    assert (ada.text(0), grace.text(0)) == ("Ada", "Grace")

    grace.setCheckState(0, UNCHECKED)
    assert set(controller.config.contacts.skipped) == {"g"}
    assert people.checkState(0) == Qt.CheckState.PartiallyChecked
    people.setCheckState(0, UNCHECKED)
    assert set(controller.config.contacts.skipped) == {"a", "g"}
    assert ada.checkState(0) == UNCHECKED
    people.setCheckState(0, CHECKED)
    assert not controller.config.contacts.skipped
    page.deleteLater()


def test_calendars_are_ticked_one_by_one(
    controller: ConnectedController, tmp_path: Path
) -> None:
    path = tmp_path / "Work.ics"
    path.write_text(CALENDAR.format(name="Work"))
    controller.add_calendar_file(path)
    page = CalendarsSyncPage(controller)
    page.reload()
    (work,) = tops(page)
    assert work.text(0) == "Work" and work.checkState(0) is CHECKED
    work.setCheckState(0, UNCHECKED)
    assert controller.config.calendars.skipped == ("file:Work.ics",)
    page.deleteLater()


def test_notes_follow_tags_and_single_memos(controller: ConnectedController) -> None:
    controller.update_config(
        replace(
            controller.config, memos=replace(controller.config.memos, tags=("work",))
        )
    )
    page = NotesSyncPage(controller)
    page.loaded(
        (
            memo("memos/1", "Plan", "work", "home"),
            memo("memos/2", "Shopping", "home"),
        )
    )
    home, work = tops(page)
    assert work.checkState(0) is CHECKED and home.checkState(0) is UNCHECKED
    assert all(child.isDisabled() for child in children(home))
    (plan,) = children(work)
    plan.setCheckState(0, UNCHECKED)
    assert controller.config.memos.skipped_memos == ("memos/1",)
    # The same memo under another tag follows along.
    assert children(home)[0].checkState(0) is UNCHECKED
    home.setCheckState(0, CHECKED)
    assert controller.config.memos.tags == ("work", "home")
    assert not any(child.isDisabled() for child in children(home))
    page.deleteLater()


def test_immich_groups_are_picked_and_update_is_offered(
    controller: ConnectedController,
) -> None:
    single = ImmichPick(ImmichPickKind.ASSET, "p1", "one.jpg")
    controller.set_immich_picks((single,))
    controller.save_secret(SecretKey.IMMICH, {"api_key": "key"})
    page = ImmichSyncPage(controller)
    page.loaded(
        (
            (ImmichCollection(ImmichPickKind.ALBUM, "a1", "Trip", 12),),
            (ImmichCollection(ImmichPickKind.PERSON, "u1", "Sam"),),
        )
    )
    favorites, albums, people = tops(page)
    assert favorites.checkState(0) is UNCHECKED
    (trip,) = children(albums)
    trip.setCheckState(0, CHECKED)
    children(people)[0].setCheckState(0, CHECKED)
    assert [(p.kind, p.id) for p in controller.config.immich.picks] == [
        (ImmichPickKind.ASSET, "p1"),
        (ImmichPickKind.ALBUM, "a1"),
        (ImmichPickKind.PERSON, "u1"),
    ]
    assert page.changed
    trip.setCheckState(0, UNCHECKED)
    assert (ImmichPickKind.ALBUM, "a1") not in {
        (p.kind, p.id) for p in controller.config.immich.picks
    }
    requested = QSignalSpy(page.updateRequested)
    button = page.findChild(QPushButton, "updateImmichPhotos")
    assert button is not None
    button.click()
    assert requested.count() == 1
    page.reset()
    assert not page.changed and not page.tree.topLevelItemCount()
    page.deleteLater()


def test_connected_pages_join_the_media_list(
    controller: ConnectedController, tmp_path: Path
) -> None:
    from tests.iOpenPod.app.services.test_library_resources import build_device

    from iOpenPod.app.library_sync_helper import IPodMediaCacheStats, IPodMediaLibrary

    context = build_context()
    artwork = ArtworkPixmapProvider(context.artwork_controller)
    workspace = SyncWorkspace(context.settings, context.theme_manager, artwork)
    device = build_device(tmp_path)
    contacts = ContactsSyncPage(controller, workspace)
    workspace.add_connected_page(PageId.CONTACTS, "user", contacts)
    try:
        workspace.load_comparison(
            _library(),
            _comparison(),
            device.active,
            IPodMediaLibrary((), (), (), IPodMediaCacheStats(), None, False),
        )
        workspace.show()
        buttons = {
            str(button.property("syncPageId")): button
            for button in workspace.findChildren(QPushButton)
            if button.property("syncPageId")
        }
        buttons["contacts"].click()
        assert contacts.isVisible() and buttons["contacts"].isChecked()
        buttons["albums"].click()
        assert not contacts.isVisible()

        workspace.set_extras_pending(True)
        summary = workspace.findChild(QLabel, "syncReviewSummary")
        assert summary is not None
        assert "Notes, calendars and contacts" in summary.text()
    finally:
        workspace.shutdown()
        artwork.shutdown()
        workspace.close()
        workspace.deleteLater()
        APPLICATION.sendPostedEvents(workspace, QEvent.Type.DeferredDelete)
        context.shutdown()
        device.coordinator.close()


def test_ticks_survive_a_rescan() -> None:
    selection = SyncSelection()
    selection.reset(_library(), _comparison())
    song = host_path_identity("C:/Music/song.mp3")
    movie = host_path_identity("C:/Movies/movie.mp4")
    photo = host_path_identity("C:/Photos/photo.jpg")
    assert selection.selected_host_paths == {song, photo}
    selection.set_tracks_checked((1,), False)  # untick something on the iPod
    selection.set_tracks_checked((2,), True)  # tick something new
    kept = (selection.host_paths, selection.selected_host_paths)

    selection.reset(_library(), _comparison())  # the rescan's defaults
    assert selection.restore_host_selection(*kept)
    assert selection.selected_host_paths == {movie, photo}
    assert selection.check_host_paths(lambda path: path == song)
    assert selection.selected_host_paths == {song, movie, photo}
