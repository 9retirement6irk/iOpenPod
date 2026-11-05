"""Remote Items join the Host Media Library and Sync Plan without a file."""

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from tests.iOpenPod.app.test_sync_plan import (
    _host_library,
    _host_track,
    _ipod_library,
    _ipod_snapshot,
    _ipod_track,
    _sync,
)

from iOpenPod.app.host_media_library import stable_playlist_identity
from iOpenPod.app.remote_media import (
    RemoteCatalog,
    RemoteCover,
    RemoteItem,
    RemotePlaylist,
    merge_remote,
    revision_time_ns,
)
from iOpenPod.app.sync_plan import (
    SyncPlanAction,
    SyncPlanBasis,
    is_remote_identity,
    prepare_sync_plan,
    select_sync_plan,
)
from iPodDB.library import LibrarySnapshot, MediaType, Track, TrackMetadata
from storage import HostPath

SHA = "a" * 64


def item(path: Path, key: str = "plex:s:1", **changes: object) -> RemoteItem:
    base = RemoteItem(
        key,
        HostPath(path),
        "rev-1",
        4_000,
        revision_time_ns("rev-1"),
        False,
        title="Song",
        artist="Band",
        album="Album",
        album_artist="Band",
        track_number=3,
        length_ms=180_000,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_items_become_host_tracks_with_remote_sources(tmp_path: Path) -> None:
    local, local_source = _host_track(
        tmp_path / "local.mp3", track_id=1, fingerprint="1,2"
    )
    library = _host_library((local,), (), (local_source,))
    cover = RemoteCover(HostPath(tmp_path / "Band/Album/cover.jpg"), 10, 5, SHA)
    song = item(tmp_path / "Band/Album/03 Song [1].flac", cover=cover)
    book = item(
        tmp_path / "Audiobooks/Book/01 Part.mp3",
        "grimmory:7:0",
        media_type=MediaType.AUDIOBOOK,
        comment="grimmory:7:70:0:0:5000",
    )
    clash = item(tmp_path / "local.mp3", "plex:s:9")  # a scanned file wins
    playlist = RemotePlaylist(
        HostPath(tmp_path / "Playlists/Mix.m3u8"),
        "Mix",
        (song.path, HostPath(tmp_path / "gone.mp3"), book.path),
    )
    merged = merge_remote(
        library, RemoteCatalog((song, book, clash), playlists=(playlist,))
    )
    assert len(merged.snapshot.tracks) == 3
    remote = [s for s in merged.sources if s.remote]
    assert [str(s.path) for s in remote] == [str(song.path), str(book.path)]
    assert all(is_remote_identity(s.acoustic_fingerprint) for s in remote)
    added = {t.metadata.location: t for t in merged.snapshot.tracks}
    track = added[str(song.path)]
    assert (track.title, track.album, track.track_number) == ("Song", "Album", 3)
    assert track.metadata.file_format == "flac"
    assert track.artwork_id == merged.artwork_sources[0].artwork_id
    assert added[str(book.path)].media_types == (MediaType.AUDIOBOOK,)
    assert added[str(book.path)].metadata.comment == "grimmory:7:70:0:0:5000"
    (mix,) = merged.snapshot.playlists
    assert mix.playlist_id == stable_playlist_identity(playlist.path.path)
    assert [e.track_id for e in mix.entries] == [
        added[str(song.path)].track_id,
        added[str(book.path)].track_id,
    ]
    # Merging again (after a targeted tag refresh) adds nothing twice.
    assert (
        merge_remote(merged, RemoteCatalog((song, book), playlists=(playlist,)))
        == merged
    )


def test_new_remote_items_can_be_checked_for_the_ipod(tmp_path: Path) -> None:
    song = item(tmp_path / "Band/Album/03 Song [1].flac")
    library = merge_remote(_host_library((), (), ()), RemoteCatalog((song,)))
    plan = prepare_sync_plan(library, _ipod_library(()), _ipod_snapshot())
    (entry,) = plan.items
    assert entry.action is SyncPlanAction.ADD and entry.ipod_id is None
    chosen = select_sync_plan(
        plan,
        selected_host_paths=frozenset({entry.host_path or ""}),
        selected_ipod_removals=frozenset(),
    )
    assert [i.action for i in chosen.items] == [SyncPlanAction.ADD]


def test_items_synced_before_streaming_keep_their_ipod_copy(tmp_path: Path) -> None:
    """A pick downloaded to the same place pairs by path; its real fingerprint
    is never compared with the remote identity, so nothing is copied again."""

    path = tmp_path / "Band/Album/03 Song [1].flac"
    cached = item(path, size_bytes=100, modified_ns=1_000, cached=True)
    synced = _ipod_track(track_id=10, fingerprint="1,2,3", sync=_sync(path))
    plan = prepare_sync_plan(
        merge_remote(_host_library((), (), ()), RemoteCatalog((cached,))),
        _ipod_library((synced,)),
        _ipod_snapshot(10),
    )
    (entry,) = plan.items
    assert entry.action is SyncPlanAction.UNCHANGED
    assert entry.basis is not SyncPlanBasis.AUDIO_PAYLOAD_CHANGED

    # A new revision on the server changes its facts: the iPod copy is replaced.
    changed = item(path, revision="rev-2", modified_ns=revision_time_ns("rev-2"))
    plan = prepare_sync_plan(
        merge_remote(_host_library((), (), ()), RemoteCatalog((changed,))),
        _ipod_library((synced,)),
        _ipod_snapshot(10),
    )
    (entry,) = plan.items
    assert entry.action is SyncPlanAction.UPDATE and entry.audio_payload_changed


def test_remote_identities_still_pair_with_each_other(tmp_path: Path) -> None:
    song = item(tmp_path / "moved/Song.flac")
    synced = _ipod_track(
        track_id=10,
        fingerprint=song.identity,
        sync=_sync(tmp_path / "old/Song.flac", size=4_000, modified=song.modified_ns),
    )
    plan = prepare_sync_plan(
        merge_remote(_host_library((), (), ()), RemoteCatalog((song,))),
        _ipod_library((synced,)),
        _ipod_snapshot(10),
    )
    (entry,) = plan.items
    assert entry.ipod_id == 10 and entry.action is not SyncPlanAction.ADD


def test_remote_identities_are_stored_like_fingerprints(tmp_path: Path) -> None:
    from iOpenPod.app.library_sync_helper import IPodTrackFingerprint
    from iOpenPod.app.media.fingerprint_codec import (
        decode_fingerprint,
        encode_fingerprint,
    )
    from storage import DevicePath

    identity = item(tmp_path / "a.flac").identity
    assert is_remote_identity(identity)
    assert identity != item(tmp_path / "a.flac", "plex:s:2").identity
    assert decode_fingerprint(encode_fingerprint(identity)) == identity
    IPodTrackFingerprint(
        1, 1, DevicePath("iPod_Control/Music/F00/a.flac"), 1, 1, identity
    )
    assert not is_remote_identity("0,0,0,12,4294967295,0,1")


@pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("ffmpeg", "ffprobe")),
    reason="Sync media tools required",
)
def test_sync_downloads_a_remote_item_and_records_its_identity(tmp_path: Path) -> None:
    """End to end: a checked remote item is downloaded while Sync prepares it."""

    import base64
    from threading import Event

    from tests.iOpenPod.app.services.test_first_artwork_save import bare_device
    from tests.iOpenPod.app.test_music_import import FIXTURES

    from iOpenPod.app.host_media_library import HostMediaSource
    from iOpenPod.app.library_sync_helper import IPodMediaCacheStats, IPodMediaLibrary
    from iOpenPod.app.sync_execution import (
        SyncExecutionRequest,
        SyncExecutionStatus,
        SyncExecutor,
    )
    from iOpenPod.app.sync_plan import host_path_identity

    tone = base64.decodebytes((FIXTURES / "tone.mp3.b64").read_bytes())
    cached = tmp_path / "Connected/Plex/Band/Album/01 Tone [5].mp3"
    song = item(cached, "plex:s:5", title="Remote tone", length_ms=1_000)
    downloads: list[str] = []

    class Downloader:
        def materialize(
            self,
            source: HostMediaSource,
            checkpoint: object,
            progress: object,
        ) -> HostMediaSource:
            downloads.append(str(source.path))
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(tone)
            stat = cached.stat()
            return replace(
                source, size_bytes=stat.st_size, modified_ns=stat.st_mtime_ns
            )

    device = bare_device(tmp_path, model_number="M9802")
    try:
        host = merge_remote(_host_library((), (), ()), RemoteCatalog((song,)))
        ipod = IPodMediaLibrary((), (), (), IPodMediaCacheStats(), None, False)
        plan = select_sync_plan(
            prepare_sync_plan(host, ipod, device.active.library),
            selected_host_paths=frozenset({host_path_identity(str(cached))}),
            selected_ipod_removals=frozenset(),
        )
        request = replace(
            SyncExecutionRequest(plan, host, ipod, device.active, 1, 1),
            remote=Downloader(),
        )
        result = SyncExecutor(device.coordinator).execute(
            request, lambda _: None, Event()
        )
        assert result.status is SyncExecutionStatus.SUCCESS, result.issues
        assert downloads == [str(cached)]
        assert result.active is not None
        assert any(t.title == "Remote tone" for t in result.active.library.tracks)
        assert result.helper is not None
        (synced,) = result.helper.tracks
        assert synced.acoustic_fingerprint == song.identity
        assert synced.sync is not None
        assert synced.sync.host_path_hint == str(cached)
        assert synced.sync.host_size_bytes == len(tone)

        # Without a downloader (signed out), the item is reported, not crashed on.
        failed = SyncExecutor(device.coordinator).execute(
            SyncExecutionRequest(plan, host, ipod, device.active, 1, 1),
            lambda _: None,
            Event(),
        )
        assert failed.status is not SyncExecutionStatus.SUCCESS or failed.issues
    finally:
        device.coordinator.close()


