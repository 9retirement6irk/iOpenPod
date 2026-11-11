"""Remote Host media: Connected Source items listed in Sync before any download.

A Remote Item is a Plex song, video or Grimmory audiobook track known from its
server's description. It joins the Host Media Library beside scanned files so the Sync
workspace can show it and Sync Plan can compare it, but its file exists only once
Sync prepares it (or while it stays in the size-limited media cache).

Each item's ``path`` is where its download is cached, so a cached item is the same
Host Track before and after download, and it is matched to its iPod copy by that
path, like a file that never moved. Its fingerprint is a Remote Identity (a marker plus
a digest of ``<source>:<id>``, in fpcalc's raw form so Sync can store it) rather
than audio evidence; Sync Plan compares file facts
for remote items instead, and the facts here are chosen to stay stable across
downloads: the cached file's facts when the cached copy is current, otherwise the
size the server reports (or the size last downloaded) and a time derived from the
item's revision, which a download also gives its file.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from iOpenPod.app.host_media_library import (
    HostMediaFileKind,
    HostMediaSource,
    folder_artwork_source,
    playlist_entry_identity,
    stable_playlist_identity,
    stable_track_identity,
)
from iOpenPod.app.sync_plan import REMOTE_IDENTITY_PREFIX, host_path_identity
from iPodDB.library import (
    MediaType,
    Playlist,
    PlaylistEntry,
    PlaylistKind,
    PlaylistSortOrder,
    Track,
    TrackMetadata,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from iOpenPod.app.host_media_library import HostMediaLibrary
    from iOpenPod.app.media.progress import MediaPreparationProgress
    from storage import HostPath


_VIDEO_TYPES = frozenset({MediaType.VIDEO, MediaType.MUSIC_VIDEO, MediaType.TV_SHOW})


def revision_time_ns(revision: str) -> int:
    """A stable stand-in modification time that changes with the revision."""

    digest = hashlib.blake2b(revision.encode(), digest_size=4).digest()
    seconds = 1_000_000_000 + int.from_bytes(digest, "big") % 600_000_000
    return seconds * 1_000_000_000


@dataclass(frozen=True, slots=True)
class RemoteCover:
    path: HostPath
    size_bytes: int
    modified_ns: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class RemoteItem:
    key: str  # "<source>:<server item id>", stable while the item exists
    path: HostPath  # its cache location
    revision: str
    size_bytes: int
    modified_ns: int
    cached: bool
    title: str
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    year: int = 0
    track_number: int = 0
    total_tracks: int = 0
    disc_number: int = 0
    total_discs: int = 0
    length_ms: int = 0
    bitrate_kbps: int = 0
    media_type: MediaType = MediaType.AUDIO
    comment: str = ""
    cover: RemoteCover | None = None
    # Videos: a TV episode's show, season and episode, and any description.
    show: str = ""
    season_number: int = 0
    episode_number: int = 0
    description: str = ""

    @property
    def video(self) -> bool:
        return self.media_type in _VIDEO_TYPES

    @property
    def identity(self) -> str:
        """This item's Remote Identity, in the raw fingerprint form Sync stores."""

        digest = hashlib.blake2b(self.key.encode(), digest_size=16).digest()
        values = (
            int.from_bytes(digest[index : index + 4], "big")
            for index in range(0, 16, 4)
        )
        return REMOTE_IDENTITY_PREFIX + ",".join(str(value) for value in values)


@dataclass(frozen=True, slots=True)
class RemotePlaylist:
    path: HostPath  # its identity, as if it were a playlist file
    name: str
    members: tuple[HostPath, ...]  # Remote Item paths, in order


@dataclass(frozen=True, slots=True)
class RemoteCatalog:
    items: tuple[RemoteItem, ...] = ()
    issues: tuple[str, ...] = ()
    playlists: tuple[RemotePlaylist, ...] = ()


