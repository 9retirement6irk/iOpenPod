"""Turn a Plex Selection into the files of the Plex Connected Source Folder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import PlexConfig, PlexPickKind
from .http import CancelledError
from .materialize import DesiredBytes, DesiredDownload, m3u8_playlist, relative_path

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from threading import Event

    from .plex import PlexClient, PlexServer, PlexTrack

PLAYLIST_FOLDER = "Playlists"


@dataclass(frozen=True, slots=True)
class PlexPlan:
    items: tuple[DesiredDownload | DesiredBytes, ...]
    tracks: int
    playlists: int
    skipped: tuple[str, ...]


def track_relative(track: PlexTrack) -> str:
    artist = track.album_artist or track.artist or "Unknown Artist"
    album = track.album or "Unknown Album"
    number = (
        f"{track.disc_number}-{track.track_number:02d} "
        if track.disc_number > 1 and track.track_number
        else f"{track.track_number:02d} "
        if track.track_number
        else ""
    )
    name = f"{number}{track.title}"
    # The rating key keeps two same-named Tracks on one album apart.
    return relative_path(artist, album, f"{name} [{track.rating_key}]{track.extension}")


def plan_plex(
    client: PlexClient,
    server: PlexServer,
    config: PlexConfig,
    *,
    cancelled: Event,
    progress: Callable[[str], None] = lambda _text: None,
) -> PlexPlan:
    tracks: dict[str, PlexTrack] = {}
    playlists: list[tuple[str, tuple[PlexTrack, ...]]] = []
    skipped: list[str] = []
    for index, pick in enumerate(config.picks, start=1):
        if cancelled.is_set():
            raise CancelledError
        progress(f"Reading Plex selection {index} of {len(config.picks)}: {pick.title}")
        expanded = client.expand(server, pick, cancelled)
        if not expanded:
            skipped.append(f"{pick.title}: no playable music found")
        for track in expanded:
            tracks.setdefault(track.rating_key, track)
        if pick.kind is PlexPickKind.PLAYLIST:
            playlists.append((pick.title, expanded))
    items: list[DesiredDownload | DesiredBytes] = [
        DesiredDownload(
            track_relative(track),
            f"{track.part_key}|{track.part_size}|{track.updated_at}",
            _fetcher(client, server, track, config, cancelled),
            track.part_size,
        )
        for track in tracks.values()
    ]
    # iOpenPod reads a folder's cover.jpg as album artwork; Plex often keeps
    # artwork in its own database rather than inside the files.
    covers: dict[str, str] = {}
    for track in tracks.values():
        if track.album_thumb:
            folder = track_relative(track).rsplit("/", 1)[0]
            covers.setdefault(folder, track.album_thumb)
    items.extend(
        DesiredDownload(
            f"{folder}/cover.jpg", f"cover|{thumb}", _cover(client, server, thumb)
        )
        for folder, thumb in covers.items()
    )
    used_names: set[str] = set()
    for title, members in playlists:
        name = title or "Plex Playlist"
        stem, counter = name, 2
        while stem.casefold() in used_names:
            stem, counter = f"{name} ({counter})", counter + 1
        used_names.add(stem.casefold())
        entries = [
            (f"../{track_relative(track)}", track.duration_ms // 1000, track.title)
            for track in members
        ]
        items.append(
            DesiredBytes(
                relative_path(PLAYLIST_FOLDER, f"{stem}.m3u8"),
                m3u8_playlist(stem, entries),
            )
        )
    return PlexPlan(tuple(items), len(tracks), len(playlists), tuple(skipped))


def _cover(
    client: PlexClient, server: PlexServer, thumb: str
) -> Callable[[Path], None]:
    def fetch(destination: Path) -> None:
        client.fetch_artwork(server, thumb, destination)

    return fetch


def _fetcher(
    client: PlexClient,
    server: PlexServer,
    track: PlexTrack,
    config: PlexConfig,
    cancelled: Event,
) -> Callable[[Path], None]:
    def fetch(destination: Path) -> None:
        client.fetch_track(
            server, track, destination, mappings=config.mappings, cancelled=cancelled
        )

    return fetch


__all__ = ["PLAYLIST_FOLDER", "PlexPlan", "plan_plex", "track_relative"]
