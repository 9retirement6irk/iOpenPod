"""Plex music and video and Grimmory audiobooks as remote Host media, with a cache.

Every Plex song, video from the chosen Plex video libraries, and Grimmory audiobook
track is listed as a Remote Item (see
``iOpenPod.app.remote_media``) from what its server reports. Nothing is downloaded
until Sync prepares an item for the iPod; the download lands at the item's cache
location inside the Connected Source Folder, which is where a pick was saved before
streaming existed, so earlier downloads count as cached copies and keep their iPod
match.

The cache keeps downloads for later Syncs up to a size limit, evicting the least
recently synced first; with the cache off, downloads are removed after each Sync.
Covers are small and always kept, since Sync reads them to give added items artwork.
A copy is current while its recorded revision matches the server's, and only files
whose size is unchanged are ever removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any, cast

from iOpenPod.app.host_media_library import HostMediaSource
from iOpenPod.app.media.progress import MediaPreparationPhase, MediaPreparationProgress
from iOpenPod.app.remote_media import (
    RemoteCatalog,
    RemoteCover,
    RemoteItem,
    RemotePlaylist,
    revision_time_ns,
)
from iOpenPod.app.sync_plan import host_path_identity
from iPodDB.library import MediaType
from storage import HostPath

from .config import PlexVideoUse
from .grimmory import (
    AudiobookInfo,
    AudiobookTrack,
    GrimmoryClient,
    GrimmoryLink,
    _extension,
    book_folder,
    tag_audiobook,
)
from .http import CancelledError, ConnectedError
from .materialize import (
    Owned,
    load_owned,
    owned_target,
    prune_empty,
    relative_path,
    safe_component,
    save_owned,
)
from .plex import PlexTrack
from .plex_source import PLAYLIST_FOLDER, track_relative

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from .config import PathMapping
    from .plex import PlexClient, PlexServer

PLEX_FOLDER = "Plex"
AUDIOBOOKS_FOLDER = "Audiobooks"
_COVER = "cover.jpg"
_COVER_WORKERS = 6
_DOWNLOADS_AT_ONCE = 2
# Without a known size, estimate 128 kbit/s (16 bytes per millisecond).
_BYTES_PER_MS = 16


def _now() -> int:
    return int(time.time())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(256 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class _CheckpointEvent(Event):
    """An Event that reports set once Sync's checkpoint says it was cancelled."""

    def __init__(self, checkpoint: Callable[[], None]) -> None:
        super().__init__()
        self._checkpoint = checkpoint

    def is_set(self) -> bool:
        try:
            self._checkpoint()
        except Exception:
            return True
        return super().is_set()


@dataclass(frozen=True, slots=True)
class _Spec:
    key: str
    folder: Path
    relative: str
    revision: str
    legacy: str  # an earlier pick's revision that is just as current
    fetch: Callable[[Path, Event], Path]  # fill a temporary folder, return the file


@dataclass(slots=True)
class _Memory:
    """What earlier listings learned: offline fallback, audiobook details, sizes."""

    plex_server: str = ""
    plex_tracks: list[dict[str, object]] = field(
        default_factory=list[dict[str, object]]
    )
    plex_playlists: list[dict[str, object]] = field(
        default_factory=list[dict[str, object]]
    )
    plex_videos: list[dict[str, object]] = field(
        default_factory=list[dict[str, object]]
    )
    books: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    infos: dict[str, dict[str, object]] = field(
        default_factory=dict[str, dict[str, object]]
    )
    sizes: dict[str, int] = field(default_factory=dict[str, int])


def _load_memory(path: Path) -> _Memory:
    try:
        raw: object = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return _Memory()
    if not isinstance(raw, dict):
        return _Memory()
    data = cast("dict[str, Any]", raw)
    memory = _Memory()
    for name in ("plex_server",):
        if isinstance(data.get(name), str):
            setattr(memory, name, data[name])
    for name in ("plex_tracks", "plex_playlists", "plex_videos", "books"):
        value = data.get(name)
        if isinstance(value, list):
            setattr(memory, name, [v for v in value if isinstance(v, dict)])
    for name in ("infos", "sizes"):
        value = data.get(name)
        if isinstance(value, dict):
            setattr(memory, name, dict(value))
    memory.sizes = {k: v for k, v in memory.sizes.items() if isinstance(v, int)}
    return memory