def merge_remote(library: HostMediaLibrary, catalog: RemoteCatalog) -> HostMediaLibrary:
    """Add remote items as Host Tracks; a scanned file at the same path wins."""

    if not catalog.items and not catalog.playlists:
        return library
    present = {host_path_identity(str(source.path)) for source in library.sources}
    track_ids = {track.track_id for track in library.snapshot.tracks}
    artwork = {source.artwork_id: source for source in library.artwork_sources}
    tracks: list[Track] = []
    sources: list[HostMediaSource] = []
    for item in catalog.items:
        identity = host_path_identity(str(item.path))
        track_id = stable_track_identity(item.path.path)
        if identity in present or track_id in track_ids:
            continue
        present.add(identity)
        track_ids.add(track_id)
        artwork_id = 0
        if item.cover is not None:
            cover = folder_artwork_source(
                item.cover.path,
                item.cover.size_bytes,
                item.cover.modified_ns,
                item.cover.content_sha256,
            )
            artwork.setdefault(cover.artwork_id, cover)
            artwork_id = cover.artwork_id
        suffix = item.path.path.suffix
        tracks.append(
            Track(
                track_id=track_id,
                title=item.title or item.path.path.stem,
                artist=item.artist,
                album=item.album,
                length_ms=item.length_ms,
                genre=item.genre,
                year=item.year,
                track_number=item.track_number,
                size_bytes=item.size_bytes,
                bitrate_kbps=item.bitrate_kbps,
                artwork_id=artwork_id,
                media_types=(item.media_type,),
                album_artist=item.album_artist,
                show=item.show,
                episode=(
                    f"S{item.season_number:02d}E{item.episode_number:02d}"
                    if item.show and item.episode_number
                    else ""
                ),
                season_number=item.season_number,
                episode_number=item.episode_number,
                metadata=TrackMetadata(
                    file_format=suffix.casefold().lstrip("."),
                    last_modified=item.modified_ns // 1_000_000_000,
                    total_tracks=item.total_tracks,
                    disc_number=item.disc_number,
                    total_discs=item.total_discs,
                    location=str(item.path),
                    comment=item.comment,
                    description=item.description,
                ),
            )
        )
        sources.append(
            HostMediaSource(
                item.path,
                HostMediaFileKind.VIDEO if item.video else HostMediaFileKind.AUDIO,
                item.size_bytes,
                item.modified_ns,
                acoustic_fingerprint=item.identity,
                remote=True,
            )
        )
    by_path = {
        host_path_identity(track.metadata.location): track.track_id
        for track in (*library.snapshot.tracks, *tracks)
        if track.metadata.location
    }
    playlist_ids = {playlist.playlist_id for playlist in library.snapshot.playlists}
    playlists: list[Playlist] = []
    for remote in catalog.playlists:
        playlist_id = stable_playlist_identity(remote.path.path)
        if playlist_id in playlist_ids:
            continue
        playlist_ids.add(playlist_id)
        entries: list[PlaylistEntry] = []
        for position, member in enumerate(remote.members):
            member_id = by_path.get(host_path_identity(str(member)))
            if member_id is not None:
                entries.append(
                    PlaylistEntry(
                        entry_id=playlist_entry_identity(
                            remote.path.path, position, member.path
                        ),
                        track_id=member_id,
                        position=len(entries),
                    )
                )
        playlists.append(
            Playlist(
                playlist_id=playlist_id,
                name=remote.name,
                kind=PlaylistKind.PLAYLIST,
                entries=tuple(entries),
                sort_order=PlaylistSortOrder.MANUAL,
            )
        )
    return replace(
        library,
        snapshot=replace(
            library.snapshot,
            tracks=(*library.snapshot.tracks, *tracks),
            playlists=(*library.snapshot.playlists, *playlists),
        ),
        sources=(*library.sources, *sources),
        artwork_sources=tuple(artwork.values()),
    )


class RemoteMedia(Protocol):
    """Downloads remote items while Sync prepares them."""

    def materialize(
        self,
        source: HostMediaSource,
        checkpoint: Callable[[], None],
        progress: Callable[[MediaPreparationProgress], None],
    ) -> HostMediaSource:
        """Make ``source.path`` a current local file; return its observed facts.

        The returned source keeps ``remote=True`` and the Remote Identity. It may
        block (one download at a time per server), honours ``checkpoint`` for
        cancellation, and raises ``ValueError`` or ``OSError`` on failure.
        """
        ...

    def discard(self, source: HostMediaSource) -> bool:
        """Forget the local copy so the next ``materialize`` downloads it again.

        Returns whether there was a copy to forget. Used when a downloaded file
        turns out to be unreadable, so a damaged copy is never kept.
        """
        ...


__all__ = [
    "RemoteCatalog",
    "RemoteCover",
    "RemoteItem",
    "RemoteMedia",
    "RemotePlaylist",
    "merge_remote",
    "revision_time_ns",
]
