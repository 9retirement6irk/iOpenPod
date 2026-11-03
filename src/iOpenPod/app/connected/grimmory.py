"""Grimmory audiobooks through Grimmory's own REST API.

Grimmory (a BookLore fork) signs a person in with ``POST /api/v1/auth/login`` and
returns a short-lived access token with a refresh token. iOpenPod keeps only those
tokens in the OS vault, never the password, and renews the access token when the
server rejects it. Audiobooks are listed through the app API
(``/api/v1/app/books?fileType=AUDIOBOOK``), described by
``/api/v1/audiobooks/{id}/info``, and downloaded as the original single file or as
each track of a folder-based audiobook.

Every downloaded file carries a Grimmory Link in its comment tag:
``grimmory:<book>:<book file>:<track index>:<track start ms>:<book duration ms>``.
The tag survives Sync into the iPod Library, so listening progress can be exchanged
with Grimmory later without guessing which iPod Track belongs to which book.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

from .http import (
    MAX_JSON_BYTES,
    CancelledError,
    ConnectedError,
    Transport,
    UrllibTransport,
    json_body,
    normalize_base_url,
)
from .materialize import DesiredBundle, relative_path, safe_component, write_atomic

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from threading import Event

    from .config import GrimmoryConfig, GrimmoryPick

_PAGE = 50
_MAX_PAGES = 400
AUDIO_EXTENSIONS = frozenset(
    {".m4b", ".m4a", ".mp3", ".aac", ".opus", ".ogg", ".flac", ".wav"}
)
_LINK = re.compile(r"grimmory:(\d+):(\d+):(\d+):(\d+):(\d+)")


def _object(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [_object(v) for v in cast("list[object]", value) if isinstance(v, dict)]


def _str(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _epoch(value: object) -> int:
    text = _str(value)
    if not text:
        return 0
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


@dataclass(frozen=True, slots=True)
class GrimmoryTokens:
    access: str
    refresh: str


@dataclass(frozen=True, slots=True)
class GrimmoryCollection:
    """A library or shelf to browse."""

    kind: str  # "library" | "shelf"
    id: str
    name: str
    count: int = 0


@dataclass(frozen=True, slots=True)
class Audiobook:
    book_id: str
    title: str
    author: str
    series: str = ""


@dataclass(frozen=True, slots=True)
class AudiobookTrack:
    index: int
    file_name: str
    title: str
    duration_ms: int
    start_ms: int


@dataclass(frozen=True, slots=True)
class AudiobookInfo:
    book_id: str
    book_file_id: str
    title: str
    duration_ms: int
    folder_based: bool
    tracks: tuple[AudiobookTrack, ...]
    file_name: str = ""


@dataclass(frozen=True, slots=True)
class GrimmoryProgress:
    position_ms: int
    track_index: int | None
    percentage: float
    updated: int  # Unix seconds; 0 when Grimmory has no progress


@dataclass(frozen=True, slots=True)
class GrimmoryLink:
    """Where one file sits inside a Grimmory audiobook."""

    book_id: str
    book_file_id: str
    track_index: int
    start_ms: int
    duration_ms: int

    @property
    def comment(self) -> str:
        return (
            f"grimmory:{self.book_id}:{self.book_file_id}:{self.track_index}"
            f":{self.start_ms}:{self.duration_ms}"
        )


def parse_link(comment: str) -> GrimmoryLink | None:
    match = _LINK.search(comment or "")
    if match is None:
        return None
    book, file, index, start, duration = match.groups()
    return GrimmoryLink(book, file, int(index), int(start), int(duration))


class GrimmoryClient:
    def __init__(
        self,
        url: str,
        tokens: GrimmoryTokens | None = None,
        transport: Transport | None = None,
        *,
        on_tokens: Callable[[GrimmoryTokens], None] = lambda _tokens: None,
    ) -> None:
        base = normalize_base_url(url)
        for suffix in ("/api/v1", "/api"):
            if base.casefold().endswith(suffix):
                base = base[: -len(suffix)]
                break
        self.base = base
        self.tokens = tokens
        self._transport = transport or UrllibTransport()
        self._on_tokens = on_tokens

    # --- authentication -------------------------------------------------------------

    def _tokens_from(self, data: dict[str, object]) -> GrimmoryTokens:
        access, refresh = _str(data.get("accessToken")), _str(data.get("refreshToken"))
        if not access or not refresh:
            raise ConnectedError("Grimmory did not return a sign-in.")
        self.tokens = GrimmoryTokens(access, refresh)
        self._on_tokens(self.tokens)
        return self.tokens

    @property
    def transport(self) -> Transport:
        return self._transport

    def accept_tokens(self, data: dict[str, object]) -> GrimmoryTokens:
        """Keep Grimmory tokens issued outside :meth:`sign_in`, such as by SSO."""

        return self._tokens_from(data)

    def public(self, path: str) -> object:
        """GET an endpoint that needs no sign-in."""

        return json_body(
            self._transport.send(
                "GET", f"{self.base}{path}", {"Accept": "application/json"}
            )
        )

    def sign_in(self, username: str, password: str) -> GrimmoryTokens:
        if not username.strip() or not password:
            raise ConnectedError("Enter your Grimmory username and password.")
        try:
            response = self._transport.send(
                "POST",
                f"{self.base}/api/v1/auth/login",
                {"Accept": "application/json", "Content-Type": "application/json"},
                json.dumps(
                    {"username": username.strip(), "password": password}
                ).encode(),
            )
        except ConnectedError as error:
            if error.status in (400, 401, 403):
                raise ConnectedError(
                    "Grimmory didn't accept that username and password. If you sign in to Grimmory through single sign-on, use Sign In with SSO instead.",
                    401,
                ) from None
            raise
        return self._tokens_from(_object(json_body(response)))

    def _refresh(self) -> None:
        if self.tokens is None:
            raise ConnectedError("Sign in to Grimmory first.", 401)
        try:
            response = self._transport.send(
                "POST",
                f"{self.base}/api/v1/auth/refresh",
                {"Accept": "application/json", "Content-Type": "application/json"},
                json.dumps({"refreshToken": self.tokens.refresh}).encode(),
            )
        except ConnectedError as error:
            if error.status in (400, 401, 403):
                raise ConnectedError(
                    "Your Grimmory sign-in expired. Sign in again.", 401
                ) from None
            raise
        self._tokens_from(_object(json_body(response)))

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        if self.tokens is None:
            raise ConnectedError("Sign in to Grimmory first.", 401)
        return {"Authorization": f"Bearer {self.tokens.access}", "Accept": accept}

    def _call(self, method: str, path: str, body: object | None = None) -> object:
        data = None if body is None else json.dumps(body).encode()
        for attempt in (0, 1):
            headers = self._headers()
            if data is not None:
                headers["Content-Type"] = "application/json"
            try:
                response = self._transport.send(
                    method,
                    f"{self.base}{path}",
                    headers,
                    data,
                    limit=MAX_JSON_BYTES,
                    timeout=60,
                )
            except ConnectedError as error:
                if error.status == 401 and attempt == 0:
                    self._refresh()
                    continue
                if error.status == 403:
                    raise ConnectedError(
                        "Your Grimmory account can't open that item. Check its library access in Grimmory.",
                        403,
                    ) from None
                raise
            return json_body(response) if response.body else {}
        raise ConnectedError("Your Grimmory sign-in expired. Sign in again.", 401)

    def _download(self, path: str, destination: Path, cancelled: Event | None) -> str:
        for attempt in (0, 1):
            try:
                return self._transport.download(
                    f"{self.base}{path}",
                    self._headers("*/*"),
                    destination,
                    cancelled=cancelled,
                )
            except ConnectedError as error:
                if error.status == 401 and attempt == 0:
                    self._refresh()
                    continue
                raise
        raise ConnectedError("Your Grimmory sign-in expired. Sign in again.", 401)

    def username(self) -> str:
        """The signed-in account's username."""

        data = _object(self._call("GET", "/api/v1/users/me"))
        return _str(data.get("username")) or _str(data.get("name"))

    # --- browsing ---------------------------------------------------------------------

    def collections(self) -> tuple[GrimmoryCollection, ...]:
        found: list[GrimmoryCollection] = []
        for kind, path in (
            ("library", "/api/v1/app/libraries"),
            ("shelf", "/api/v1/app/shelves"),
        ):
            found.extend(
                GrimmoryCollection(
                    kind,
                    _str(row.get("id")),
                    _str(row.get("name")) or kind.capitalize(),
                    _int(row.get("bookCount")),
                )
                for row in _objects(self._call("GET", path))
                if _str(row.get("id"))
            )
        return tuple(found)

    def audiobooks(
        self,
        collection: GrimmoryCollection | None = None,
        search: str = "",
        cancelled: Event | None = None,
    ) -> tuple[Audiobook, ...]:
        books: list[Audiobook] = []
        for page in range(_MAX_PAGES):
            if cancelled is not None and cancelled.is_set():
                raise CancelledError
            query: dict[str, str] = {
                "fileType": "AUDIOBOOK",
                "page": str(page),
                "size": str(_PAGE),
                "sort": "title",
                "dir": "asc",
            }
            if search.strip():
                query["search"] = search.strip()
            if collection is not None:
                query["libraryId" if collection.kind == "library" else "shelfId"] = (
                    collection.id
                )
            data = _object(self._call("GET", f"/api/v1/app/books?{urlencode(query)}"))
            for row in _objects(data.get("content")):
                book_id = _str(row.get("id"))
                if not book_id:
                    continue
                authors = [
                    str(a) for a in cast("list[object]", row.get("authors") or []) if a
                ]
                books.append(
                    Audiobook(
                        book_id,
                        _str(row.get("title")) or "Untitled audiobook",
                        ", ".join(authors),
                        _str(row.get("seriesName")),
                    )
                )
            if not data.get("hasNext"):
                break
        return tuple(books)

    def info(self, book_id: str) -> AudiobookInfo:
        data = _object(
            self._call("GET", f"/api/v1/audiobooks/{book_id}/info?bookType=AUDIOBOOK")
        )
        tracks = tuple(
            AudiobookTrack(
                _int(row.get("index")),
                _str(row.get("fileName")),
                _str(row.get("title")),
                _int(row.get("durationMs")),
                _int(row.get("cumulativeStartMs")),
            )
            for row in _objects(data.get("tracks"))
        )
        file_id = _str(data.get("bookFileId"))
        file_name = ""
        if not data.get("folderBased"):
            try:  # only for the file's extension; the download's type is a fallback
                detail = _object(self._call("GET", f"/api/v1/app/books/{book_id}"))
            except CancelledError:
                raise
            except ConnectedError as error:
                if error.status == 401:
                    raise
                detail = {}
            for row in _objects(detail.get("files")):
                if _str(row.get("id")) == file_id:
                    file_name = _str(row.get("fileName"))
        if not file_id:
            raise ConnectedError("Grimmory has no audiobook file for that book.")
        return AudiobookInfo(
            book_id,
            file_id,
            _str(data.get("title")),
            _int(data.get("durationMs")),
            bool(data.get("folderBased")),
            tracks,
            file_name,
        )

    def cover(self, book_id: str) -> bytes:
        try:
            return self._transport.send(
                "GET",
                f"{self.base}/api/v1/audiobooks/{book_id}/cover",
                self._headers("image/*"),
                limit=8 * 1024 * 1024,
            ).body
        except ConnectedError:
            return b""

    def download_book(
        self, info: AudiobookInfo, destination: Path, cancelled: Event | None = None
    ) -> str:
        return self._download(
            f"/api/v1/books/{info.book_id}/files/{info.book_file_id}/download",
            destination,
            cancelled,
        )

    def download_track(
        self,
        info: AudiobookInfo,
        track: AudiobookTrack,
        destination: Path,
        cancelled: Event | None = None,
    ) -> str:
        return self._download(
            f"/api/v1/audiobooks/{info.book_id}/track/{track.index}/stream?bookType=AUDIOBOOK",
            destination,
            cancelled,
        )

    # --- progress ---------------------------------------------------------------------

    def progress(self, book_id: str) -> GrimmoryProgress:
        data = _object(self._call("GET", f"/api/v1/app/books/{book_id}/progress"))
        audio = _object(data.get("audiobookProgress"))
        if not audio:
            return GrimmoryProgress(0, None, 0.0, 0)
        index = audio.get("trackIndex")
        percentage = audio.get("percentage")
        return GrimmoryProgress(
            _int(audio.get("positionMs")),
            index if isinstance(index, int) and not isinstance(index, bool) else None,
            float(percentage) if isinstance(percentage, int | float) else 0.0,
            _epoch(audio.get("updatedAt")) or _epoch(data.get("lastReadTime")),
        )

    def save_progress(
        self,
        link: GrimmoryLink,
        position_ms: int,
        track_index: int | None,
        track_position_ms: int,
    ) -> None:
        """Record progress exactly as Grimmory's own audiobook player does."""

        percentage = (
            round(min(100.0, max(0.0, 100 * position_ms / link.duration_ms)), 1)
            if link.duration_ms
            else 0.0
        )
        progress: dict[str, object] = {
            "positionMs": position_ms,
            "trackPositionMs": track_position_ms,
            "percentage": percentage,
        }
        file_progress: dict[str, object] = {
            "bookFileId": int(link.book_file_id),
            "positionData": str(position_ms),
            "progressPercent": percentage,
        }
        if track_index is not None:
            progress["trackIndex"] = track_index
            file_progress["positionHref"] = str(track_index)
        self._call(
            "POST",
            "/api/v1/books/progress",
            {
                "bookId": int(link.book_id),
                "audiobookProgress": progress,
                "fileProgress": file_progress,
            },
        )


