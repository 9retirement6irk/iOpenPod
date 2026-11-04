"""The Plex page chooses what each Plex library is used for."""

from pathlib import Path
from unittest.mock import MagicMock

from PySide6.QtWidgets import QCheckBox, QComboBox
from tests.iOpenPod.GUI.application_shell_test_support import APPLICATION

from iOpenPod.app.connected.config import (
    ConnectedConfigStore,
    PlexVideoSection,
    PlexVideoUse,
)
from iOpenPod.app.connected.plex import PlexSection
from iOpenPod.app.connected.secrets import MemorySecretStore
from iOpenPod.app.connected_controller import ConnectedController
from iOpenPod.GUI.pages.connected_pages import PlexPage
from storage import AtomicHostFile


def test_each_library_gets_a_use(tmp_path: Path) -> None:
    controller = ConnectedController(
        MagicMock(),
        ConnectedConfigStore(AtomicHostFile(tmp_path / "connected.json")),
        MagicMock(),
        MagicMock(),
        secrets=MemorySecretStore(),
    )
    page = PlexPage(controller, MagicMock(), MagicMock())
    try:
        page._show_libraries(  # pyright: ignore[reportPrivateUsage]
            (
                PlexSection("1", "Music", "artist"),
                PlexSection("2", "Audiobooks", "artist"),
                PlexSection("3", "Films", "movie"),
                PlexSection("4", "Concerts", "movie"),
                PlexSection("5", "TV", "show"),
            )
        )
        boxes = {
            key: page.findChild(QComboBox, f"plexLibrary-{key}")
            for key in ("1", "2", "3", "4", "5")
        }
        assert all(box is not None for box in boxes.values())

        def choose(key: str, value: str) -> None:
            box = boxes[key]
            assert box is not None
            box.setCurrentIndex(box.findData(value))

        # Music libraries start in use; video libraries start unused.
        assert [boxes[k].currentData() for k in boxes] == [  # type: ignore[union-attr]
            "music",
            "music",
            "",
            "",
            "",
        ]
        choose("2", "")
        choose("3", "movies")
        choose("4", "music_videos")
        choose("5", "tv_shows")
        plex = controller.config.plex
        assert plex.skipped_sections == ("2",)
        assert plex.video_sections == (
            PlexVideoSection("3", PlexVideoUse.MOVIES),
            PlexVideoSection("4", PlexVideoUse.MUSIC_VIDEOS),
            PlexVideoSection("5", PlexVideoUse.TV_SHOWS),
        )
        choose("4", "")
        assert [v.key for v in controller.config.plex.video_sections] == ["3", "5"]
        # Music videos Plex attached to artists and songs are a separate switch.
        extras = page.findChild(QCheckBox, "plexMusicVideos")
        assert extras is not None and not extras.isChecked()
        extras.setChecked(True)
        assert controller.config.plex.music_videos
        # The choices survive a restart.
        reloaded = ConnectedConfigStore(AtomicHostFile(tmp_path / "connected.json"))
        assert (
            reloaded.load().plex.video_sections == controller.config.plex.video_sections
        )
    finally:
        page.deleteLater()
        APPLICATION.processEvents()
        controller.shutdown()
