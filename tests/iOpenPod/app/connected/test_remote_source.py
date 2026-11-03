"""Plex and Grimmory listed for Sync, downloaded on demand, kept in a media cache."""

import os
from collections.abc import Iterable
from pathlib import Path
from threading import Event

import pytest
from tests.iOpenPod.app.connected.support import FakeTransport, reply
from tests.iOpenPod.app.connected.test_grimmory import BASE, MP3, signed_in
from tests.iOpenPod.app.connected.test_plex import plex

from iOpenPod.app.connected.config import PathMapping, PlexPickKind
from iOpenPod.app.connected.materialize import Owned, load_owned, save_owned
from iOpenPod.app.connected.plex import PlexItem, PlexServer, PlexTrack
from iOpenPod.app.connected.remote_source import ConnectedRemote
from iOpenPod.app.host_media_library import HostMediaFileKind, HostMediaSource
from iOpenPod.app.remote_media import RemoteItem, revision_time_ns
from iPodDB.library import MediaType

SERVER = PlexServer("srv", "Home", "http://plex:32400", "tok")
JPEG = b"\xff\xd8\xff\xe0cover"


class StubPlex:
    def __init__(self, tracks: tuple[PlexTrack, ...]) -> None:
        self.tracks = tracks
        self.fetched: list[str] = []
        self.offline = False

    def library_tracks(
        self, _server: PlexServer, _cancelled: Event
    ) -> tuple[PlexTrack, ...]:
        if self.offline:
            from iOpenPod.app.connected.http import ConnectedError

            raise ConnectedError("Could not reach plex")
        return self.tracks

    def playlists(self, _server: PlexServer) -> tuple[PlexItem, ...]:
        return (PlexItem(PlexPickKind.PLAYLIST, "p1", "Road Trip"),)

    def playlist_tracks(
        self, _server: PlexServer, _key: str, _cancelled: Event
    ) -> tuple[PlexTrack, ...]:
        return tuple(reversed(self.tracks))

    def fetch_track(
        self,
        _server: PlexServer,
        track: PlexTrack,
        destination: Path,
        *,
        mappings: Iterable[PathMapping] = (),
        cancelled: Event | None = None,
    ) -> None:
        self.fetched.append(track.rating_key)
        destination.write_bytes(b"x" * 4_000)

    def fetch_artwork(
        self, _server: PlexServer, _thumb: str, destination: Path
    ) -> None:
        destination.write_bytes(JPEG)


def tracks() -> tuple[PlexTrack, ...]:
    from dataclasses import replace

    one = replace(
        plex("1", "One", "Band", "Album", 200_000), album_thumb="/t/1", part_size=4_000
    )
    two = replace(
        plex("2", "Two", "Band", "Album", 200_000), album_thumb="/t/1", part_size=4_000
    )
    return (one, two)


def remote(tmp_path: Path, client: StubPlex) -> ConnectedRemote:
    return ConnectedRemote(
        tmp_path / "Connected",
        tmp_path / "catalog.json",
        plex=client,  # type: ignore[arg-type]
        server=SERVER,
    )


def source(entry: RemoteItem) -> HostMediaSource:
    return HostMediaSource(
        entry.path,
        HostMediaFileKind.AUDIO,
        entry.size_bytes,
        entry.modified_ns,
        acoustic_fingerprint=entry.identity,
        remote=True,
    )


def test_plex_is_listed_downloaded_once_and_cached(tmp_path: Path) -> None:
    client = StubPlex(tracks())
    catalog = remote(tmp_path, client).catalog(Event())
    one, two = catalog.items
    assert one.key == "plex:srv:1" and not one.cached
    assert one.path.path.relative_to(tmp_path / "Connected/Plex").parts[:2] == (
        "Band",
        "Album",
    )
    assert one.size_bytes == 4_000 and one.modified_ns == revision_time_ns(one.revision)
    assert one.cover is not None and Path(one.cover.path.path).read_bytes() == JPEG
    (playlist,) = catalog.playlists
    assert playlist.name == "Road Trip"
    assert [str(p) for p in playlist.members] == [str(two.path), str(one.path)]
    assert client.fetched == []  # listing downloads nothing but covers

    downloader = remote(tmp_path, client)
    downloader.catalog(Event())
    got = downloader.materialize(source(one), lambda: None, lambda _p: None)
    assert client.fetched == ["1"] and got.remote
    assert Path(got.path.path).read_bytes() == b"x" * 4_000
    # The file gets the listing's facts, so the next plan sees no change.
    assert (got.size_bytes, got.modified_ns) == (one.size_bytes, one.modified_ns)
    again = downloader.materialize(source(one), lambda: None, lambda _p: None)
    assert client.fetched == ["1"] and again == got

    relisted = remote(tmp_path, client).catalog(Event())
    assert relisted.items[0].cached and not relisted.items[1].cached
    assert (relisted.items[0].size_bytes, relisted.items[0].modified_ns) == (
        got.size_bytes,
        got.modified_ns,
    )


