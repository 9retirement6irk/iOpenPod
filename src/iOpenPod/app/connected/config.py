"""Non-secret Connected Source choices, kept in one Host configuration file.

Selections and preferences are plain JSON beside other iOpenPod Host settings.
Anything that grants access (tokens, keys, passwords, private calendar links) lives
in the OS vault through :mod:`secrets` instead.
"""

from __future__ import annotations

import json
from dataclasses import MISSING, asdict, dataclass, field, fields, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from storage import AtomicHostFile

_VERSION = 1
MAX_CONFIG_BYTES = 8 * 1024 * 1024


class PlexPickKind(StrEnum):
    PLAYLIST = "playlist"
    ARTIST = "artist"
    ALBUM = "album"
    TRACK = "track"


@dataclass(frozen=True, slots=True)
class PlexPick:
    kind: PlexPickKind
    key: str
    title: str
    subtitle: str = ""


@dataclass(frozen=True, slots=True)
class PathMapping:
    """A Plex server path prefix and the same folder as this Host sees it."""

    server: str
    host: str


@dataclass(frozen=True, slots=True)
class PlexConfig:
    server_id: str = ""
    server_name: str = ""
    username: str = ""
    picks: tuple[PlexPick, ...] = ()
    mappings: tuple[PathMapping, ...] = ()
    sync_ratings: bool = False
    # Music libraries (Plex section keys) left out of Sync; new ones are included.
    skipped_sections: tuple[str, ...] = ()


class ImmichPickKind(StrEnum):
    FAVORITES = "favorites"
    ALBUM = "album"
    PERSON = "person"
    ASSET = "asset"


@dataclass(frozen=True, slots=True)
class ImmichPick:
    kind: ImmichPickKind
    id: str
    title: str


class ImmichSize(StrEnum):
    PREVIEW = "preview"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class ImmichConfig:
    url: str = ""
    picks: tuple[ImmichPick, ...] = ()
    size: ImmichSize = ImmichSize.PREVIEW


@dataclass(frozen=True, slots=True)
class GrimmoryPick:
    book_id: str
    title: str
    author: str = ""


@dataclass(frozen=True, slots=True)
class GrimmoryMark:
    """Book positions on the iPod and in Grimmory when they last agreed."""

    book_id: str
    ipod_ms: int = 0
    grimmory_ms: int = 0


@dataclass(frozen=True, slots=True)
class GrimmoryConfig:
    url: str = ""
    username: str = ""
    picks: tuple[GrimmoryPick, ...] = ()
    # Exchange audiobook listening progress with Grimmory before each Sync.
    sync_progress: bool = True
    marks: tuple[GrimmoryMark, ...] = ()


class TagMatch(StrEnum):
    ANY = "any"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class MemosConfig:
    url: str = ""
    tags: tuple[str, ...] = ()
    match: TagMatch = TagMatch.ANY
    only_mine: bool = True
    include_archived: bool = False


@dataclass(frozen=True, slots=True)
class CalendarFile:
    """An uploaded calendar, stored as a Host file next to this configuration."""

    name: str
    filename: str


@dataclass(frozen=True, slots=True)
class CalendarsConfig:
    files: tuple[CalendarFile, ...] = ()
    trim_days: int = 30


@dataclass(frozen=True, slots=True)
class ConnectedConfig:
    folder_root: str = ""
    plex: PlexConfig = field(default_factory=PlexConfig)
    immich: ImmichConfig = field(default_factory=ImmichConfig)
    grimmory: GrimmoryConfig = field(default_factory=GrimmoryConfig)
    # Keep Plex and Grimmory downloads between Syncs, up to this many GB.
    cache_media: bool = True
    cache_limit_gb: int = 20
    memos: MemosConfig = field(default_factory=MemosConfig)
    calendars: CalendarsConfig = field(default_factory=CalendarsConfig)

    def root(self) -> Path:
        if self.folder_root:
            return Path(self.folder_root)
        return Path.home() / "Music" / "iOpenPod Connected"


# --- tolerant decoding: unknown or malformed values fall back to defaults ---------


