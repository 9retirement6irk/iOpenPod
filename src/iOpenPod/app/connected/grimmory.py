"""Grimmory audiobooks over its OPDS catalog (HTTP Basic with an OPDS user).

Grimmory (a BookLore fork) publishes OPDS feeds at ``/api/v1/opds``. Its OPDS
users are separate credentials created in Grimmory's settings, so iOpenPod never
needs the person's main password. Each audiobook entry carries an acquisition
link titled ``AUDIOBOOK``; folder-based audiobooks download as a ZIP archive.
"""

from __future__ import annotations

import base64
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urljoin, urlsplit
from xml.etree import ElementTree

from .http import (
    CancelledError,
    ConnectedError,
    Transport,
    UrllibTransport,
    normalize_base_url,
)
from .materialize import DesiredBundle, relative_path, safe_component

if TYPE_CHECKING:
    from collections.abc import Callable
    from threading import Event

    from .config import GrimmoryConfig, GrimmoryPick

_ATOM = "{http://www.w3.org/2005/Atom}"
_OPDS = "/api/v1/opds"
_FEED_LIMIT = 16 * 1024 * 1024
_MAX_PAGES = 400
AUDIO_EXTENSIONS = frozenset(
    {".m4b", ".m4a", ".mp3", ".aac", ".opus", ".ogg", ".flac", ".wav"}
)
_COVER_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})
MAX_ARCHIVE_MEMBERS = 5_000


@dataclass(frozen=True, slots=True)
class OpdsLink:
    href: str
    rel: str
    type: str
    title: str


@dataclass(frozen=True, slots=True)
class OpdsShelf:
    """A navigation entry: a library, shelf, series, or author feed."""

    title: str
    href: str


@dataclass(frozen=True, slots=True)
class Audiobook:
    book_id: str
    title: str
    author: str
    download: str
    mime: str
    cover: str
    series: str = ""


@dataclass(frozen=True, slots=True)
class _Feed:
    entries: tuple[ElementTree.Element, ...]
    next: str


def _text(element: ElementTree.Element | None) -> str:
    return " ".join((element.text or "").split()) if element is not None else ""