# --- files ------------------------------------------------------------------------------


def tag_audiobook(path: Path, link: GrimmoryLink) -> Path:
    """Mark a file as an Audiobook and store its Grimmory Link in the comment.

    ``.m4a`` and ``.mp4`` become ``.m4b`` (the audiobook extension of the same
    container) with ``stik`` 2; MP3s get an ID3 ``media_type`` of ``audiobook``.
    Returns the file's final path.
    """

    suffix = path.suffix.casefold()
    from mutagen import id3, mp4

    native_id3: Any = id3
    native_mp4: Any = mp4
    if suffix in {".m4a", ".m4b", ".mp4"}:
        target = path.with_suffix(".m4b")
        if target != path:
            path.replace(target)
        try:
            tags = native_mp4.MP4(target)
            tags["stik"] = [2]
            tags["\xa9cmt"] = [link.comment]
            tags.save()
        except Exception as error:
            raise ConnectedError(f"Couldn't tag {target.name}: {error}") from None
        return target
    if suffix == ".mp3":
        try:
            tags = native_id3.ID3(path)
        except native_id3.ID3NoHeaderError:
            tags = native_id3.ID3()
        tags.delall("TXXX:media_type")
        tags.add(native_id3.TXXX(encoding=3, desc="media_type", text=["audiobook"]))
        tags.delall("COMM")
        tags.add(native_id3.COMM(encoding=3, lang="eng", desc="", text=[link.comment]))
        tags.save(path)
        return path
    try:
        import mutagen

        media: Any = cast("Any", mutagen).File(path)
        if media is not None and media.tags is not None:
            media.tags["comment"] = [link.comment]
            media.save()
    except Exception:
        pass  # other formats sync by their folder's audio role; progress uses titles
    return path