def test_earlier_picks_count_as_cached(tmp_path: Path) -> None:
    client = StubPlex(tracks())
    first = remote(tmp_path, client).catalog(Event()).items[0]
    folder = tmp_path / "Connected/Plex"
    relative = first.path.path.relative_to(folder).as_posix()
    Path(first.path.path).parent.mkdir(parents=True, exist_ok=True)
    Path(first.path.path).write_bytes(b"y" * 4_000)
    os.utime(first.path.path, ns=(1, 777))
    owned = load_owned(folder)
    owned[relative] = Owned(first.revision, 4_000)
    save_owned(folder, owned)
    item = remote(tmp_path, client).catalog(Event()).items[0]
    assert item.cached and item.modified_ns == 777  # facts the iPod copy recorded


def test_offline_plex_shows_the_last_listing(tmp_path: Path) -> None:
    client = StubPlex(tracks())
    remote(tmp_path, client).catalog(Event())
    client.offline = True
    catalog = remote(tmp_path, client).catalog(Event())
    assert len(catalog.items) == 2
    assert "last listed" in catalog.issues[0]


def test_cache_limit_and_cache_off(tmp_path: Path) -> None:
    client = StubPlex(tracks())
    downloader = remote(tmp_path, client)
    one, two = downloader.catalog(Event()).items
    downloader.materialize(source(one), lambda: None, lambda _p: None)
    downloader.materialize(source(two), lambda: None, lambda _p: None)
    folder = tmp_path / "Connected/Plex"
    owned = load_owned(folder)
    for key, entry in owned.items():  # make "one" the least recently synced
        if key.endswith(one.path.path.name):
            owned[key] = Owned(entry.revision, entry.size, used=1)
    save_owned(folder, owned)

    removed, kept = downloader.trim(keep=True, limit_bytes=5_000)
    assert (removed, kept) == (1, 4_000)
    assert not Path(one.path.path).exists() and Path(two.path.path).exists()
    assert one.cover is not None and Path(one.cover.path.path).exists()  # covers stay

    removed, kept = downloader.trim(keep=False, limit_bytes=10**9)
    assert (removed, kept) == (1, 0) and not Path(two.path.path).exists()


def test_cancelling_stops_a_download(tmp_path: Path) -> None:
    from iOpenPod.app.connected.http import CancelledError

    class CancelledHereError(Exception):
        pass

    class Slow(StubPlex):
        def fetch_track(
            self, *args: object, cancelled: Event | None = None, **kw: object
        ) -> None:
            assert cancelled is not None and cancelled.is_set()
            raise CancelledError

    def checkpoint() -> None:
        raise CancelledHereError

    downloader = remote(tmp_path, Slow(tracks()))
    first = downloader.catalog(Event()).items[0]
    with pytest.raises(CancelledHereError):
        downloader.materialize(source(first), checkpoint, lambda _p: None)
    assert not Path(first.path.path).exists()


def test_grimmory_audiobooks_are_listed_and_tagged_on_download(tmp_path: Path) -> None:
    from mutagen.id3 import ID3

    transport = FakeTransport(
        {
            f"GET {BASE}/api/v1/app/books": {
                "content": [
                    {"id": 9, "title": "Folder Book", "authors": ["A. Writer"]}
                ],
                "hasNext": False,
            },
            f"GET {BASE}/api/v1/audiobooks/9/info": {
                "bookFileId": 90,
                "title": "Folder Book",
                "durationMs": 120_000,
                "folderBased": True,
                "tracks": [
                    {
                        "index": i,
                        "fileName": f"{i}.mp3",
                        "title": f"Part {i + 1}",
                        "durationMs": 60_000,
                        "cumulativeStartMs": i * 60_000,
                    }
                    for i in (0, 1)
                ],
            },
            f"GET {BASE}/api/v1/audiobooks/9/cover": reply(
                body=JPEG, content_type="image/jpeg"
            ),
        }
    )
    transport.files[f"{BASE}/api/v1/audiobooks/9/track/"] = MP3
    downloader = ConnectedRemote(
        tmp_path / "Connected", tmp_path / "catalog.json", grimmory=signed_in(transport)
    )
    one, two = downloader.catalog(Event()).items
    assert one.path.path.relative_to(tmp_path / "Connected/Audiobooks").as_posix() == (
        "A. Writer - Folder Book/01 Part 1.mp3"
    )
    assert one.media_type is MediaType.AUDIOBOOK
    assert (one.album, one.artist, one.track_number, one.total_tracks) == (
        "Folder Book",
        "A. Writer",
        1,
        2,
    )
    assert two.comment == "grimmory:9:90:1:60000:120000"
    assert one.size_bytes == 60_000 * 16  # an estimate until it is downloaded
    got = downloader.materialize(source(two), lambda: None, lambda _p: None)
    tags = ID3(got.path.path)  # type: ignore[no-untyped-call]
    assert tags.getall("COMM")[0].text == ["grimmory:9:90:1:60000:120000"]  # type: ignore[no-untyped-call]
    # The real size is remembered for listings after the cache drops it.
    downloader.trim(keep=False, limit_bytes=0)
    relisted = ConnectedRemote(
        tmp_path / "Connected", tmp_path / "catalog.json", grimmory=signed_in(transport)
    ).catalog(Event())
    assert relisted.items[1].size_bytes == got.size_bytes
    assert relisted.items[1].modified_ns == got.modified_ns
    # Audiobook details were remembered, so only the list is fetched again.
    infos = [c for c in transport.calls if "/info" in c[1]]
    assert len(infos) == 1