def test_a_song_synced_on_another_computer_shows_as_ticked_and_unchanged(
    tmp_path: Path,
) -> None:
    """Synced from Plex on one computer, compared on another whose cache lives
    elsewhere: the Remote Identity pairs them, so it is on the iPod and ticked."""

    from PySide6.QtCore import Qt

    from iOpenPod.app.models.sync_selection import SyncSelection

    mac_cache = (
        tmp_path
        / "Users/will/Music/iOpenPod Connected/Plex/Band/Album/03 Song [1].flac"
    )
    windows_cache = (
        tmp_path
        / "C/Users/will/Music/iOpenPod Connected/Plex/Band/Album/03 Song [1].flac"
    )
    on_windows = item(windows_cache)  # same Plex server and item, not cached here
    synced_on_mac = _ipod_track(
        track_id=10,
        fingerprint=on_windows.identity,
        sync=_sync(
            mac_cache, size=on_windows.size_bytes, modified=on_windows.modified_ns
        ),
    )
    host = merge_remote(_host_library((), (), ()), RemoteCatalog((on_windows,)))
    plan = prepare_sync_plan(host, _ipod_library((synced_on_mac,)), _ipod_snapshot(10))
    (entry,) = plan.items
    assert entry.ipod_id == 10
    assert entry.action is SyncPlanAction.UNCHANGED
    assert not entry.audio_payload_changed
    selection = SyncSelection()
    selection.reset(host, plan)
    (track,) = host.snapshot.tracks
    assert selection.track_on_ipod(track.track_id)
    assert selection.track_check_state(track.track_id) is Qt.CheckState.Checked


@pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("ffmpeg", "ffprobe")),
    reason="Video preparation requires FFmpeg and FFprobe",
)
@pytest.mark.parametrize(
    "media_type", [MediaType.TV_SHOW, MediaType.MUSIC_VIDEO, MediaType.VIDEO]
)
def test_sync_downloads_and_converts_a_remote_video(
    tmp_path: Path, media_type: MediaType
) -> None:
    """A Plex video is downloaded during Sync, converted like a Host video, and
    written with its media type, so it lands in the iPod's matching Videos menu."""

    import subprocess
    from threading import Event

    from tests.iOpenPod.app.services.test_library_resources import build_device
    from tests.iOpenPod.app.test_sync_execution import (
        _request,  # pyright: ignore[reportPrivateUsage]
    )

    from iOpenPod.app.host_media_library import HostMediaFileKind, HostMediaSource
    from iOpenPod.app.sync_execution import SyncExecutionStatus, SyncExecutor

    made = tmp_path / "server.mkv"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=s=320x240:r=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(made),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    cached = tmp_path / "Connected/Plex/TV Shows/Show/Season 01/S01E02 - Pilot [20].mkv"
    episode = item(
        cached,
        "plex:s:20",
        title="Pilot",
        artist="Show",
        album="Season 1",
        album_artist="Show",
        track_number=2,
        length_ms=2_000,
        media_type=media_type,
        show="Show" if media_type is MediaType.TV_SHOW else "",
        season_number=1 if media_type is MediaType.TV_SHOW else 0,
        episode_number=2 if media_type is MediaType.TV_SHOW else 0,
        description="Day one.",
    )
    downloads: list[HostMediaFileKind] = []

    class Downloader:
        def materialize(
            self, source: HostMediaSource, checkpoint: object, progress: object
        ) -> HostMediaSource:
            downloads.append(source.kind)
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(made, cached)
            stat = cached.stat()
            return replace(
                source, size_bytes=stat.st_size, modified_ns=stat.st_mtime_ns
            )

    device = build_device(tmp_path)
    try:
        host = merge_remote(_host_library((), (), ()), RemoteCatalog((episode,)))
        request = replace(_request(device, host), remote=Downloader())
        assert [i.action for i in request.plan.items] == [SyncPlanAction.ADD]
        result = SyncExecutor(device.coordinator).execute(
            request, lambda _: None, Event()
        )
        assert result.status is SyncExecutionStatus.SUCCESS, result.issues
        assert downloads == [HostMediaFileKind.VIDEO]
        assert result.active is not None
        # Read the committed database back from the iPod, as the firmware would.
        from iPodDB.library import IPodLibrary

        database = device.root / "iPod_Control/iTunes/iTunesDB"
        reread = IPodLibrary.parse(database.read_bytes()).snapshot
        (track,) = (t for t in reread.tracks if t.title == "Pilot")
        assert track.media_types == (media_type,)
        if media_type is MediaType.TV_SHOW:
            assert (track.show, track.season_number, track.episode_number) == (
                "Show",
                1,
                2,
            )
        assert track.metadata.location.endswith((".m4v", ".mp4"))
        assert result.helper is not None
        synced = next(t for t in result.helper.tracks if t.track_id == track.track_id)
        assert synced.acoustic_fingerprint == episode.identity
        assert synced.sync is not None and synced.sync.was_transcoded
    finally:
        device.coordinator.close()


