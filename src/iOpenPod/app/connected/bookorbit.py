"""BookOrbit audiobooks and listening progress through BookOrbit's REST API.

iOpenPod connects with a BookOrbit API key (Settings → API Keys in BookOrbit), kept
in the OS vault. A key with write access is needed to send listening progress; a
read-only key can still download.

Audiobooks are listed with the book query (``POST /api/v1/books/query``, filtered to
audio formats) and described by their manifest (``GET /api/v1/audiobooks/{id}/manifest``):
the audio files ("assets") in playing order with their durations, and a revision that
changes when the files do. Each asset downloads from
``/api/v1/audiobooks/{id}/assets/{asset}/content``.

Every downloaded file carries a link in its comment tag, in the same shape as a
Grimmory Link: ``bookorbit:<book>:<file>:<index>:<start ms>:<book duration ms>``.
The tag survives Sync into the iPod Library, so listening progress can be exchanged
without guessing which iPod Track belongs to which book.

Progress is BookOrbit's audiobook playback state, shared with its web player and
apps: an asset and a position inside it, guarded by a revision. iOpenPod turns it into
a position in the whole book so the Grimmory progress rules apply unchanged.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from .grimmory import GrimmoryLink, GrimmoryProgress, tag_audiobook
from .http import (
    MAX_JSON_BYTES,
    CancelledError,
    ConnectedError,
    Transport,
    UrllibTransport,
    json_body,
    normalize_base_url,
)
from .materialize import relative_path, safe_component

if TYPE_CHECKING:
    from pathlib import Path
    from threading import Event

SOURCE = "bookorbit"
API_KEY_PREFIX = "bok_"
# The formats BookOrbit's own apps play, as its query's format filter names them.
AUDIO_FORMATS = ("m4b", "mp3", "m4a", "opus", "ogg", "flac", "aac", "wav")
_PAGE = 200  # the largest page BookOrbit's book query allows
_MAX_PAGES = 500


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


def _names(value: object) -> str:
    if not isinstance(value, list):
        return ""
    names: list[str] = []
    for entry in cast("list[object]", value):
        name = (
            _str(_object(entry).get("name")) if isinstance(entry, dict) else _str(entry)
        )
        if name.strip():
            names.append(name.strip())
    return ", ".join(names)


def _epoch(value: object) -> int:
    text = _str(value)
    if not text:
        return 0
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class BookOrbitBook:
    book_id: str
    title: str
    author: str
    series: str = ""
    # Changes when the book's files or details change; keys the manifest cache.
    updated: str = ""


@dataclass(frozen=True, slots=True)
class BookOrbitAsset:
    asset_id: str
    file_id: str
    sequence: int
    format: str
    duration_ms: int  # 0 when BookOrbit couldn't measure it
    size_bytes: int
    etag: str


@dataclass(frozen=True, slots=True)
class BookOrbitChapter:
    title: str
    start_ms: int  # on the book timeline


@dataclass(frozen=True, slots=True)
class BookOrbitManifest:
    book_id: str
    revision: str
    title: str
    authors: str
    narrators: str
    has_cover: bool
    assets: tuple[BookOrbitAsset, ...]  # in playing order
    total_ms: int
    chapters: tuple[BookOrbitChapter, ...] = ()

    def starts(self) -> tuple[int, ...]:
        """Where each asset starts inside the whole book."""

        starts: list[int] = []
        elapsed = 0
        for asset in self.assets:
            starts.append(elapsed)
            elapsed += asset.duration_ms
        return tuple(starts)

    def link(self, index: int) -> GrimmoryLink:
        asset = self.assets[index]
        return GrimmoryLink(
            self.book_id,
            asset.file_id,
            index,
            self.starts()[index],
            self.total_ms or sum(a.duration_ms for a in self.assets),
            SOURCE,
        )

    def locate(self, position_ms: int) -> tuple[int, int]:
        """``(asset index, position inside it)`` for a position in the whole book."""

        starts = self.starts()
        index = 0
        for candidate, start in enumerate(starts):
            if start <= position_ms:
                index = candidate
        offset = max(0, position_ms - starts[index])
        duration = self.assets[index].duration_ms
        return index, min(offset, duration) if duration else offset


@dataclass(frozen=True, slots=True)
class BookOrbitPlayback:
    asset_id: str
    position_ms: int
    revision: int
    manifest_revision: str
    captured: int  # Unix seconds


def manifest_from(data: dict[str, object], book_id: str = "") -> BookOrbitManifest:
    book = _object(data.get("book"))
    assets = tuple(
        sorted(
            (
                BookOrbitAsset(
                    _str(row.get("assetId")),
                    _str(row.get("fileId")),
                    _int(row.get("sequence")),
                    _str(row.get("format")).casefold(),
                    _int(row.get("durationMs")),
                    _int(row.get("sizeBytes")),
                    _str(row.get("etag")),
                )
                for row in _objects(data.get("assets"))
                if _str(row.get("assetId"))
            ),
            key=lambda asset: asset.sequence,
        )
    )
    return BookOrbitManifest(
        _str(book.get("id")) or book_id,
        _str(data.get("revision")),
        _str(book.get("title")),
        _names(book.get("authors")),
        _names(book.get("narrators")),
        bool(book.get("hasCover")),
        assets,
        _int(data.get("totalDurationMs")),
        tuple(
            BookOrbitChapter(_str(row.get("title")), _int(row.get("startMs")))
            for row in _objects(data.get("chapters"))
        ),
    )


class BookOrbitClient:
    def __init__(
        self, url: str, api_key: str, transport: Transport | None = None
    ) -> None:
        base = normalize_base_url(url)
        for suffix in ("/api/v1", "/api"):
            if base.casefold().endswith(suffix):
                base = base[: -len(suffix)]
                break
        self.base = base
        self.api_key = api_key.strip()
        self._transport = transport or UrllibTransport()

    @property
    def transport(self) -> Transport:
        return self._transport

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        if not self.api_key:
            raise ConnectedError("Enter a BookOrbit API key first.", 401)
        return {"X-Api-Key": self.api_key, "Accept": accept}

    def _explain(self, error: ConnectedError, what: str) -> ConnectedError:
        if error.status == 401:
            return ConnectedError(
                "BookOrbit didn't accept the API key. Check that it hasn't expired or been revoked in BookOrbit → Settings → API Keys.",
                401,
            )
        if error.status == 403 and "read-only" in str(error):
            return ConnectedError(
                "This BookOrbit API key is read-only. Create one with write access to send listening progress.",
                403,
            )
        if error.status == 403:
            return ConnectedError(
                "Your BookOrbit API key can't open that item. Check the key's libraries in BookOrbit.",
                403,
            )
        if what and (error.status or 0) >= 500:
            # Name the step: a bare "HTTP 500" can't be told apart from sign-in.
            return ConnectedError(
                f"BookOrbit had a problem {what} ({error}) Its server log has the details.",
                error.status,
            )
        return error

    def _call(
        self, method: str, path: str, body: object | None = None, *, what: str = ""
    ) -> object:
        headers = self._headers()
        data = None
        if body is not None:
            data = json.dumps(body).encode()
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
        except CancelledError:
            raise
        except ConnectedError as error:
            raise self._explain(error, what) from None
        return json_body(response) if response.body.strip() else None

    # --- account ---------------------------------------------------------------------

    def username(self) -> str:
        """The key owner's username; also checks the address and key."""

        data = _object(
            self._call("GET", "/api/v1/auth/me", what="reading your account")
        )
        return _str(data.get("username")) or _str(data.get("name")) or "BookOrbit"

    # --- browsing ---------------------------------------------------------------------

    def audiobooks(self, cancelled: Event | None = None) -> tuple[BookOrbitBook, ...]:
        books: dict[str, BookOrbitBook] = {}
        for page in range(_MAX_PAGES):
            if cancelled is not None and cancelled.is_set():
                raise CancelledError
            data = _object(
                self._call(
                    "POST",
                    "/api/v1/books/query",
                    {
                        "sort": [{"field": "title", "dir": "asc"}],
                        "pagination": {"page": page, "size": _PAGE},
                        "filter": {
                            "type": "group",
                            "join": "AND",
                            "rules": [
                                {
                                    "type": "rule",
                                    "field": "format",
                                    "operator": "includesAny",
                                    "value": list(AUDIO_FORMATS),
                                }
                            ],
                        },
                    },
                    what="listing audiobooks",
                )
            )
            rows = _objects(data.get("items"))
            for row in rows:
                book_id = _str(row.get("id"))
                if not book_id:
                    continue
                authors = [
                    str(a)
                    for a in cast("list[object]", row.get("authors") or [])
                    if isinstance(a, str) and a.strip()
                ]
                books[book_id] = BookOrbitBook(
                    book_id,
                    _str(row.get("title")) or "Untitled audiobook",
                    ", ".join(authors),
                    _str(row.get("seriesName")),
                    _str(row.get("updatedAt")) or _str(row.get("coverVersion")),
                )
            size = _int(data.get("size")) or _PAGE
            if not rows or (page + 1) * size >= _int(data.get("total")):
                break
        return tuple(books.values())

    def manifest_data(self, book_id: str) -> dict[str, object]:
        """The manifest exactly as BookOrbit sent it, for caching."""

        return _object(
            self._call(
                "GET",
                f"/api/v1/audiobooks/{book_id}/manifest",
                what="describing an audiobook",
            )
        )

    def manifest(self, book_id: str) -> BookOrbitManifest:
        manifest = manifest_from(self.manifest_data(book_id), book_id)
        if not manifest.assets:
            raise ConnectedError("BookOrbit has no audio files for that book.")
        return manifest

    def cover(self, book_id: str) -> bytes:
        try:
            return self._transport.send(
                "GET",
                f"{self.base}/api/v1/books/{book_id}/cover",
                self._headers("image/*"),
                limit=8 * 1024 * 1024,
            ).body
        except ConnectedError:
            return b""

    def download_asset(
        self,
        book_id: str,
        asset: BookOrbitAsset,
        destination: Path,
        cancelled: Event | None = None,
    ) -> str:
        try:
            return self._transport.download(
                f"{self.base}/api/v1/audiobooks/{book_id}/assets/{asset.asset_id}/content",
                self._headers("*/*"),
                destination,
                cancelled=cancelled,
            )
        except CancelledError:
            raise
        except ConnectedError as error:
            raise self._explain(error, "downloading an audiobook") from None

    # --- progress ---------------------------------------------------------------------

    def playback(self, book_id: str) -> BookOrbitPlayback | None:
        data = self._call(
            "GET",
            f"/api/v1/audiobooks/{book_id}/playback-state",
            what="reading listening progress",
        )
        if not isinstance(data, dict):
            return None  # BookOrbit answers null before the first listen
        state = _object(data)
        return BookOrbitPlayback(
            _str(state.get("assetId")),
            _int(state.get("positionMs")),
            _int(state.get("revision")),
            _str(state.get("manifestRevision")),
            _epoch(state.get("capturedAt")),
        )

    def progress(self, book_id: str) -> GrimmoryProgress:
        """BookOrbit's position as a position in the whole book.

        Raises ``ConnectedError`` with status 404 when BookOrbit no longer has the book.
        """

        state = self.playback(book_id)
        if state is None:
            return GrimmoryProgress(0, None, 0.0, 0)
        manifest = self.manifest(book_id)
        ids = [asset.asset_id for asset in manifest.assets]
        if state.asset_id not in ids:
            return GrimmoryProgress(0, None, 0.0, 0)
        index = ids.index(state.asset_id)
        position = manifest.starts()[index] + state.position_ms
        total = manifest.total_ms or 1
        return GrimmoryProgress(
            position, index, min(100.0, 100 * position / total), state.captured
        )

    def save_progress(
        self,
        link: GrimmoryLink,
        position_ms: int,
        track_index: int | None,
        track_position_ms: int,
    ) -> None:
        """Record the iPod's position as BookOrbit's playback state.

        The position is placed by the file it was heard in when BookOrbit still has
        that file, otherwise by where it falls in the book as it is now.
        """

        del track_index  # the file id says which asset more reliably
        for attempt in (0, 1):
            manifest = self.manifest(link.book_id)
            index = next(
                (
                    i
                    for i, asset in enumerate(manifest.assets)
                    if asset.file_id == link.book_file_id
                ),
                None,
            )
            if index is None:
                index, offset = manifest.locate(position_ms)
            else:
                offset = track_position_ms
                duration = manifest.assets[index].duration_ms
                if duration:
                    offset = min(offset, duration)
            current = self.playback(link.book_id)
            captured = _now()
            body = {
                "assetId": manifest.assets[index].asset_id,
                "positionMs": max(0, offset),
                "capturedAt": captured,
                "operationId": str(uuid.uuid4()),
                "baseRevision": current.revision if current is not None else 0,
                "manifestRevision": manifest.revision,
            }
            try:
                self._call(
                    "PUT",
                    f"/api/v1/audiobooks/{link.book_id}/playback-state",
                    body,
                    what="saving listening progress",
                )
                return
            except ConnectedError as error:
                # Another player wrote in between (409) or the files changed (412):
                # read again and retry once.
                if error.status not in (409, 412) or attempt:
                    raise
        raise ConnectedError("BookOrbit kept changing while saving progress.")


