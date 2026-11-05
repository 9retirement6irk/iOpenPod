"""Playlists are ticked in Select Media: ticking one syncs it and its songs."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QTreeView
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
from iOpenPod.GUI.sync_workspace import SyncWorkspace
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