def _plex_track(data: dict[str, object]) -> PlexTrack | None:
    try:
        return PlexTrack(**cast("dict[str, Any]", data))
    except TypeError:
        return None


def _info(data: dict[str, object]) -> AudiobookInfo | None:
    try:
        values = dict(cast("dict[str, Any]", data))
        values["tracks"] = tuple(
            AudiobookTrack(**cast("dict[str, Any]", track))
            for track in values["tracks"]
        )
        return AudiobookInfo(**values)
    except (TypeError, KeyError):
        return None


class ConnectedRemote:
    """Lists Plex and Grimmory as Remote Items and downloads them during Sync."""

    def __init__(
        self,
        root: Path,
        memory: Path,
        *,
        plex: PlexClient | None = None,
        server: PlexServer | None = None,
        mappings: Iterable[PathMapping] = (),
        plex_skip: Iterable[str] = (),
        plex_videos: Mapping[str, str] | None = None,
        plex_music_videos: bool = False,
        grimmory: GrimmoryClient | None = None,
    ) -> None:
        self.root = root
        self._memory_path = memory
        self._memory = _load_memory(memory)
        self._plex, self._server = plex, server
        self._mappings = tuple(mappings)
        self._plex_skip = frozenset(plex_skip)
        # Video library key → PlexVideoUse value; only these libraries are listed.
        self._plex_videos = dict(plex_videos or {})
        self._plex_music_videos = plex_music_videos
        self._grimmory = grimmory
        self._specs: dict[str, _Spec] = {}
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(_DOWNLOADS_AT_ONCE)
        self.downloaded = 0

    @property
    def plex_folder(self) -> Path:
        return self.root / PLEX_FOLDER

    @property
    def audiobooks_folder(self) -> Path:
        return self.root / AUDIOBOOKS_FOLDER

    # --- listing -------------------------------------------------------------------

    def _wanted(self, video: PlexTrack) -> bool:
        """Whether a remembered video still belongs to the chosen libraries."""

        if video.extra:
            return self._plex_music_videos and video.section not in self._plex_skip
        return self._plex_videos.get(video.section) == video.media

    def catalog(
        self, cancelled: Event, progress: Callable[[str], None] = lambda _t: None
    ) -> RemoteCatalog:
        items: list[RemoteItem] = []
        playlists: list[RemotePlaylist] = []
        issues: list[str] = []
        if self._plex is not None and self._server is not None:
            found, lists, problems = self._plex_catalog(cancelled, progress)
            items.extend(found)
            playlists.extend(lists)
            issues.extend(problems)
        if self._grimmory is not None:
            found, problems = self._grimmory_catalog(cancelled, progress)
            items.extend(found)
            issues.extend(problems)
        self.save_memory()
        return RemoteCatalog(tuple(items), tuple(issues), tuple(playlists))

    def _cached(
        self, owned: dict[str, Owned], spec: _Spec, path: Path
    ) -> os.stat_result | None:
        entry = owned.get(spec.relative)
        if entry is None or entry.revision not in {spec.revision, spec.legacy}:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat if stat.st_size == entry.size else None

    def _item(
        self,
        spec: _Spec,
        owned: dict[str, Owned],
        size_hint: int,
        cover: RemoteCover | None,
        **details: Any,
    ) -> RemoteItem:
        path = spec.folder / spec.relative
        self._specs[host_path_identity(str(path))] = spec
        stat = self._cached(owned, spec, path)
        if stat is not None:
            size, modified, cached = stat.st_size, stat.st_mtime_ns, True
        else:
            size = self._memory.sizes.get(spec.key) or size_hint or 1
            modified, cached = revision_time_ns(spec.revision), False
        return RemoteItem(
            spec.key,
            HostPath(path),
            spec.revision,
            size,
            modified,
            cached,
            cover=cover,
            **details,
        )

    def _covers(
        self,
        folder: Path,
        owned: dict[str, Owned],
        wanted: dict[str, tuple[str, Callable[[Path], None]]],
        cancelled: Event,
        progress: Callable[[str], None],
        label: str,
    ) -> dict[str, RemoteCover]:
        """Covers keyed by item folder; missing ones download a few at a time."""

        missing = {
            directory: (revision, fetch)
            for directory, (revision, fetch) in wanted.items()
            if (entry := owned.get(f"{directory}/{_COVER}")) is None
            or entry.revision != revision
            or not (folder / directory / _COVER).is_file()
        }
        done = 0

        def fetch_one(
            directory: str, revision: str, fetch: Callable[[Path], None]
        ) -> None:
            nonlocal done
            if cancelled.is_set():
                return
            target = folder / directory / _COVER
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                dir=target.parent, prefix=".cover-", suffix=".part"
            )
            os.close(descriptor)
            part = Path(temporary)
            try:
                fetch(part)
                if part.stat().st_size and part.read_bytes()[:3] == b"\xff\xd8\xff":
                    os.replace(part, target)
                    with self._lock:
                        owned[f"{directory}/{_COVER}"] = Owned(
                            revision, target.stat().st_size
                        )
            except (ConnectedError, OSError):
                pass  # an item without a cover still syncs
            finally:
                part.unlink(missing_ok=True)
                with self._lock:
                    done += 1
                    if done % 25 == 0 or done == len(missing):
                        progress(f"{label}: artwork {done} of {len(missing)}")

        if missing:
            with ThreadPoolExecutor(_COVER_WORKERS) as pool:
                for directory, (revision, fetch) in missing.items():
                    pool.submit(fetch_one, directory, revision, fetch)
            save_owned(folder, owned)
        if cancelled.is_set():
            raise CancelledError
        covers: dict[str, RemoteCover] = {}
        for directory in wanted:
            path = folder / directory / _COVER
            if f"{directory}/{_COVER}" not in owned or not path.is_file():
                continue
            try:
                stat = path.stat()
                covers[directory] = RemoteCover(
                    HostPath(path), stat.st_size, stat.st_mtime_ns, _sha256(path)
                )
            except OSError:
                continue
        return covers

    def _plex_catalog(
        self, cancelled: Event, progress: Callable[[str], None]
    ) -> tuple[list[RemoteItem], list[RemotePlaylist], list[str]]:
        plex, server = self._plex, self._server
        assert plex is not None and server is not None
        issues: list[str] = []
        progress("Plex: listing your music…")
        try:
            tracks = plex.library_tracks(server, cancelled, skip=self._plex_skip)
            videos = (
                plex.library_videos(server, self._plex_videos, cancelled)
                if self._plex_videos
                else ()
            )
            if self._plex_music_videos:
                progress("Plex: looking for music videos…")
                videos = (*videos, *plex.music_videos(server, tracks, cancelled))
            listed = plex.playlists(server)
            playlists = [
                (item.title, plex.playlist_tracks(server, item.key, cancelled))
                for item in listed
            ]
            self._memory.plex_server = server.id
            self._memory.plex_tracks = [asdict(track) for track in tracks]
            self._memory.plex_videos = [asdict(video) for video in videos]
            self._memory.plex_playlists = [
                {"title": title, "keys": [t.rating_key for t in members]}
                for title, members in playlists
            ]
        except CancelledError:
            raise
        except ConnectedError as error:
            if self._memory.plex_server != server.id or not (
                self._memory.plex_tracks or self._memory.plex_videos
            ):
                return [], [], [f"Plex: {error}"]
            issues.append(f"Plex: {error} Showing your music as last listed.")
            tracks = tuple(
                t for row in self._memory.plex_tracks if (t := _plex_track(row))
            )
            by_key = {track.rating_key: track for track in tracks}
            playlists = [
                (
                    str(row.get("title") or ""),
                    tuple(
                        by_key[key]
                        for key in cast("list[str]", row.get("keys") or [])
                        if key in by_key
                    ),
                )
                for row in self._memory.plex_playlists
            ]
            tracks = tuple(t for t in tracks if t.section not in self._plex_skip)
            videos = tuple(
                video
                for row in self._memory.plex_videos
                if (video := _plex_track(row)) and self._wanted(video)
            )
        # Playlists keep only songs from the chosen libraries; empty ones are left out.
        included = {track.rating_key for track in tracks}
        playlists = [
            (title, kept)
            for title, members in playlists
            if (kept := tuple(t for t in members if t.rating_key in included))
        ]
        folder = self.plex_folder
        owned = load_owned(folder)
        thumbs: dict[str, tuple[str, Callable[[Path], None]]] = {}
        for track in tracks:
            if track.album_thumb:
                directory = track_relative(track).rsplit("/", 1)[0]
                thumbs.setdefault(
                    directory,
                    (
                        f"cover|{track.album_thumb}",
                        _plex_cover(plex, server, track.album_thumb),
                    ),
                )
        for video in videos:
            if video.thumb:
                thumbs.setdefault(
                    video_relative(video).rsplit("/", 1)[0],
                    (f"cover|{video.thumb}", _plex_cover(plex, server, video.thumb)),
                )
        covers = self._covers(folder, owned, thumbs, cancelled, progress, "Plex")
        items: list[RemoteItem] = []
        for track in tracks:
            relative = track_relative(track)
            spec = _Spec(
                f"plex:{server.id}:{track.rating_key}",
                folder,
                relative,
                f"{track.part_key}|{track.part_size}|{track.updated_at}",
                "",
                _plex_fetch(plex, server, track, self._mappings),
            )
            items.append(
                self._item(
                    spec,
                    owned,
                    track.part_size,
                    covers.get(relative.rsplit("/", 1)[0]),
                    title=track.title,
                    artist=track.artist,
                    album=track.album,
                    album_artist=track.album_artist,
                    track_number=track.track_number,
                    disc_number=track.disc_number,
                    length_ms=track.duration_ms,
                    genre=track.genre,
                    year=track.year,
                )
            )
        for video in videos:
            relative = video_relative(video)
            spec = _Spec(
                f"plex:{server.id}:{video.rating_key}",
                folder,
                relative,
                f"{video.part_key}|{video.part_size}|{video.updated_at}",
                "",
                _plex_fetch(plex, server, video, self._mappings),
            )
            episode = video.media == PlexVideoUse.TV_SHOWS.value
            items.append(
                self._item(
                    spec,
                    owned,
                    video.part_size,
                    covers.get(relative.rsplit("/", 1)[0]),
                    title=video.title,
                    artist=video.artist,
                    album=video.album,
                    album_artist=video.album_artist,
                    track_number=video.track_number if episode else 0,
                    disc_number=video.disc_number if episode else 0,
                    length_ms=video.duration_ms,
                    genre=video.genre,
                    year=video.year,
                    media_type=_VIDEO_MEDIA[video.media],
                    show=video.show if episode else "",
                    season_number=video.disc_number if episode else 0,
                    episode_number=video.track_number if episode else 0,
                    description=video.summary,
                )
            )
        lists: list[RemotePlaylist] = []
        used: set[str] = set()
        for title, members in playlists:
            name = title or "Plex Playlist"
            stem, counter = name, 2
            while stem.casefold() in used:
                stem, counter = f"{name} ({counter})", counter + 1
            used.add(stem.casefold())
            # The same identity a Plex playlist pick had, so iPod copies stay linked.
            lists.append(
                RemotePlaylist(
                    HostPath(folder / relative_path(PLAYLIST_FOLDER, f"{stem}.m3u8")),
                    stem,
                    tuple(HostPath(folder / track_relative(t)) for t in members),
                )
            )
        songs = len(items) - len(videos)
        progress(
            f"Plex: {songs} songs, {len(videos)} videos, {len(lists)} playlists."
            if videos
            else f"Plex: {songs} songs, {len(lists)} playlists."
        )
        return items, lists, issues

    def _grimmory_catalog(
        self, cancelled: Event, progress: Callable[[str], None]
    ) -> tuple[list[RemoteItem], list[str]]:
        client = self._grimmory
        assert client is not None
        issues: list[str] = []
        progress("Grimmory: listing your audiobooks…")
        try:
            books = client.audiobooks(cancelled=cancelled)
            self._memory.books = [asdict(book) for book in books]
        except CancelledError:
            raise
        except ConnectedError as error:
            if not self._memory.books:
                return [], [f"Grimmory: {error}"]
            issues.append(f"Grimmory: {error} Showing your audiobooks as last listed.")
            books = ()
        from .grimmory import Audiobook

        if not books:
            books = tuple(
                Audiobook(**cast("dict[str, Any]", row)) for row in self._memory.books
            )
        infos: dict[str, AudiobookInfo] = {}
        for index, book in enumerate(books, start=1):
            if cancelled.is_set():
                raise CancelledError
            known = self._memory.infos.get(book.book_id)
            info = _info(known) if known else None
            if info is None:
                if index % 10 == 1:
                    progress(f"Grimmory: reading audiobook {index} of {len(books)}…")
                try:
                    info = client.info(book.book_id)
                except CancelledError:
                    raise
                except ConnectedError as error:
                    issues.append(f"{book.title}: {error}")
                    continue
                self._memory.infos[book.book_id] = asdict(info)
            infos[book.book_id] = info
        folder = self.audiobooks_folder
        owned = load_owned(folder)
        from .config import GrimmoryPick

        directories = {
            book.book_id: book_folder(
                GrimmoryPick(book.book_id, book.title, book.author)
            )
            for book in books
        }
        wanted = {
            directories[book.book_id]: (
                f"cover|grimmory:{book.book_id}",
                _grimmory_cover(client, book.book_id),
            )
            for book in books
            if book.book_id in infos
        }
        covers = self._covers(folder, owned, wanted, cancelled, progress, "Grimmory")
        items: list[RemoteItem] = []
        for book in books:
            info = infos.get(book.book_id)
            if info is None:
                continue
            directory = directories[book.book_id]
            total = info.duration_ms or sum(t.duration_ms for t in info.tracks)
            parts: list[tuple[AudiobookTrack | None, str, int, int, int, str]] = []
            part: AudiobookTrack | None
            if info.folder_based and info.tracks:
                width = max(2, len(str(len(info.tracks))))
                for track in info.tracks:
                    extension = _extension(track.file_name, "", ".mp3")
                    stem = safe_component(
                        f"{track.index + 1:0{width}d} "
                        f"{track.title or track.file_name.rsplit('.', 1)[0]}",
                        f"{track.index + 1:0{width}d}",
                        limit=100,
                    )
                    parts.append(
                        (
                            track,
                            f"{stem}{_final(extension)}",
                            track.index,
                            track.start_ms,
                            track.duration_ms,
                            track.title,
                        )
                    )
            else:
                extension = _extension(info.file_name, "")
                name = safe_component(book.title or info.title, "Audiobook", limit=100)
                parts.append((None, f"{name}{_final(extension)}", 0, 0, total, ""))
            for part, name, index, start, duration, title in parts:
                link = GrimmoryLink(
                    book.book_id, info.book_file_id, index, start, total
                )
                spec = _Spec(
                    f"grimmory:{book.book_id}:{index}",
                    folder,
                    relative_path(directory, name),
                    f"grimmory:{book.book_id}:{info.book_file_id}:{index}:{duration}",
                    f"grimmory-native:{book.book_id}",
                    _grimmory_fetch(client, info, part, link, name),
                )
                items.append(
                    self._item(
                        spec,
                        owned,
                        duration * _BYTES_PER_MS,
                        covers.get(directory),
                        title=title or book.title or info.title,
                        artist=book.author,
                        album=book.title or info.title,
                        album_artist=book.author,
                        track_number=index + 1 if len(parts) > 1 else 0,
                        total_tracks=len(parts) if len(parts) > 1 else 0,
                        length_ms=duration,
                        media_type=MediaType.AUDIOBOOK,
                        comment=link.comment,
                    )
                )
        progress(f"Grimmory: {len(books)} audiobooks.")
        return items, issues

    def save_memory(self) -> None:
        try:
            self._memory_path.parent.mkdir(parents=True, exist_ok=True)
            from .materialize import write_atomic

            write_atomic(
                self._memory_path, json.dumps(asdict(self._memory)).encode("utf-8")
            )
        except OSError:
            pass  # only an optimization and an offline fallback

    # --- downloading during Sync ------------------------------------------------------

    def materialize(
        self,
        source: HostMediaSource,
        checkpoint: Callable[[], None],
        progress: Callable[[MediaPreparationProgress], None],
    ) -> HostMediaSource:
        spec = self._specs.get(host_path_identity(str(source.path)))
        if spec is None:
            raise ValueError(
                f"{source.path.path.name} is no longer in Plex or Grimmory. Run Sync again."
            )
        target = owned_target(spec.folder, spec.relative)
        with self._slots:
            checkpoint()
            with self._lock:
                owned = load_owned(spec.folder)
                stat = self._cached(owned, spec, target)
                if stat is not None:
                    entry = owned[spec.relative]
                    owned[spec.relative] = Owned(
                        entry.revision, entry.size, entry.bundle, _now()
                    )
                    save_owned(spec.folder, owned)
            if stat is None:
                progress(
                    MediaPreparationProgress(
                        MediaPreparationPhase.READING, f"Downloading {target.name}"
                    )
                )
                spec.folder.mkdir(parents=True, exist_ok=True)
                staging = Path(tempfile.mkdtemp(dir=spec.folder, prefix=".download-"))
                try:
                    try:
                        produced = spec.fetch(staging, _CheckpointEvent(checkpoint))
                    except CancelledError:
                        checkpoint()
                        raise
                    except ConnectedError as error:
                        raise ValueError(str(error)) from None
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(produced, target)
                finally:
                    shutil.rmtree(staging, ignore_errors=True)
                # A stable time keeps this copy's facts equal to the listing's.
                os.utime(target, ns=(time.time_ns(), revision_time_ns(spec.revision)))
                stat = target.stat()
                with self._lock:
                    owned = load_owned(spec.folder)
                    owned[spec.relative] = Owned(
                        spec.revision, stat.st_size, used=_now()
                    )
                    save_owned(spec.folder, owned)
                    self._memory.sizes[spec.key] = stat.st_size
                    self.downloaded += 1
        return HostMediaSource(
            HostPath(target),
            source.kind,
            stat.st_size,
            stat.st_mtime_ns,
            acoustic_fingerprint=source.acoustic_fingerprint,
            remote=True,
        )

    # --- the cache ---------------------------------------------------------------------

    def trim(self, *, keep: bool, limit_bytes: int) -> tuple[int, int]:
        """Apply the cache setting; returns ``(files removed, bytes kept)``."""

        removed = kept_bytes = 0
        for folder in (self.plex_folder, self.audiobooks_folder):
            if not folder.is_dir():
                continue
            with self._lock:
                owned = load_owned(folder)
                media = sorted(
                    (
                        (entry.used, key)
                        for key, entry in owned.items()
                        if not key.endswith(f"/{_COVER}") and key != _COVER
                    ),
                )
                total = sum(owned[key].size for _used, key in media)
                for _used, key in media:
                    if keep and total <= limit_bytes:
                        break
                    entry = owned[key]
                    target = owned_target(folder, key)
                    try:
                        if target.stat().st_size == entry.size:
                            target.unlink()
                            removed += 1
                        elif target.exists():
                            continue  # changed by someone else: leave it, still owned
                    except FileNotFoundError:
                        pass
                    except OSError:
                        continue
                    total -= entry.size
                    del owned[key]
                    prune_empty(folder, target.parent)
                save_owned(folder, owned)
                kept_bytes += total
        self.save_memory()
        return removed, kept_bytes