def _ipod_songs(*songs: tuple[int, str, str, str, int, int]) -> LibrarySnapshot:
    """(id, title, artist, album, track number, length) for songs on the iPod."""

    return LibrarySnapshot(
        tracks=tuple(
            Track(
                identity,
                title,
                artist,
                album,
                length,
                track_number=number,
                metadata=TrackMetadata(
                    location=f"iPod_Control/Music/F00/track-{identity}.m4a"
                ),
            )
            for identity, title, artist, album, number, length in songs
        )
    )


FLAC_FINGERPRINT = "1,2,3,4"


def test_plex_songs_pair_with_songs_added_from_local_flac_files(
    tmp_path: Path,
) -> None:
    """Songs put on the iPod from FLAC files (Acoustic Fingerprints, no Sync
    record) are the same songs Plex lists: pair by details, copy nothing."""

    from PySide6.QtCore import Qt

    from iOpenPod.app.models.sync_selection import SyncSelection

    ipod_songs = _ipod_songs(
        (1, "Karma Police", "Radiohead", "OK Computer", 6, 264_000),
        (2, "Airbag", "Radiohead", "OK Computer", 1, 284_000),
        (3, "Lucky (Live)", "Radiohead", "Live Album", 4, 300_000),
        (4, "Only On The iPod", "Someone", "Elsewhere", 1, 200_000),
    )
    ipod = _ipod_library(
        (
            _ipod_track(track_id=1, fingerprint=FLAC_FINGERPRINT),
            _ipod_track(track_id=2, fingerprint="5,6,7"),
            # Synced earlier from a local folder: still kept as it is.
            _ipod_track(
                track_id=3,
                fingerprint="8,9",
                sync=_sync(tmp_path / "flac/Lucky.flac", size=9_999),
            ),
            _ipod_track(track_id=4, fingerprint="10,11"),
        )
    )
    plex = RemoteCatalog(
        (
            # Plex spells it differently; the details still agree.
            item(
                tmp_path / "Plex/Radiohead/OK Computer/06 Karma Police [1].flac",
                "plex:s:1",
                title="karma police",
                artist="Radiohead",
                album="OK Computer",
                track_number=6,
                length_ms=264_500,
            ),
            # Plex renamed the title; album, number and artist agree.
            item(
                tmp_path / "Plex/Radiohead/OK Computer/01 Airbag [2].flac",
                "plex:s:2",
                title="Airbag (Remastered)",
                artist="Radiohead",
                album="OK Computer",
                track_number=1,
                length_ms=284_000,
            ),
            item(
                tmp_path / "Plex/Radiohead/Live Album/04 Lucky (Live) [3].flac",
                "plex:s:3",
                title="Lucky (Live)",
                artist="Radiohead",
                album="Live Album",
                track_number=4,
                length_ms=301_000,
            ),
            # Same name, different recording length: not the iPod's song.
            item(
                tmp_path / "Plex/Someone/Other/01 Only On The iPod [4].flac",
                "plex:s:4",
                title="Only On The iPod",
                artist="Someone",
                album="Other",
                track_number=1,
                length_ms=260_000,
            ),
        )
    )
    host = merge_remote(_host_library((), (), ()), plex)
    plan = prepare_sync_plan(host, ipod, ipod_songs)
    by_key = {entry.ipod_id: entry for entry in plan.items if entry.ipod_id}
    for track_id in (1, 2, 3):
        entry = by_key[track_id]
        assert entry.action is SyncPlanAction.UNCHANGED, entry
        assert entry.basis is SyncPlanBasis.TRACK_DETAILS_MATCH
        assert entry.host_path is not None and "Plex" in entry.host_path
    assert by_key[4].action is SyncPlanAction.REMOVE
    assert plan.count(SyncPlanAction.ADD) == 1  # only the different recording

    # Paired songs show as on the iPod and ticked, so nothing is copied twice.
    selection = SyncSelection()
    selection.reset(host, plan)
    paired = [t.track_id for t in host.snapshot.tracks if t.title != "Only On The iPod"]
    assert all(selection.track_on_ipod(i) for i in paired)
    assert all(selection.track_check_state(i) is Qt.CheckState.Checked for i in paired)
    assert not any(
        e.action is SyncPlanAction.ADD and e.ipod_id
        for e in select_sync_plan(
            plan,
            selected_host_paths=selection.selected_host_paths,
            selected_ipod_removals=frozenset(),
        ).items
    )


