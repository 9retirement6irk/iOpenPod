"""Playlists are ticked in Select Media: ticking one syncs it and its songs."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QTreeView
from tests.iOpenPod.app.services.test_library_resources import build_device
from tests.iOpenPod.GUI.application_shell_test_support import APPLICATION, build_context

from iOpenPod.app.host_media_library import HostMediaCacheStats, HostMediaLibrary
from iOpenPod.app.library_sync_helper import IPodMediaCacheStats, IPodMediaLibrary
from iOpenPod.app.models.sync_selection import SyncSelection
from iOpenPod.app.playlist_sync import (
    PlaylistAssociation,
    PlaylistSyncChoice,
    PlaylistSyncReviewItem,
)
from iOpenPod.app.sync_plan import (
    SyncPlan,
    SyncPlanAction,
    SyncPlanBasis,
    SyncPlanItem,
    SyncPlanMediaKind,
    host_path_identity,
)
from iOpenPod.GUI.presentation.artwork_provider import ArtworkPixmapProvider
from iOpenPod.GUI.sync_workspace import SyncStage, SyncWorkspace
from iOpenPod.GUI.widgets.playlist_tree import (
    _check_rect,  # pyright: ignore[reportPrivateUsage]
)
from iPodDB.library import (
    LibrarySnapshot,
    Playlist,
    PlaylistKind,
    Track,
    TrackMetadata,
    playlist_entries,
)

CHECKED = Qt.CheckState.Checked
UNCHECKED = Qt.CheckState.Unchecked


def _all_state(selection: SyncSelection) -> Qt.CheckState | None:
    return selection.playlists_check_state


def _stage(workspace: SyncWorkspace) -> SyncStage:
    return workspace.stage


def _library() -> HostMediaLibrary:
    tracks = tuple(
        Track(
            identity,
            f"Song {identity}",
            "Artist",
            "Album",
            1000,
            metadata=TrackMetadata(location=f"C:/Music/{identity}.mp3"),
        )
        for identity in range(1, 6)
    )
    return HostMediaLibrary(
        LibrarySnapshot(
            tracks=tracks,
            playlists=(
                Playlist(10, "Road Trip", entries=playlist_entries((1, 2, 3))),
                Playlist(11, "Chill", entries=playlist_entries((3, 4))),
                Playlist(12, "Smart", PlaylistKind.SMART),
            ),
        ),
        (),
        (),
        HostMediaCacheStats(),
    )


def _comparison() -> SyncPlan:
    """Song 1 is already on the iPod; the rest are new."""

    return SyncPlan(
        tuple(
            SyncPlanItem(
                SyncPlanAction.UNCHANGED if identity == 1 else SyncPlanAction.ADD,
                SyncPlanMediaKind.TRACK,
                SyncPlanBasis.CONTENT_MATCH
                if identity == 1
                else SyncPlanBasis.HOST_ONLY,
                f"Song {identity}",
                "",
                f"C:/Music/{identity}.mp3",
                None,
                100 + identity if identity == 1 else None,
            )
            for identity in range(1, 6)
        )
    )


def test_ticking_a_playlist_ticks_its_songs_and_unticking_never_removes() -> None:
    selection = SyncSelection()
    selection.reset(_library(), _comparison())
    assert selection.playlist_ids == {10, 11}
    assert selection.playlist_check_state(12) is None  # Smart Playlists can't sync
    assert selection.playlist_check_state(10) is UNCHECKED
    assert selection.track_check_state(1) is CHECKED  # on the iPod already
    assert selection.track_check_state(2) is UNCHECKED

    assert selection.set_playlist_checked(10, True)
    assert selection.playlist_check_state(10) is CHECKED
    assert [selection.track_check_state(i) for i in (1, 2, 3, 4)] == [
        CHECKED,
        CHECKED,
        CHECKED,
        UNCHECKED,
    ]
    assert selection.set_playlist_checked(11, True)
    assert selection.track_check_state(4) is CHECKED

    # Unticking keeps songs on the iPod and songs another ticked Playlist holds.
    assert selection.set_playlist_checked(10, False)
    assert selection.chosen_playlist_ids == {11}
    assert [selection.track_check_state(i) for i in (1, 2, 3, 4)] == [
        CHECKED,
        UNCHECKED,
        CHECKED,
        CHECKED,
    ]
    assert not selection.set_playlist_checked(10, False)  # already unticked
    assert not selection.set_playlist_checked(12, True)  # not a Playlist to sync

    # Defaults (Playlists synced before) don't change songs, and survive a rescan.
    selection.choose_playlists((10, 99))
    assert selection.chosen_playlist_ids == {10, 11}
    assert selection.track_check_state(2) is UNCHECKED
    selection.reset(_library(), _comparison())
    assert selection.chosen_playlist_ids == {10, 11}
    selection.clear()
    assert not selection.chosen_playlist_ids


def test_all_playlists_are_chosen_or_unchosen_in_one_update() -> None:
    selection = SyncSelection()
    assert _all_state(selection) is None  # nothing to choose yet
    selection.reset(_library(), _comparison())
    assert _all_state(selection) is UNCHECKED
    selection.set_playlist_checked(11, True)
    assert _all_state(selection) is Qt.CheckState.PartiallyChecked

    host_changes = QSignalSpy(selection.hostSelectionChanged)
    playlist_changes = QSignalSpy(selection.playlistsChanged)
    assert selection.set_all_playlists_checked(True)
    # One update however many Playlists there are, so lists refresh once.
    assert host_changes.count() == playlist_changes.count() == 1
    assert _all_state(selection) is CHECKED
    assert selection.chosen_playlist_ids == {10, 11}
    assert [selection.track_check_state(i) for i in (1, 2, 3, 4, 5)] == [
        CHECKED,
        CHECKED,
        CHECKED,
        CHECKED,
        UNCHECKED,  # in no Playlist
    ]
    assert not selection.set_all_playlists_checked(True)

    assert selection.set_all_playlists_checked(False)
    assert _all_state(selection) is UNCHECKED
    # Song 1 is on the iPod: unticking every Playlist never plans a removal.
    assert [selection.track_check_state(i) for i in (1, 2, 3, 4)] == [
        CHECKED,
        UNCHECKED,
        UNCHECKED,
        UNCHECKED,
    ]


@pytest.fixture
def workspace() -> Iterator[SyncWorkspace]:
    context = build_context()
    artwork = ArtworkPixmapProvider(context.artwork_controller)
    page = SyncWorkspace(context.settings, context.theme_manager, artwork)
    page.resize(1050, 800)
    page.show()
    try:
        yield page
    finally:
        page.shutdown()
        artwork.shutdown()
        page.close()
        page.deleteLater()
        APPLICATION.sendPostedEvents(page, QEvent.Type.DeferredDelete)
        context.shutdown()


def test_select_media_chooses_playlists_and_review_follows(
    tmp_path: Path, workspace: SyncWorkspace
) -> None:
    device = build_device(tmp_path)
    try:
        ipod = replace(
            IPodMediaLibrary((), (), (), IPodMediaCacheStats(), None, False),
            playlist_associations=(PlaylistAssociation(11, 500),),
        )
        workspace.load_comparison(_library(), _comparison(), device.active, ipod)
        workspace.show_selection()
        selection = workspace.selection
        # A Playlist synced before stays ticked; the others start unticked.
        assert selection.chosen_playlist_ids == {11}
        assert set(workspace.playlist_choices) == {
            PlaylistSyncChoice(10, included=False)
        }

        tree = workspace.findChild(QTreeView, "playlistTree")
        assert tree is not None
        model = tree.model()
        row = next(
            index
            for index in (model.index(r, 0) for r in range(model.rowCount()))
            if index.data() == "Road Trip"
        )
        tree.scrollTo(row)
        APPLICATION.processEvents()
        rect = tree.visualRect(row)
        assert rect.isValid()
        # Clicking the checkbox ticks the Playlist and its songs, without opening it.
        box = _check_rect(row, rect).center()
        QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=box)
        assert selection.chosen_playlist_ids == {10, 11}
        assert selection.track_check_state(2) is CHECKED
        assert tree.currentIndex() != row
        assert workspace.playlist_choices == ()

        # Space on the current Playlist unticks it again.
        tree.setCurrentIndex(row)
        QTest.keyClick(tree, Qt.Key.Key_Space)
        assert selection.chosen_playlist_ids == {11}

        # Review only shows the ticked Playlists.
        rows = tuple(
            PlaylistSyncReviewItem(identity, name, PlaylistSyncChoice(identity))
            for identity, name in ((10, "Road Trip"), (11, "Chill"))
        )
        workspace.set_playlist_preview(rows)
        assert [r.host_playlist_id for r in workspace._playlist_preview] == [11]  # pyright: ignore[reportPrivateUsage]
        assert host_path_identity("C:/Music/2.mp3") not in selection.selected_host_paths
    finally:
        device.coordinator.close()


def test_select_all_playlists_checkbox_and_the_playlists_stage(
    tmp_path: Path, workspace: SyncWorkspace
) -> None:
    device = build_device(tmp_path)
    try:
        ipod = IPodMediaLibrary((), (), (), IPodMediaCacheStats(), None, False)
        workspace.load_comparison(_library(), _comparison(), device.active, ipod)
        selection = workspace.selection
        everything = workspace.findChild(QCheckBox, "selectAllSyncPlaylists")
        proceed = workspace.findChild(QPushButton, "reviewSyncSelection")
        assert everything is not None and proceed is not None
        assert everything.isVisible() and everything.text() == "All Playlists"
        assert everything.checkState() is UNCHECKED
        assert proceed.text() == "Review Sync"

        # No Playlists chosen: Select Media goes straight to Review.
        proceed.click()
        assert _stage(workspace) is SyncStage.REVIEW
        to_playlists = workspace.findChild(QPushButton, "backToSyncPlaylists")
        assert to_playlists is not None and not to_playlists.isVisible()
        workspace.show_selection()

        selection.set_playlist_checked(11, True)
        assert everything.checkState() is Qt.CheckState.PartiallyChecked
        everything.click()  # some chosen -> all chosen
        assert selection.chosen_playlist_ids == {10, 11}
        assert everything.checkState() is CHECKED
        assert selection.track_check_state(2) is CHECKED
        everything.click()  # all chosen -> none
        assert not selection.chosen_playlist_ids
        assert everything.checkState() is UNCHECKED
        everything.click()
        assert selection.chosen_playlist_ids == {10, 11}

        # With Playlists chosen, they get their own stage before Review.
        assert proceed.text() == "Review Playlists"
        proceed.click()
        assert _stage(workspace) is SyncStage.PLAYLISTS
        title = workspace.findChild(QLabel, "syncPlaylistReviewTitle")
        execute = workspace.findChild(QPushButton, "executeSync")
        assert title is not None and title.isVisible()
        assert title.text() == "Review Playlists"
        assert execute is not None and not execute.isVisible()
        current = [
            label.text()
            for label in workspace.findChildren(QLabel, "syncStageIndicator")
            if label.property("current")
        ]
        assert current == ["3  Playlists"]

        onward = workspace.findChild(QPushButton, "reviewSyncAfterPlaylists")
        assert onward is not None and onward.text() == "Review Sync"
        onward.click()
        assert _stage(workspace) is SyncStage.REVIEW
        assert to_playlists.isVisible()
        to_playlists.click()
        assert _stage(workspace) is SyncStage.PLAYLISTS
        back = workspace.findChild(QPushButton, "backToSyncSelectionFromPlaylists")
        assert back is not None
        back.click()
        assert _stage(workspace) is SyncStage.SELECT
    finally:
        device.coordinator.close()