def book_folder(pick: GrimmoryPick) -> str:
    title = pick.title or f"Audiobook {pick.book_id}"
    label = f"{pick.author} - {title}" if pick.author else title
    return relative_path(safe_component(label, "Audiobook", limit=120))


def _extension(name: str, content_type: str, fallback: str = ".m4b") -> str:
    lower = name.casefold()
    for extension in AUDIO_EXTENSIONS:
        if lower.endswith(extension):
            return extension
    return {
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4b",
        "audio/x-m4b": ".m4b",
        "audio/opus": ".opus",
        "audio/ogg": ".ogg",
        "audio/flac": ".flac",
    }.get(content_type.split(";", 1)[0].strip().casefold(), fallback)


def fetch_audiobook(
    client: GrimmoryClient,
    pick: GrimmoryPick,
    staging: Path,
    cancelled: Event | None = None,
) -> None:
    info = client.info(pick.book_id)
    total = info.duration_ms or sum(track.duration_ms for track in info.tracks)
    if info.folder_based and info.tracks:
        width = max(2, len(str(len(info.tracks))))
        for track in info.tracks:
            if cancelled is not None and cancelled.is_set():
                raise CancelledError
            temporary = staging / f".track-{track.index}.part"
            content_type = client.download_track(info, track, temporary, cancelled)
            extension = _extension(track.file_name, content_type, ".mp3")
            stem = safe_component(
                f"{track.index + 1:0{width}d} {track.title or track.file_name.rsplit('.', 1)[0]}",
                f"{track.index + 1:0{width}d}",
                limit=100,
            )
            final = staging / f"{stem}{extension}"
            temporary.replace(final)
            tag_audiobook(
                final,
                GrimmoryLink(
                    info.book_id, info.book_file_id, track.index, track.start_ms, total
                ),
            )
    else:
        temporary = staging / ".book.part"
        content_type = client.download_book(info, temporary, cancelled)
        extension = _extension(info.file_name, content_type)
        final = (
            staging
            / f"{safe_component(pick.title or info.title, 'Audiobook', limit=100)}{extension}"
        )
        temporary.replace(final)
        tag_audiobook(final, GrimmoryLink(info.book_id, info.book_file_id, 0, 0, total))
    cover = client.cover(info.book_id)
    if cover[:3] == b"\xff\xd8\xff" or cover[:8] == b"\x89PNG\r\n\x1a\n":
        write_atomic(
            staging / ("cover.jpg" if cover[:1] == b"\xff" else "cover.png"), cover
        )