_VIDEO_MEDIA = {
    PlexVideoUse.MOVIES.value: MediaType.VIDEO,
    PlexVideoUse.TV_SHOWS.value: MediaType.TV_SHOW,
    PlexVideoUse.MUSIC_VIDEOS.value: MediaType.MUSIC_VIDEO,
}
VIDEO_FOLDERS = {
    PlexVideoUse.MOVIES.value: "Movies",
    PlexVideoUse.TV_SHOWS.value: "TV Shows",
    PlexVideoUse.MUSIC_VIDEOS.value: "Music Videos",
}


def video_relative(video: PlexTrack) -> str:
    """Where a Plex video is cached; each movie and music video gets a folder
    (for its poster), and episodes share their season's folder."""

    key = f"[{video.rating_key}]"
    if video.media == PlexVideoUse.TV_SHOWS.value:
        show = video.show or "Unknown Show"
        number = (
            f"S{video.disc_number:02d}E{video.track_number:02d} - "
            if video.track_number
            else ""
        )
        return relative_path(
            VIDEO_FOLDERS[video.media],
            show,
            f"Season {video.disc_number:02d}",
            f"{number}{video.title} {key}{video.extension}",
        )
    name = video.title
    if video.media == PlexVideoUse.MUSIC_VIDEOS.value and video.artist:
        name = f"{video.artist} - {video.title}"
    elif video.year:
        name = f"{video.title} ({video.year})"
    return relative_path(
        VIDEO_FOLDERS.get(video.media, "Videos"),
        f"{name} {key}",
        f"{name}{video.extension}",
    )


