"""Connected Source Folders gain, keep, and lose only the files iOpenPod owns."""

from pathlib import Path
from threading import Event

import pytest

from iOpenPod.app.connected.http import CancelledError, ConnectedError
from iOpenPod.app.connected.materialize import (
    DesiredBundle,
    DesiredBytes,
    DesiredDownload,
    m3u8_playlist,
    refresh_folder,
    safe_component,
)


def download(
    relative: str, revision: str, data: bytes, log: list[str]
) -> DesiredDownload:
    def fetch(destination: Path) -> None:
        log.append(relative)
        destination.write_bytes(data)

    return DesiredDownload(relative, revision, fetch)


def test_refresh_downloads_keeps_and_removes_only_owned_files(tmp_path: Path) -> None:
    log: list[str] = []
    folder = tmp_path / "Plex"
    first = refresh_folder(
        folder,
        [
            download("A/One.mp3", "r1", b"one", log),
            download("A/Two.mp3", "r1", b"two", log),
            DesiredBytes("Playlists/Mix.m3u8", b"#EXTM3U\n"),
        ],
        cancelled=Event(),
    )
    assert (first.downloaded, first.written, first.removed) == (2, 1, 0)
    (folder / "A" / "Mine.mp3").write_bytes(b"added by the person")

    second = refresh_folder(
        folder,
        [
            download("A/One.mp3", "r1", b"one", log),
            download("B/Three.mp3", "r1", b"3", log),
        ],
        cancelled=Event(),
    )
    assert (second.downloaded, second.kept, second.removed) == (1, 1, 2)
    assert log == ["A/One.mp3", "A/Two.mp3", "B/Three.mp3"]
    assert not (folder / "A" / "Two.mp3").exists()
    assert not (folder / "Playlists").exists()  # emptied folder pruned
    assert (folder / "A" / "Mine.mp3").read_bytes() == b"added by the person"


def test_revision_change_refetches_and_foreign_names_are_never_overwritten(
    tmp_path: Path,
) -> None:
    log: list[str] = []
    folder = tmp_path / "Immich"
    folder.mkdir()
    (folder / "Taken.jpg").write_bytes(b"person's own")
    report = refresh_folder(
        folder,
        [
            download("Taken.jpg", "r1", b"server", log),
            download("Photo.jpg", "r1", b"v1", log),
        ],
        cancelled=Event(),
    )
    assert (folder / "Taken.jpg").read_bytes() == b"person's own"
    assert report.failed and "already uses this name" in report.failed[0]
    refresh_folder(folder, [download("Photo.jpg", "r2", b"v2", log)], cancelled=Event())
    assert (folder / "Photo.jpg").read_bytes() == b"v2"


def test_changed_owned_file_is_kept_on_removal(tmp_path: Path) -> None:
    folder = tmp_path / "Plex"
    refresh_folder(folder, [download("x.mp3", "r", b"abc", [])], cancelled=Event())
    (folder / "x.mp3").write_bytes(b"edited by the person")
    report = refresh_folder(folder, [], cancelled=Event())
    assert (folder / "x.mp3").exists()
    assert "changed outside iOpenPod" in report.failed[0]


def test_bundles_own_every_unpacked_file(tmp_path: Path) -> None:
    folder = tmp_path / "Audiobooks"

    def fetch(staging: Path) -> None:
        (staging / "01.mp3").write_bytes(b"1")
        (staging / "02.mp3").write_bytes(b"2")

    bundle = DesiredBundle("Author - Book", "grimmory:1", fetch)
    assert refresh_folder(folder, [bundle], cancelled=Event()).downloaded == 1
    assert refresh_folder(folder, [bundle], cancelled=Event()).kept == 1
    report = refresh_folder(folder, [], cancelled=Event())
    assert report.removed == 2
    assert not (folder / "Author - Book").exists()


def test_unsafe_names_and_duplicates_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ConnectedError):
        refresh_folder(
            tmp_path, [DesiredBytes("../escape.txt", b"")], cancelled=Event()
        )
    with pytest.raises(ConnectedError):
        refresh_folder(
            tmp_path,
            [DesiredBytes("a.txt", b""), DesiredBytes("A.txt", b"")],
            cancelled=Event(),
        )
    assert safe_component('a/b?*"') == "a_b___"
    assert safe_component("CON") == "_CON"
    assert safe_component("con.txt") == "_con.txt"
    assert safe_component("...") == "Untitled"


def test_cancelled_refresh_keeps_progress(tmp_path: Path) -> None:
    cancelled = Event()
    cancelled.set()
    with pytest.raises(CancelledError):
        refresh_folder(
            tmp_path / "x", [DesiredBytes("a.txt", b"a")], cancelled=cancelled
        )


def test_playlist_text() -> None:
    text = m3u8_playlist("Road Trip", [("../A/B/01 x.mp3", 200, "x  y")]).decode()
    assert text.splitlines() == [
        "#EXTM3U",
        "#PLAYLIST:Road Trip",
        "#EXTINF:200,x y",
        "../A/B/01 x.mp3",
    ]