def plan_grimmory(
    client: GrimmoryClient,
    config: GrimmoryConfig,
    *,
    cancelled: Event,
    progress: Callable[[str], None] = lambda _text: None,
) -> tuple[DesiredBundle, ...]:
    items: list[DesiredBundle] = []
    used: set[str] = set()
    for index, pick in enumerate(config.picks, start=1):
        if cancelled.is_set():
            raise CancelledError
        progress(f"Preparing audiobook {index} of {len(config.picks)}: {pick.title}")
        folder = book_folder(pick)
        if folder.casefold() in used:
            folder = relative_path(f"{folder} ({pick.book_id})")
        used.add(folder.casefold())
        items.append(
            DesiredBundle(
                folder,
                f"grimmory-native:{pick.book_id}",
                _fetcher(client, pick, cancelled),
            )
        )
    return tuple(items)


def _fetcher(
    client: GrimmoryClient, pick: GrimmoryPick, cancelled: Event
) -> Callable[[Path], None]:
    def fetch(staging: Path) -> None:
        fetch_audiobook(client, pick, staging, cancelled)

    return fetch


__all__ = [
    "AUDIO_EXTENSIONS",
    "Audiobook",
    "AudiobookInfo",
    "AudiobookTrack",
    "GrimmoryClient",
    "GrimmoryCollection",
    "GrimmoryLink",
    "GrimmoryProgress",
    "GrimmoryTokens",
    "book_folder",
    "fetch_audiobook",
    "parse_link",
    "plan_grimmory",
    "tag_audiobook",
]
