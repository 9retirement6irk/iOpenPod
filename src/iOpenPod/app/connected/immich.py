"""Immich access for the Immich Connected Source: albums, favorites, people, photos.

Photos are saved as JPEG, the format iPod Photo artwork is prepared from without
extra codecs: Immich's JPEG previews by default, or full-resolution JPEGs.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

from .config import ImmichConfig, ImmichPick, ImmichPickKind, ImmichSize
from .http import (
    MAX_JSON_BYTES,
    CancelledError,
    ConnectedError,
    Transport,
    UrllibTransport,
    normalize_base_url,
)
from .materialize import DesiredDownload, relative_path, safe_component, write_atomic

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from threading import Event

PERMISSIONS = (
    "album.read",
    "asset.read",
    "asset.view",
    "person.read",
    "asset.download",
)
_SEARCH_PAGE = 1000
_MAX_PAGES = 1000
_IMAGE_LIMIT = 256 * 1024 * 1024


def _object(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [_object(v) for v in cast("list[object]", value) if isinstance(v, dict)]


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _permission_for(path: str) -> str:
    route = path.split("?", 1)[0]
    if route.startswith("/albums"):
        return "album.read"
    if route.startswith("/people"):
        return "person.read"
    if route.startswith("/search/"):
        return "asset.read"
    if route.endswith("/thumbnail"):
        return "asset.view"
    if route.endswith("/original"):
        return "asset.download"
    return ""


@dataclass(frozen=True, slots=True)
class ImmichCollection:
    """A browsable album, person, or the Favorites collection."""

    kind: ImmichPickKind
    id: str
    title: str
    count: int = 0
    cover_asset: str = ""


@dataclass(frozen=True, slots=True)
class ImmichPhoto:
    id: str
    filename: str
    original_mime: str
    taken: str
    checksum: str


class ImmichClient:
    def __init__(
        self, url: str, api_key: str, transport: Transport | None = None
    ) -> None:
        base = normalize_base_url(url)
        if base.casefold().endswith("/api"):
            base = base[:-4]
        if not api_key.strip():
            raise ConnectedError(
                "Enter an Immich API key (Account Settings → API Keys)."
            )
        self.base = base
        self._key = api_key.strip()
        self._transport = transport or UrllibTransport()

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        return {"x-api-key": self._key, "Accept": accept}

    def _request(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        limit: int = MAX_JSON_BYTES,
    ) -> bytes:
        headers = self._headers()
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            return self._transport.send(
                method,
                f"{self.base}/api{path}",
                headers,
                data,
                timeout=120,
                limit=limit,
            ).body
        except ConnectedError as error:
            if error.status == 401:
                raise ConnectedError(
                    "Immich didn't accept the API key. It may be mistyped, expired, or deleted.",
                    401,
                ) from None
            if error.status == 403 and (need := _permission_for(path)):
                raise ConnectedError(
                    f"The Immich API key is missing the {need} permission. Edit the key in "
                    f"Immich → Account Settings → API Keys and tick: {', '.join(PERMISSIONS)}.",
                    403,
                ) from None
            raise

    def _json(self, method: str, path: str, body: object | None = None) -> object:
        try:
            return json.loads(self._request(method, path, body) or b"{}")
        except ValueError:
            raise ConnectedError("Immich sent a reply that is not JSON.") from None

    def ping(self) -> str:
        try:
            self._transport.send(
                "GET",
                f"{self.base}/api/server/ping",
                {"Accept": "application/json"},
                timeout=15,
            )
        except ConnectedError as error:
            raise ConnectedError(
                f"Couldn't reach Immich at {self.base}: {error}"
            ) from None
        try:
            me = _object(self._json("GET", "/users/me"))
        except ConnectedError as error:
            if error.status != 403:
                raise
            me = {}  # user.read is optional; it only names the account
        return _str(me.get("name")) or _str(me.get("email")) or "Immich user"

    def albums(self) -> tuple[ImmichCollection, ...]:
        seen: dict[str, ImmichCollection] = {}
        for query in ("", "?shared=true"):
            try:
                rows = _objects(self._json("GET", f"/albums{query}"))
            except ConnectedError:
                if not query:
                    raise
                continue
            for row in rows:
                album_id = _str(row.get("id"))
                if album_id and album_id not in seen:
                    count = row.get("assetCount")
                    seen[album_id] = ImmichCollection(
                        ImmichPickKind.ALBUM,
                        album_id,
                        _str(row.get("albumName")) or "Untitled album",
                        count if isinstance(count, int) else 0,
                        _str(row.get("albumThumbnailAssetId")),
                    )
        return tuple(sorted(seen.values(), key=lambda item: item.title.casefold()))

    def people(self) -> tuple[ImmichCollection, ...]:
        found: list[ImmichCollection] = []
        for page in range(1, _MAX_PAGES):
            data = _object(
                self._json("GET", f"/people?withHidden=false&size=500&page={page}")
            )
            for row in _objects(data.get("people")):
                person_id, name = _str(row.get("id")), _str(row.get("name")).strip()
                if person_id and name and not row.get("isHidden"):
                    found.append(
                        ImmichCollection(ImmichPickKind.PERSON, person_id, name)
                    )
            if not data.get("hasNextPage"):
                break
        return tuple(sorted(found, key=lambda item: item.title.casefold()))

    def photos(
        self, kind: ImmichPickKind, collection_id: str, cancelled: Event | None = None
    ) -> tuple[ImmichPhoto, ...]:
        """All images (never videos) in an album, a person, or Favorites."""

        body: dict[str, object] = {
            "type": "IMAGE",
            "size": _SEARCH_PAGE,
            "page": 1,
            "withDeleted": False,
        }
        if kind is ImmichPickKind.FAVORITES:
            body["isFavorite"] = True
        elif kind is ImmichPickKind.ALBUM:
            body["albumIds"] = [collection_id]
        elif kind is ImmichPickKind.PERSON:
            body["personIds"] = [collection_id]
        else:
            return (self.photo(collection_id),)
        photos: list[ImmichPhoto] = []
        for _ in range(_MAX_PAGES):
            if cancelled is not None and cancelled.is_set():
                raise CancelledError
            assets = _object(
                _object(self._json("POST", "/search/metadata", body)).get("assets")
            )
            for row in _objects(assets.get("items")):
                photo = _photo(row)
                if photo is not None:
                    photos.append(photo)
            following = assets.get("nextPage")
            if not following:
                break
            body["page"] = int(str(following))
        return tuple(photos)

    def photo(self, asset_id: str) -> ImmichPhoto:
        photo = _photo(_object(self._json("GET", f"/assets/{quote(asset_id)}")))
        if photo is None:
            raise ConnectedError("That Immich item is not a photo or is in the trash.")
        return photo

    def thumbnail(self, asset_id: str) -> bytes:
        """A small preview image (WebP or JPEG) for the picker."""

        return self._request(
            "GET",
            f"/assets/{quote(asset_id)}/thumbnail?size=thumbnail",
            limit=4 * 1024 * 1024,
        )

    def person_thumbnail(self, person_id: str) -> bytes:
        return self._request(
            "GET", f"/people/{quote(person_id)}/thumbnail", limit=4 * 1024 * 1024
        )

    def jpeg(self, photo: ImmichPhoto, size: ImmichSize) -> bytes:
        """The photo as JPEG bytes at the chosen size."""

        asset = quote(photo.id)
        if size is ImmichSize.FULL:
            paths = (
                [f"/assets/{asset}/original"]
                if photo.original_mime in {"image/jpeg", "image/jpg"}
                else []
            ) + [
                f"/assets/{asset}/thumbnail?size=fullsize",
                f"/assets/{asset}/thumbnail?size=preview",
            ]
        else:
            paths = [f"/assets/{asset}/thumbnail?size=preview"]
        last: Exception | None = None
        for path in paths:
            try:
                data = self._request("GET", path, limit=_IMAGE_LIMIT)
                return data if _is_jpeg(data) else to_jpeg(data)
            except ConnectedError as error:
                if error.status in (401, 403):
                    raise
                last = error
        raise ConnectedError(f"Couldn't get a JPEG of {photo.filename}: {last}")


def _photo(row: dict[str, object]) -> ImmichPhoto | None:
    asset_id = _str(row.get("id"))
    if not asset_id or row.get("type", "IMAGE") != "IMAGE" or row.get("isTrashed"):
        return None
    return ImmichPhoto(
        asset_id,
        _str(row.get("originalFileName")) or asset_id,
        _str(row.get("originalMimeType")).casefold(),
        _str(row.get("localDateTime")) or _str(row.get("fileCreatedAt")),
        _str(row.get("checksum")),
    )


def _is_jpeg(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff"


def to_jpeg(data: bytes) -> bytes:
    """Re-encode WebP, PNG, TIFF, GIF and similar images with Pillow."""

    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            output = io.BytesIO()
            image.convert("RGB").save(output, "JPEG", quality=90)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError):
        raise ConnectedError(
            "This image format can't be converted to JPEG here."
        ) from None


def folder_for(pick: ImmichPick) -> str:
    if pick.kind is ImmichPickKind.FAVORITES:
        return "Favorites"
    if pick.kind is ImmichPickKind.ASSET:
        return "Selected Photos"
    prefix = "People - " if pick.kind is ImmichPickKind.PERSON else ""
    return safe_component(f"{prefix}{pick.title}", "Album")


def plan_immich(
    client: ImmichClient,
    config: ImmichConfig,
    *,
    cancelled: Event,
    progress: Callable[[str], None] = lambda _text: None,
) -> tuple[DesiredDownload, ...]:
    items: list[DesiredDownload] = []
    used: set[str] = set()
    for index, pick in enumerate(config.picks, start=1):
        if cancelled.is_set():
            raise CancelledError
        progress(
            f"Reading Immich selection {index} of {len(config.picks)}: {pick.title}"
        )
        folder = folder_for(pick)
        try:
            photos = client.photos(pick.kind, pick.id, cancelled)
        except ConnectedError as error:
            if error.status in (401, 403):
                raise
            progress(f"{pick.title}: {error}")
            continue
        for photo in photos:
            stem = safe_component(photo.filename.rsplit(".", 1)[0], photo.id, limit=80)
            name = f"{stem}.jpg"
            key = relative_path(folder, name)
            if key.casefold() in used:
                key = relative_path(folder, f"{stem}_{photo.id[:8]}.jpg")
            if key.casefold() in used:
                continue
            used.add(key.casefold())
            items.append(
                DesiredDownload(
                    key,
                    f"{photo.id}|{photo.checksum}|{config.size.value}",
                    _fetcher(client, photo, config.size),
                )
            )
    return tuple(items)


def _fetcher(
    client: ImmichClient, photo: ImmichPhoto, size: ImmichSize
) -> Callable[[Path], None]:
    def fetch(destination: Path) -> None:
        write_atomic(destination, client.jpeg(photo, size))

    return fetch


__all__ = [
    "PERMISSIONS",
    "ImmichClient",
    "ImmichCollection",
    "ImmichPhoto",
    "folder_for",
    "plan_immich",
    "to_jpeg",
]