def test_details_pair_only_when_unique_on_both_sides(tmp_path: Path) -> None:
    """Duplicates on either side are left alone rather than guessed."""

    song = ("Intro", "Band", "Album", 1, 60_000)
    ipod_songs = _ipod_songs((1, *song), (2, *song))  # the same song twice
    ipod = _ipod_library(
        (
            _ipod_track(track_id=1, fingerprint="1,2"),
            _ipod_track(track_id=2, fingerprint="3,4"),
        )
    )
    remote = item(
        tmp_path / "Plex/Band/Album/01 Intro [1].flac",
        "plex:s:1",
        title="Intro",
        artist="Band",
        album="Album",
        track_number=1,
        length_ms=60_000,
    )
    plan = prepare_sync_plan(
        merge_remote(_host_library((), (), ()), RemoteCatalog((remote,))),
        ipod,
        ipod_songs,
    )
    assert not any(e.basis is SyncPlanBasis.TRACK_DETAILS_MATCH for e in plan.items)

    # Two Plex copies of one iPod song: also left alone.
    twin = replace(
        remote, key="plex:s:2", path=HostPath(tmp_path / "Plex/Band/Copy/01 Intro.flac")
    )
    plan = prepare_sync_plan(
        merge_remote(_host_library((), (), ()), RemoteCatalog((remote, twin))),
        _ipod_library((_ipod_track(track_id=1, fingerprint="1,2"),)),
        _ipod_songs((1, *song)),
    )
    assert not any(e.basis is SyncPlanBasis.TRACK_DETAILS_MATCH for e in plan.items)

    # A local file with an Acoustic Fingerprint pairs by its sound, never by details.
    local, local_source = _host_track(
        tmp_path / "Intro.mp3", track_id=9, fingerprint="7,7"
    )
    plan = prepare_sync_plan(
        _host_library((local,), (), (local_source,)),
        _ipod_library((_ipod_track(track_id=1, fingerprint="1,2"),)),
        _ipod_songs((1, "Song 9", "Artist", "Album", 0, 180_000)),
    )
    assert not any(e.basis is SyncPlanBasis.TRACK_DETAILS_MATCH for e in plan.items)