class GrimmoryClient:
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        transport: Transport | None = None,
    ) -> None:
        base = normalize_base_url(url)
        for suffix in (_OPDS, "/api/v1", "/api"):
            if base.casefold().endswith(suffix):
                base = base[: -len(suffix)]
                break
        if not username.strip() or not password:
            raise ConnectedError(
                "Enter a Grimmory OPDS username and password (Grimmory → Settings → OPDS)."
            )
        self.base = base
        token = base64.b64encode(f"{username.strip()}:{password}".encode()).decode(
            "ascii"
        )
        self._auth = {"Authorization": f"Basic {token}"}
        self._transport = transport or UrllibTransport()

    def _url(self, href: str) -> str:
        absolute = urljoin(self.base + "/", href)
        if urlsplit(absolute)[:2] != urlsplit(self.base)[:2]:
            raise ConnectedError(
                "Grimmory linked to another server; refusing to follow."
            )
        return absolute

    def _feed(self, href: str) -> _Feed:
        try:
            response = self._transport.send(
                "GET",
                self._url(href),
                {**self._auth, "Accept": "application/atom+xml,application/xml"},
                limit=_FEED_LIMIT,
                timeout=60,
            )
        except ConnectedError as error:
            if error.status == 401:
                raise ConnectedError(
                    "Grimmory didn't accept the OPDS username or password.", 401
                ) from None
            if error.status == 403:
                raise ConnectedError(
                    "This Grimmory OPDS user can't open that library. Check its access in Grimmory.",
                    403,
                ) from None
            raise
        if b"<!DOCTYPE" in response.body[:2048].upper():
            raise ConnectedError(
                "Grimmory sent an unexpected page instead of a catalog."
            )
        try:
            root = ElementTree.fromstring(response.body)
        except ElementTree.ParseError:
            raise ConnectedError(
                "Grimmory sent a catalog that couldn't be read."
            ) from None
        if root.tag != f"{_ATOM}feed":
            raise ConnectedError(
                "That address answered, but not with a Grimmory catalog."
            )
        following = next(
            (
                link.get("href", "")
                for link in root.findall(f"{_ATOM}link")
                if link.get("rel") == "next"
            ),
            "",
        )
        return _Feed(tuple(root.findall(f"{_ATOM}entry")), following)

    def check(self) -> None:
        self._feed(_OPDS)

    def shelves(self, kind: str) -> tuple[OpdsShelf, ...]:
        """Navigation feeds: ``libraries``, ``shelves``, ``magic-shelves``, ``series``, ``authors``."""

        if kind not in {"libraries", "shelves", "magic-shelves", "series", "authors"}:
            raise ValueError(kind)
        found: list[OpdsShelf] = []
        href = f"{_OPDS}/{kind}"
        for _ in range(_MAX_PAGES):
            feed = self._feed(href)
            for entry in feed.entries:
                link = next(
                    (
                        item.get("href", "")
                        for item in entry.findall(f"{_ATOM}link")
                        if item.get("rel") == "subsection"
                    ),
                    "",
                )
                if link:
                    found.append(OpdsShelf(_text(entry.find(f"{_ATOM}title")), link))
            if not feed.next:
                break
            href = feed.next
        return tuple(found)

    def audiobooks(
        self, href: str = f"{_OPDS}/catalog", cancelled: Event | None = None
    ) -> tuple[Audiobook, ...]:
        """Every audiobook in an acquisition feed, following its pages."""

        books: list[Audiobook] = []
        for _ in range(_MAX_PAGES):
            if cancelled is not None and cancelled.is_set():
                raise CancelledError
            feed = self._feed(href)
            books.extend(book for entry in feed.entries if (book := _audiobook(entry)))
            if not feed.next:
                break
            href = feed.next
        return tuple(books)

    def search(self, query: str) -> tuple[Audiobook, ...]:
        text = query.strip()
        if not text:
            return ()
        return self.audiobooks(f"{_OPDS}/catalog?q={quote(text)}")

    def cover(self, book: Audiobook) -> bytes:
        if not book.cover:
            return b""
        return self._transport.send(
            "GET",
            self._url(book.cover),
            {**self._auth, "Accept": "image/*"},
            limit=8 * 1024 * 1024,
        ).body

    def download(
        self,
        book_id: str,
        file_href: str,
        destination: Path,
        cancelled: Event | None = None,
    ) -> str:
        href = file_href or f"{_OPDS}/{quote(book_id)}/download"
        return self._transport.download(
            self._url(href),
            {**self._auth, "Accept": "*/*"},
            destination,
            cancelled=cancelled,
        )


def _audiobook(entry: ElementTree.Element) -> Audiobook | None:
    identity = _text(entry.find(f"{_ATOM}id"))
    book_id = identity.rsplit(":", 1)[-1] if identity.startswith("urn:") else ""
    links = [
        OpdsLink(
            link.get("href", ""),
            link.get("rel", ""),
            link.get("type", ""),
            link.get("title", ""),
        )
        for link in entry.findall(f"{_ATOM}link")
    ]
    acquisition = next(
        (
            link
            for link in links
            if link.rel.startswith("http://opds-spec.org/acquisition")
            and (link.title.upper() == "AUDIOBOOK" or link.type.startswith("audio/"))
        ),
        None,
    )
    if not book_id or acquisition is None or not acquisition.href:
        return None
    if not book_id.isdigit():
        query = parse_qs(urlsplit(acquisition.href).query)
        book_id = book_id or (query.get("bookId") or [""])[0]
    authors = [
        _text(author.find(f"{_ATOM}name")) for author in entry.findall(f"{_ATOM}author")
    ]
    cover = next(
        (link.href for link in links if link.rel == "http://opds-spec.org/image"), ""
    )
    series = ""
    for meta in entry.iter():
        if (
            meta.tag.endswith("meta")
            and meta.get("property") == "belongs-to-collection"
        ):
            series = _text(meta)
    return Audiobook(
        book_id,
        _text(entry.find(f"{_ATOM}title")) or "Untitled audiobook",
        ", ".join(name for name in authors if name),
        acquisition.href,
        acquisition.type,
        cover,
        series,
    )


