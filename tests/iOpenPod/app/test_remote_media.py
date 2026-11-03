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
from iPodDB.library import MediaType
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