# --- files ------------------------------------------------------------------------------


def book_folder(book: BookOrbitBook) -> str:
    title = book.title or f"Audiobook {book.book_id}"
    label = f"{book.author} - {title}" if book.author else title
    return relative_path(safe_component(label, "Audiobook", limit=120))


def extension(asset: BookOrbitAsset) -> str:
    """The file extension an asset is saved with, before tagging."""

    value = asset.format.casefold().lstrip(".")
    return f".{value}" if value in AUDIO_FORMATS else ".m4b"


def asset_name(manifest: BookOrbitManifest, index: int, book_title: str) -> str:
    """The downloaded file's name; ``.m4a`` assets end up ``.m4b`` once tagged."""

    suffix = extension(manifest.assets[index])
    suffix = ".m4b" if suffix in {".m4a", ".m4b"} else suffix
    if len(manifest.assets) == 1:
        return f"{safe_component(book_title or manifest.title, 'Audiobook', limit=100)}{suffix}"
    width = max(2, len(str(len(manifest.assets))))
    return f"{index + 1:0{width}d} {safe_component(book_title or manifest.title, 'Part', limit=90)}{suffix}"


def fetch_asset(
    client: BookOrbitClient,
    manifest: BookOrbitManifest,
    index: int,
    staging: Path,
    cancelled: Event | None = None,
) -> Path:
    """Download one asset into ``staging``, tag it with its link, return the file."""

    asset = manifest.assets[index]
    part = staging / ".part"
    client.download_asset(manifest.book_id, asset, part, cancelled)
    downloaded = staging / f"download{extension(asset)}"
    part.replace(downloaded)
    return tag_audiobook(downloaded, manifest.link(index))


__all__ = [
    "API_KEY_PREFIX",
    "AUDIO_FORMATS",
    "SOURCE",
    "BookOrbitAsset",
    "BookOrbitBook",
    "BookOrbitChapter",
    "BookOrbitClient",
    "BookOrbitManifest",
    "BookOrbitPlayback",
    "asset_name",
    "book_folder",
    "extension",
    "fetch_asset",
    "manifest_from",
]