def _decode[T](cls: type[T], raw: object) -> T:
    if not isinstance(raw, dict):
        return cls()
    data = cast("dict[str, object]", raw)
    values: dict[str, Any] = {}
    defaults = cls()
    for item in fields(cls):  # type: ignore[arg-type]
        if item.name not in data:
            continue
        current = getattr(defaults, item.name)
        decoded = _value(current, data[item.name], cls, item.name)
        if decoded is not None:
            values[item.name] = decoded
    return replace(defaults, **values)  # type: ignore[type-var]


_ITEM_TYPES: dict[tuple[type, str], type] = {
    (PlexConfig, "picks"): PlexPick,
    (PlexConfig, "mappings"): PathMapping,
    (ImmichConfig, "picks"): ImmichPick,
    (GrimmoryConfig, "picks"): GrimmoryPick,
    (GrimmoryConfig, "marks"): GrimmoryMark,
    (CalendarsConfig, "files"): CalendarFile,
}


def _value(current: object, raw: object, owner: type, name: str) -> object | None:
    if isinstance(current, bool):
        return raw if isinstance(raw, bool) else None
    if isinstance(current, StrEnum):
        if not isinstance(raw, str):
            return None
        try:
            return type(current)(raw)
        except ValueError:
            return None
    if isinstance(current, int):
        return raw if isinstance(raw, int) and not isinstance(raw, bool) else None
    if isinstance(current, str):
        return raw if isinstance(raw, str) else None
    if isinstance(current, tuple):
        if not isinstance(raw, list):
            return None
        item_type = _ITEM_TYPES.get((owner, name))
        values: list[object] = []
        for entry in cast("list[object]", raw):
            if item_type is None:
                if isinstance(entry, str):
                    values.append(entry)
                continue
            decoded = _record(item_type, entry)
            if decoded is not None:
                values.append(decoded)
        return tuple(values)
    if hasattr(current, "__dataclass_fields__"):
        return _decode(type(current), raw)
    return None


def _record(cls: type, raw: object) -> object | None:
    if not isinstance(raw, dict):
        return None
    data = cast("dict[str, object]", raw)
    values: dict[str, object] = {}
    for item in fields(cls):
        value = data.get(item.name)
        annotation = str(item.type)
        if annotation in {"PlexPickKind", "ImmichPickKind"}:
            enum_type = PlexPickKind if annotation == "PlexPickKind" else ImmichPickKind
            if not isinstance(value, str):
                return None
            try:
                values[item.name] = enum_type(value)
            except ValueError:
                return None
        elif annotation == "int":
            if isinstance(value, int) and not isinstance(value, bool):
                values[item.name] = value
            elif item.default is MISSING:
                return None
        elif isinstance(value, str):
            values[item.name] = value
        elif item.default is not MISSING:
            continue
        else:
            return None
    try:
        record: object = cls(**values)
    except TypeError:
        return None
    return record


def _plain(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


class ConnectedConfigStore:
    def __init__(self, host_file: AtomicHostFile) -> None:
        self._file = host_file

    @property
    def directory(self) -> Path:
        return self._file.path.parent

    def load(self) -> ConnectedConfig:
        try:
            raw = self._file.read_bytes(max_bytes=MAX_CONFIG_BYTES)
        except OSError:
            return ConnectedConfig()
        if raw is None:
            return ConnectedConfig()
        try:
            data: object = json.loads(raw)
        except ValueError:
            return ConnectedConfig()
        if not isinstance(data, dict) or data.get("version") != _VERSION:
            return ConnectedConfig()
        return _decode(ConnectedConfig, data)

    def save(self, config: ConnectedConfig) -> None:
        payload = {
            "version": _VERSION,
            **cast("dict[str, object]", _plain(asdict(config))),
        }
        self._file.replace_bytes(json.dumps(payload, indent=1).encode("utf-8"))


__all__ = [
    "CalendarFile",
    "CalendarsConfig",
    "ConnectedConfig",
    "ConnectedConfigStore",
    "GrimmoryConfig",
    "GrimmoryMark",
    "GrimmoryPick",
    "ImmichConfig",
    "ImmichPick",
    "ImmichPickKind",
    "ImmichSize",
    "MemosConfig",
    "PathMapping",
    "PlexConfig",
    "PlexPick",
    "PlexPickKind",
    "TagMatch",
]