def unpack_download(
    archive_or_file: Path, staging: Path, title: str, content_type: str
) -> None:
    """Place one downloaded audiobook into ``staging`` as audiobook-tagged files."""

    if zipfile.is_zipfile(archive_or_file):
        with zipfile.ZipFile(archive_or_file) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ConnectedError("The audiobook archive has too many files.")
            for member in members:
                inner = PurePosixPath(member.filename.replace("\\", "/"))
                suffix = inner.suffix.casefold()
                if (
                    inner.is_absolute()
                    or ".." in inner.parts
                    or (
                        suffix not in AUDIO_EXTENSIONS
                        and suffix not in _COVER_EXTENSIONS
                    )
                ):
                    continue
                name = safe_component(inner.name)
                if suffix in _COVER_EXTENSIONS:
                    name = f"cover{suffix}"
                target = staging / name
                if target.exists():
                    target = staging / safe_component(
                        f"{len(list(staging.iterdir()))} {name}"
                    )
                with archive.open(member) as source, target.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
        archive_or_file.unlink(missing_ok=True)
    else:
        extension = {
            "audio/mpeg": ".mp3",
            "audio/opus": ".opus",
            "audio/mp4": ".m4b",
            "audio/x-m4b": ".m4b",
            "audio/m4b": ".m4b",
        }.get(content_type.split(";", 1)[0].strip().casefold(), ".m4b")
        archive_or_file.replace(staging / safe_component(f"{title}{extension}"))
    audio = [p for p in staging.iterdir() if p.suffix.casefold() in AUDIO_EXTENSIONS]
    if not audio:
        raise ConnectedError("The download contained no audio files.")
    for path in audio:
        mark_audiobook(path)


def mark_audiobook(path: Path) -> None:
    """Tag a file so iOpenPod's Host Media Scan classifies it as an Audiobook.

    ``.m4a`` becomes ``.m4b`` (the same container, the audiobook extension); MP3s
    get an ID3 ``media_type`` frame. Other formats keep their tags; they are
    converted during Sync and classified by their folder's Audiobook role there.
    """

    suffix = path.suffix.casefold()
    if suffix == ".m4a":
        path.replace(path.with_suffix(".m4b"))
        return
    if suffix != ".mp3":
        return
    from mutagen import id3

    # Mutagen is untyped; existing tag code adapts it the same way.
    native: Any = id3
    try:
        tags = native.ID3(path)
    except native.ID3NoHeaderError:
        tags = native.ID3()
    tags.delall("TXXX:media_type")
    tags.add(native.TXXX(encoding=3, desc="media_type", text=["audiobook"]))
    tags.save(path)


def book_folder(pick: GrimmoryPick) -> str:
    title = pick.title or f"Audiobook {pick.book_id}"
    label = f"{pick.author} - {title}" if pick.author else title
    return relative_path(safe_component(label, "Audiobook", limit=120))


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
                folder, f"grimmory:{pick.book_id}", _fetcher(client, pick, cancelled)
            )
        )
    return tuple(items)


def _fetcher(
    client: GrimmoryClient, pick: GrimmoryPick, cancelled: Event
) -> Callable[[Path], None]:
    def fetch(staging: Path) -> None:
        archive = staging.parent / f".grimmory-{pick.book_id}.download"
        try:
            content_type = client.download(
                pick.book_id, pick.download, archive, cancelled
            )
            unpack_download(archive, staging, pick.title, content_type)
        finally:
            archive.unlink(missing_ok=True)

    return fetch


__all__ = [
    "AUDIO_EXTENSIONS",
    "Audiobook",
    "GrimmoryClient",
    "OpdsShelf",
    "book_folder",
    "mark_audiobook",
    "plan_grimmory",
    "unpack_download",
]
