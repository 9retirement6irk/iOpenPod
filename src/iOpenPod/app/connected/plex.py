"""Plex Media Server access for the Plex Connected Source.

Covers the plex.tv PIN sign-in, server selection, music browsing, expanding a
Selection into Tracks, fetching original media, ratings, and reporting a play as
the iPod that recorded it.
"""

from __future__ import annotations

import contextlib
import platform
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, cast
from urllib.parse import quote, urlencode

from iOpenPod.app.core.version import get_version

from .config import PathMapping, PlexPick, PlexPickKind
from .http import (
    MAX_JSON_BYTES,
    ConnectedError,
    Transport,
    UrllibTransport,
    json_body,
    normalize_base_url,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from threading import Event

PLEX_TV = "https://plex.tv"
CLIENTS_PLEX_TV = "https://clients.plex.tv"
LIBRARY_ID = "com.plexapp.plugins.library"
PIN_LIFETIME = 900
_PAGE = 500
PRODUCT = "iOpenPod"


def _object(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        _object(item) for item in cast("list[object]", value) if isinstance(item, dict)
    ]


def _str(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(int(value)) if float(value).is_integer() else str(value)
    return ""


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _container(value: object) -> dict[str, object]:
    return _object(_object(value).get("MediaContainer"))


@dataclass(frozen=True, slots=True)
class PlexPin:
    id: int
    code: str
    auth_url: str
    created: float


@dataclass(frozen=True, slots=True)
class PlexServerChoice:
    id: str
    name: str
    owned: bool
    access_token: str
    candidates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlexServer:
    """A reachable server: base URL, its token, and identity."""

    id: str
    name: str
    uri: str
    token: str


@dataclass(frozen=True, slots=True)
class PlexItem:
    """One browsable Plex record for the picker."""

    kind: PlexPickKind
    key: str
    title: str
    subtitle: str = ""
    count: int = 0
    thumb: str = ""


@dataclass(frozen=True, slots=True)
class PlexTrack:
    rating_key: str
    title: str
    artist: str
    album_artist: str
    album: str
    track_number: int
    disc_number: int
    duration_ms: int
    part_key: str
    part_file: str
    part_size: int
    container: str
    updated_at: int
    user_rating: float | None = None
    view_count: int = 0
    album_thumb: str = ""

    @property
    def extension(self) -> str:
        for candidate in (PurePosixPath(self.part_file).suffix, f".{self.container}"):
            text = candidate.casefold()
            if 1 < len(text) <= 6 and text[1:].isalnum():
                return text
        return ".m4a"


@dataclass(slots=True)
class _PinState:
    pin: PlexPin | None = None
    known: dict[str, PlexServerChoice] = field(
        default_factory=dict[str, PlexServerChoice]
    )


def client_identifier(seed: str) -> str:
    return f"iopenpod-{uuid.uuid5(uuid.NAMESPACE_URL, 'iopenpod-plex:' + seed)}"


class PlexClient:
    def __init__(
        self,
        client_id: str,
        transport: Transport | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client_id = client_id
        self._transport = transport or UrllibTransport()
        self._clock = clock
        self._state = _PinState()

    # --- headers / requests --------------------------------------------------------

    def headers(self, token: str = "", **extra: str) -> dict[str, str]:
        system = platform.system() or "Computer"
        values = {
            "Accept": "application/json",
            "X-Plex-Product": PRODUCT,
            "X-Plex-Version": get_version(),
            "X-Plex-Client-Identifier": self.client_id,
            "X-Plex-Platform": system,
            "X-Plex-Device": system,
            "X-Plex-Device-Name": f"{PRODUCT} ({platform.node() or 'computer'})",
        }
        if token:
            values["X-Plex-Token"] = token
        values.update(extra)
        return values

    def _json(
        self,
        method: str,
        url: str,
        token: str = "",
        *,
        timeout: float = 30,
        headers: Mapping[str, str] | None = None,
    ) -> object:
        try:
            response = self._transport.send(
                method,
                url,
                {**self.headers(token), **(headers or {})},
                b"" if method in {"POST", "PUT"} else None,
                timeout=timeout,
                limit=MAX_JSON_BYTES,
            )
        except ConnectedError as error:
            if error.status == 401:
                raise ConnectedError(
                    "Plex didn't accept the sign-in. Sign out of Plex and sign in again.",
                    401,
                ) from None
            raise
        return json_body(response)

    # --- sign-in -------------------------------------------------------------------

    def start_sign_in(self) -> PlexPin:
        data = _object(self._json("POST", f"{PLEX_TV}/api/v2/pins?strong=true"))
        pin_id, code = _int(data.get("id")), _str(data.get("code"))
        if not pin_id or not code:
            raise ConnectedError("Plex did not start a sign-in. Try again.")
        query = urlencode(
            {
                "clientID": self.client_id,
                "code": code,
                "context[device][product]": PRODUCT,
            }
        )
        pin = PlexPin(pin_id, code, f"https://app.plex.tv/auth#?{query}", self._clock())
        self._state.pin = pin
        return pin

    def poll_sign_in(self, pin: PlexPin) -> tuple[str, str] | None:
        """``(token, username)`` once approved, ``None`` while waiting."""

        if self._clock() - pin.created > PIN_LIFETIME:
            raise ConnectedError("The Plex sign-in expired. Start again.")
        data = _object(self._json("GET", f"{PLEX_TV}/api/v2/pins/{pin.id}"))
        token = _str(data.get("authToken"))
        if not token:
            return None
        user = _object(self._json("GET", f"{PLEX_TV}/api/v2/user", token))
        name = (
            _str(user.get("title"))
            or _str(user.get("username"))
            or _str(user.get("email"))
            or "Plex user"
        )
        return token, name

    # --- servers -------------------------------------------------------------------

    def servers(self, account_token: str) -> tuple[PlexServerChoice, ...]:
        raw = self._json(
            "GET",
            f"{CLIENTS_PLEX_TV}/api/v2/resources?includeHttps=1&includeRelay=1&includeIPv6=1",
            account_token,
        )
        choices: list[PlexServerChoice] = []
        for resource in _objects(raw):
            if "server" not in _str(resource.get("provides")):
                continue
            ranked: list[tuple[int, str]] = []
            for connection in _objects(resource.get("connections")):
                relay, local = (
                    bool(connection.get("relay")),
                    bool(connection.get("local")),
                )
                rank = 3 if relay else 0 if local else 2
                uri = _str(connection.get("uri"))
                if uri:
                    ranked.append((rank, uri))
                address, port = (
                    _str(connection.get("address")),
                    _int(connection.get("port")),
                )
                if local and address and port and ":" not in address:
                    ranked.append((rank + 1, f"http://{address}:{port}"))
            ordered = tuple(dict.fromkeys(uri for _rank, uri in sorted(ranked)))
            identifier = _str(resource.get("clientIdentifier"))
            if identifier and ordered:
                choices.append(
                    PlexServerChoice(
                        identifier,
                        _str(resource.get("name")) or "Plex Media Server",
                        bool(resource.get("owned")),
                        _str(resource.get("accessToken")) or account_token,
                        ordered,
                    )
                )
        self._state.known = {choice.id: choice for choice in choices}
        return tuple(choices)

    def connect(self, choice: PlexServerChoice) -> PlexServer:
        def reachable(uri: str) -> bool:
            try:
                self._json("GET", f"{uri}/identity", choice.access_token, timeout=6)
            except ConnectedError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reachable, choice.candidates))
        for uri, ok in zip(choice.candidates, results, strict=True):
            if ok:
                return PlexServer(choice.id, choice.name, uri, choice.access_token)
        raise ConnectedError(
            f"Could not reach {choice.name} on any of its addresses. Is it running?"
        )

    def connect_manual(self, address: str, token: str) -> PlexServer:
        uri = normalize_base_url(address)
        identity = _container(self._json("GET", f"{uri}/identity", token, timeout=10))
        machine = _str(identity.get("machineIdentifier")) or uri
        return PlexServer(machine, uri, uri, token)

    # --- library -------------------------------------------------------------------

    def _get(
        self, server: PlexServer, path: str, *, timeout: float = 60
    ) -> dict[str, object]:
        return _container(
            self._json("GET", f"{server.uri}{path}", server.token, timeout=timeout)
        )

    def _paged(
        self, server: PlexServer, path: str, cancelled: Event | None = None
    ) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        separator = "&" if "?" in path else "?"
        start = 0
        while True:
            if cancelled is not None and cancelled.is_set():
                raise ConnectedError("Cancelled.")
            page = self._get(
                server,
                f"{path}{separator}X-Plex-Container-Start={start}&X-Plex-Container-Size={_PAGE}",
                timeout=120,
            )
            batch = _objects(page.get("Metadata"))
            items.extend(batch)
            start += len(batch)
            total = page.get("totalSize")
            if not batch or len(batch) < _PAGE or len(batch) > _PAGE:
                break
            if total is not None and start >= _int(total):
                break
        return items

    def music_sections(self, server: PlexServer) -> tuple[tuple[str, str], ...]:
        sections = _objects(self._get(server, "/library/sections").get("Directory"))
        return tuple(
            (_str(item.get("key")), _str(item.get("title")) or "Music")
            for item in sections
            if item.get("type") == "artist" and _str(item.get("key"))
        )

    def playlists(self, server: PlexServer) -> tuple[PlexItem, ...]:
        rows = _objects(
            self._get(server, "/playlists?playlistType=audio").get("Metadata")
        )
        items = [
            PlexItem(
                PlexPickKind.PLAYLIST,
                _str(row.get("ratingKey")),
                _str(row.get("title")) or "Untitled playlist",
                "Smart playlist" if row.get("smart") else "Playlist",
                _int(row.get("leafCount")),
                _str(row.get("composite")) or _str(row.get("thumb")),
            )
            for row in rows
            if _str(row.get("ratingKey")) and row.get("playlistType") in (None, "audio")
        ]
        return tuple(sorted(items, key=lambda item: item.title.casefold()))

    def artists(self, server: PlexServer, section: str) -> tuple[PlexItem, ...]:
        rows = self._paged(server, f"/library/sections/{quote(section)}/all?type=8")
        return tuple(
            PlexItem(
                PlexPickKind.ARTIST,
                _str(row.get("ratingKey")),
                _str(row.get("title")) or "Unknown artist",
                "Artist",
                _int(row.get("childCount")),
                _str(row.get("thumb")),
            )
            for row in rows
            if _str(row.get("ratingKey"))
        )

    def albums(self, server: PlexServer, artist_key: str) -> tuple[PlexItem, ...]:
        rows = _objects(
            self._get(server, f"/library/metadata/{quote(artist_key)}/children").get(
                "Metadata"
            )
        )
        return tuple(
            PlexItem(
                PlexPickKind.ALBUM,
                _str(row.get("ratingKey")),
                _str(row.get("title")) or "Unknown album",
                " · ".join(
                    part
                    for part in (_str(row.get("parentTitle")), _str(row.get("year")))
                    if part
                ),
                _int(row.get("leafCount")),
                _str(row.get("thumb")),
            )
            for row in rows
            if _str(row.get("ratingKey"))
        )

    def album_tracks(self, server: PlexServer, album_key: str) -> tuple[PlexTrack, ...]:
        rows = _objects(
            self._get(server, f"/library/metadata/{quote(album_key)}/children").get(
                "Metadata"
            )
        )
        return tuple(track for row in rows if (track := parse_track(row)) is not None)

    def search(
        self, server: PlexServer, section: str, query: str
    ) -> tuple[PlexItem, ...]:
        """Artists, albums and Tracks whose names contain ``query``."""

        text = query.strip()
        if not text:
            return ()
        found: list[PlexItem] = []
        for kind, type_id in (
            (PlexPickKind.ARTIST, 8),
            (PlexPickKind.ALBUM, 9),
            (PlexPickKind.TRACK, 10),
        ):
            rows = _objects(
                self._get(
                    server,
                    f"/library/sections/{quote(section)}/all?type={type_id}"
                    f"&title={quote(text)}&X-Plex-Container-Start=0&X-Plex-Container-Size=60",
                ).get("Metadata")
            )
            for row in rows:
                key = _str(row.get("ratingKey"))
                if not key:
                    continue
                subtitle = {
                    PlexPickKind.ARTIST: "Artist",
                    PlexPickKind.ALBUM: _str(row.get("parentTitle")),
                    PlexPickKind.TRACK: " · ".join(
                        part
                        for part in (
                            _str(row.get("originalTitle"))
                            or _str(row.get("grandparentTitle")),
                            _str(row.get("parentTitle")),
                        )
                        if part
                    ),
                }.get(kind, "")
                found.append(
                    PlexItem(
                        kind,
                        key,
                        _str(row.get("title")) or "Untitled",
                        subtitle,
                        _int(row.get("leafCount")),
                        _str(row.get("thumb")),
                    )
                )
        return tuple(found)

    def expand(
        self, server: PlexServer, pick: PlexPick, cancelled: Event | None = None
    ) -> tuple[PlexTrack, ...]:
        """All Tracks a Selection entry stands for, in Plex's order."""

        key = quote(pick.key)
        if pick.kind is PlexPickKind.PLAYLIST:
            rows = self._paged(server, f"/playlists/{key}/items", cancelled)
        elif pick.kind is PlexPickKind.ARTIST:
            rows = self._paged(server, f"/library/metadata/{key}/allLeaves", cancelled)
        elif pick.kind is PlexPickKind.ALBUM:
            rows = _objects(
                self._get(server, f"/library/metadata/{key}/children").get("Metadata")
            )
        else:
            rows = _objects(
                self._get(server, f"/library/metadata/{key}").get("Metadata")
            )
        return tuple(
            track
            for row in rows
            if row.get("type") in (None, "track")
            and (track := parse_track(row)) is not None
        )

    def library_tracks(
        self, server: PlexServer, cancelled: Event | None = None
    ) -> tuple[PlexTrack, ...]:
        tracks: list[PlexTrack] = []
        for section, _title in self.music_sections(server):
            rows = self._paged(
                server, f"/library/sections/{quote(section)}/all?type=10", cancelled
            )
            tracks.extend(
                track for row in rows if (track := parse_track(row)) is not None
            )
        return tuple(tracks)

    # --- media -------------------------------------------------------------------

    def fetch_track(
        self,
        server: PlexServer,
        track: PlexTrack,
        destination: Path,
        *,
        mappings: Iterable[PathMapping] = (),
        cancelled: Event | None = None,
    ) -> None:
        """Copy the original file through a path mapping when reachable, else download."""

        local = map_server_path(track.part_file, mappings)
        if (
            local is not None
            and local.is_file()
            and track.part_size
            and local.stat().st_size != track.part_size
        ):
            local = None
        if local is not None and not local.is_file():
            local = None
        if local is not None:
            temporary = destination.with_name(f".copy-{destination.name}.part")
            try:
                shutil.copyfile(local, temporary)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            return
        separator = "&" if "?" in track.part_key else "?"
        self._transport.download(
            f"{server.uri}{track.part_key}{separator}download=1",
            {**self.headers(server.token), "Accept": "*/*"},
            destination,
            cancelled=cancelled,
        )

    def thumbnail(self, server: PlexServer, thumb: str, size: int = 96) -> bytes:
        """A small JPEG of Plex artwork for the picker."""

        return self._transport.send(
            "GET",
            f"{server.uri}/photo/:/transcode?"
            + urlencode(
                {"url": thumb, "width": size, "height": size, "format": "jpeg"}
            ),
            {**self.headers(server.token), "Accept": "image/jpeg"},
            limit=2 * 1024 * 1024,
        ).body

    def fetch_artwork(self, server: PlexServer, thumb: str, destination: Path) -> None:
        """Save album artwork (Plex often keeps it outside the media files)."""

        self._transport.download(
            f"{server.uri}/photo/:/transcode?"
            + urlencode({"url": thumb, "width": 600, "height": 600, "format": "jpeg"}),
            {**self.headers(server.token), "Accept": "image/jpeg"},
            destination,
        )

    def rate(self, server: PlexServer, rating_key: str, rating: float) -> None:
        """Set a 0-10 Plex rating (stars x 2). ``-1`` clears it."""

        self._json(
            "PUT",
            f"{server.uri}/:/rate?identifier={LIBRARY_ID}"
            f"&key={quote(rating_key)}&rating={rating:g}",
            server.token,
        )

    # --- plays ---------------------------------------------------------------------

    def record_play(
        self,
        server: PlexServer,
        track: PlexTrack,
        player: Mapping[str, str],
        *,
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        """Report one complete listen the way a Plex player does.

        Create a play queue, then report ``playing`` timeline positions to the end
        while reading the matching slices of the file, then ``stopped``. Plex counts
        a finished play and shows the iPod as the player in Play History. If the
        server refuses timeline reports, fall back to a plain scrobble.
        """

        headers = {**player, "X-Plex-Session-Identifier": uuid.uuid4().hex}
        duration = track.duration_ms or 180_000
        base = (
            f"{server.uri}/:/timeline?ratingKey={quote(track.rating_key)}"
            f"&key=%2Flibrary%2Fmetadata%2F{quote(track.rating_key)}"
            f"&identifier={LIBRARY_ID}&duration={duration}&hasMDE=1"
        )
        try:
            queue = _container(
                self._json(
                    "POST",
                    f"{server.uri}/playQueues?"
                    + urlencode(
                        {
                            "type": "audio",
                            "uri": f"server://{server.id}/{LIBRARY_ID}/library/metadata/{track.rating_key}",
                            "shuffle": 0,
                            "repeat": 0,
                            "continuous": 0,
                            "own": 1,
                        }
                    ),
                    server.token,
                    headers=headers,
                )
            )
            queue_id = _str(queue.get("playQueueID"))
            item_id = _str(queue.get("playQueueSelectedItemID"))
            if queue_id:
                base += f"&containerKey=%2FplayQueues%2F{queue_id}&playQueueItemID={item_id}"
        except ConnectedError:
            pass

        def timeline(state: str, position: float) -> None:
            self._json(
                "GET",
                f"{base}&state={state}&time={int(position)}",
                server.token,
                timeout=20,
                headers=headers,
            )

        try:
            for fraction in (0.0, 0.25, 0.5, 0.75, 0.95):
                timeline("playing", duration * fraction)
                self._read_slice(server, track, headers, fraction)
                pause(0.15)
            timeline("playing", max(0, duration - 500))
            timeline("stopped", duration)
        except ConnectedError as error:
            if error.status not in (400, 404, 405, 501):
                raise
            self._json(
                "GET",
                f"{server.uri}/:/scrobble?identifier={LIBRARY_ID}&key={quote(track.rating_key)}",
                server.token,
            )

    def _read_slice(
        self,
        server: PlexServer,
        track: PlexTrack,
        headers: Mapping[str, str],
        fraction: float,
    ) -> None:
        if not track.part_key:
            return
        offset = int(track.part_size * fraction) if track.part_size else 0
        separator = "&" if "?" in track.part_key else "?"
        with contextlib.suppress(ConnectedError):
            self._transport.send(
                "GET",
                f"{server.uri}{track.part_key}{separator}download=0",
                {
                    **self.headers(server.token),
                    **headers,
                    "Accept": "*/*",
                    "Range": f"bytes={offset}-{offset + 65535}",
                },
                limit=1024 * 1024,
            )


def ipod_player(device_identity: str, device_name: str) -> dict[str, str]:
    """Headers that make Plex attribute plays to this iPod, platform "iPod"."""

    return {
        "X-Plex-Client-Identifier": client_identifier(f"ipod:{device_identity}"),
        "X-Plex-Device-Name": device_name or "iPod",
        "X-Plex-Platform": "iPod",
        "X-Plex-Platform-Version": "",
        "X-Plex-Device": "iPod",
        "X-Plex-Model": "iPod",
        "X-Plex-Product": "iPod",
        "X-Plex-Version": get_version(),
    }


def parse_track(row: Mapping[str, object]) -> PlexTrack | None:
    key = _str(row.get("ratingKey"))
    if not key:
        return None
    part: dict[str, object] = {}
    container = ""
    for media in _objects(row.get("Media")):
        parts = _objects(media.get("Part"))
        if parts:
            part = parts[0]
            container = _str(media.get("container"))
            break
    if not _str(part.get("key")):
        return None
    rating = row.get("userRating")
    return PlexTrack(
        rating_key=key,
        title=_str(row.get("title")) or "Untitled",
        artist=_str(row.get("originalTitle")) or _str(row.get("grandparentTitle")),
        album_artist=_str(row.get("grandparentTitle")),
        album=_str(row.get("parentTitle")),
        track_number=_int(row.get("index")),
        disc_number=_int(row.get("parentIndex")),
        duration_ms=_int(row.get("duration")) or _int(part.get("duration")),
        part_key=_str(part.get("key")),
        part_file=_str(part.get("file")),
        part_size=_int(part.get("size")),
        container=container or _str(part.get("container")),
        updated_at=_int(row.get("updatedAt")),
        user_rating=float(rating)
        if isinstance(rating, int | float) and not isinstance(rating, bool)
        else None,
        view_count=_int(row.get("viewCount")),
        album_thumb=_str(row.get("parentThumb")),
    )


def map_server_path(server_path: str, mappings: Iterable[PathMapping]) -> Path | None:
    """The Host path for a server file using the longest matching mapping."""

    if not server_path:
        return None
    normalized = server_path.replace("\\", "/")
    best: tuple[int, PathMapping] | None = None
    for mapping in mappings:
        prefix = mapping.server.replace("\\", "/").rstrip("/")
        if not prefix or not mapping.host.strip():
            continue
        matches = normalized == prefix or normalized.startswith(prefix + "/")
        if matches and (best is None or len(prefix) > best[0]):
            best = (len(prefix), mapping)
    if best is None:
        return None
    remainder = normalized[best[0] :].lstrip("/")
    host = best[1].host
    parts = PurePosixPath(remainder).parts
    if ".." in parts:
        return None
    if "\\" in host or (len(host) > 1 and host[1] == ":"):
        return Path(PureWindowsPath(host).joinpath(*parts))
    return Path(host).joinpath(*parts)


__all__ = [
    "PlexClient",
    "PlexItem",
    "PlexPin",
    "PlexServer",
    "PlexServerChoice",
    "PlexTrack",
    "client_identifier",
    "ipod_player",
    "map_server_path",
    "parse_track",
]