def _final(extension: str) -> str:
    """The extension a downloaded audiobook file ends up with once tagged."""

    return ".m4b" if extension in {".m4a", ".m4b", ".mp4"} else extension


def _plex_fetch(
    client: PlexClient,
    server: PlexServer,
    track: PlexTrack,
    mappings: tuple[PathMapping, ...],
) -> Callable[[Path, Event], Path]:
    def fetch(staging: Path, cancelled: Event) -> Path:
        destination = staging / f"track{track.extension}"
        client.fetch_track(
            server, track, destination, mappings=mappings, cancelled=cancelled
        )
        return destination

    return fetch


def _plex_cover(
    client: PlexClient, server: PlexServer, thumb: str
) -> Callable[[Path], None]:
    def fetch(destination: Path) -> None:
        client.fetch_artwork(server, thumb, destination)

    return fetch


def _grimmory_cover(client: GrimmoryClient, book_id: str) -> Callable[[Path], None]:
    def fetch(destination: Path) -> None:
        destination.write_bytes(client.cover(book_id))

    return fetch


def _grimmory_fetch(
    client: GrimmoryClient,
    info: AudiobookInfo,
    track: AudiobookTrack | None,
    link: GrimmoryLink,
    name: str,
) -> Callable[[Path, Event], Path]:
    def fetch(staging: Path, cancelled: Event) -> Path:
        part = staging / ".part"
        if track is not None:
            content_type = client.download_track(info, track, part, cancelled)
            extension = _extension(track.file_name, content_type, ".mp3")
        else:
            content_type = client.download_book(info, part, cancelled)
            extension = _extension(info.file_name, content_type)
        downloaded = staging / f"download{extension}"
        part.replace(downloaded)
        tagged = tag_audiobook(downloaded, link)
        if tagged.suffix.casefold() != Path(name).suffix.casefold():
            raise ConnectedError(
                f"Grimmory sent a {tagged.suffix} file where {Path(name).suffix} was listed. Run Sync again."
            )
        return tagged

    return fetch


__all__ = ["AUDIOBOOKS_FOLDER", "PLEX_FOLDER", "ConnectedRemote"]
