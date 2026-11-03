"""Reconcile a Connected Source Folder with the files a Selection requires.

Each Connected Source Folder carries a manifest naming exactly the files iOpenPod
wrote there. Refresh downloads missing items, rewrites changed generated files,
and removes owned files that are no longer selected. Files the person placed in
the folder themselves are never modified or removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from .http import CancelledError, ConnectedError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from threading import Event

MANIFEST_NAME = ".iopenpod-connected.json"
_MANIFEST_VERSION = 1
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def safe_component(name: str, fallback: str = "Untitled", *, limit: int = 100) -> str:
    """One portable path component, safe on Windows, macOS, Linux and FAT32."""

    text = unicodedata.normalize("NFC", name)
    text = _UNSAFE.sub("_", text).strip().strip(".").strip()
    if not text:
        text = fallback
    if text.split(".", 1)[0].casefold() in _RESERVED:
        text = f"_{text}"
    if len(text) > limit:
        stem, dot, suffix = text.rpartition(".")
        if dot and 0 < len(suffix) <= 8 and stem:
            text = f"{stem[: limit - len(suffix) - 1].rstrip()}.{suffix}"
        else:
            text = text[:limit].rstrip()
    return text


def relative_path(*components: str) -> str:
    """Join already-safe components into a manifest key (always ``/``)."""

    parts = [safe_component(item) for item in components if item]
    if not parts:
        raise ValueError("A Connected Source file needs a name.")
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class DesiredDownload:
    """A file fetched from the server. ``revision`` changes when it must refresh."""

    relative: str
    revision: str
    fetch: Callable[[Path], None]
    size_hint: int = 0


@dataclass(frozen=True, slots=True)
class DesiredBytes:
    """A small generated file (a playlist, a cover) written from memory."""

    relative: str
    data: bytes


@dataclass(frozen=True, slots=True)
class DesiredBundle:
    """One download that unpacks into several files beneath ``relative``.

    ``fetch`` fills an empty staging directory and must leave only regular files.
    Every produced file is owned and removed together with the bundle.
    """

    relative: str
    revision: str
    fetch: Callable[[Path], None]


type Desired = DesiredDownload | DesiredBytes | DesiredBundle


@dataclass(slots=True)
class RefreshReport:
    downloaded: int = 0
    written: int = 0
    kept: int = 0
    removed: int = 0
    failed: list[str] = field(default_factory=list[str])

    @property
    def summary(self) -> str:
        parts = [
            f"{self.downloaded} downloaded",
            f"{self.kept} already up to date",
            f"{self.removed} removed",
        ]
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class _Owned:
    revision: str
    size: int
    bundle: str = ""


def _load_manifest(folder: Path) -> dict[str, _Owned]:
    path = folder / MANIFEST_NAME
    try:
        raw: object = json.loads(path.read_bytes())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise ConnectedError(
            f"The Connected Source record in {folder} is unreadable. Move that folder aside and refresh again."
        ) from None
    if not isinstance(raw, dict):
        raise ConnectedError(f"The Connected Source record in {folder} is invalid.")
    data = cast("dict[str, object]", raw)
    files = data.get("files")
    if data.get("version") != _MANIFEST_VERSION or not isinstance(files, dict):
        raise ConnectedError(f"The Connected Source record in {folder} is invalid.")
    owned: dict[str, _Owned] = {}
    for key, value in cast("dict[object, object]", files).items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        item = cast("dict[str, object]", value)
        revision, size = item.get("revision"), item.get("size")
        bundle = item.get("bundle", "")
        if (
            isinstance(revision, str)
            and isinstance(size, int)
            and isinstance(bundle, str)
            and _valid_key(key)
        ):
            owned[key] = _Owned(revision, size, bundle)
    return owned


def _save_manifest(folder: Path, owned: dict[str, _Owned]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _MANIFEST_VERSION,
        "files": {
            key: {"revision": value.revision, "size": value.size}
            | ({"bundle": value.bundle} if value.bundle else {})
            for key, value in sorted(owned.items())
        },
    }
    write_atomic(folder / MANIFEST_NAME, json.dumps(payload, indent=1).encode("utf-8"))


def _valid_key(key: str) -> bool:
    path = PurePosixPath(key)
    return (
        bool(key)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in key
        and all(part == safe_component(part, part) for part in path.parts)
    )


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=".write-", suffix=".part"
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _target(folder: Path, key: str) -> Path:
    if not _valid_key(key):
        raise ConnectedError(f"Refusing an unsafe file name: {key!r}")
    return folder.joinpath(*PurePosixPath(key).parts)


def refresh_folder(
    folder: Path,
    desired: Iterable[Desired],
    *,
    cancelled: Event,
    progress: Callable[[str, int, int], None] = lambda _t, _c, _n: None,
) -> RefreshReport:
    """Make ``folder``'s owned files match ``desired``. Unowned files are untouched."""

    items = list(desired)
    folded: set[str] = set()
    for item in items:
        key = item.relative
        if not _valid_key(key):
            raise ConnectedError(f"Refusing an unsafe file name: {key!r}")
        if key.casefold() in folded:
            raise ConnectedError(f"Two selected items would both be saved as {key}.")
        folded.add(key.casefold())
    owned = _load_manifest(folder)
    report = RefreshReport()
    for index, item in enumerate(items):
        if cancelled.is_set():
            _save_manifest(folder, owned)
            raise CancelledError
        if isinstance(item, DesiredBundle):
            _refresh_bundle(
                folder, item, owned, report, cancelled, index, len(items), progress
            )
        else:
            _refresh_file(folder, item, owned, report, index, len(items), progress)
        # Persist progress so an interrupted refresh resumes without re-downloading.
        if index % 25 == 0:
            _save_manifest(folder, owned)
    plain = {item.relative for item in items if not isinstance(item, DesiredBundle)}
    bundles = {item.relative for item in items if isinstance(item, DesiredBundle)}
    for key in sorted(owned):
        entry = owned[key]
        if (entry.bundle and entry.bundle in bundles) or (
            not entry.bundle and key in plain
        ):
            continue
        _remove_owned(folder, key, owned, report)
    _save_manifest(folder, owned)
    progress("", len(items), len(items))
    return report


def _refresh_file(
    folder: Path,
    item: DesiredDownload | DesiredBytes,
    owned: dict[str, _Owned],
    report: RefreshReport,
    index: int,
    total: int,
    progress: Callable[[str, int, int], None],
) -> None:
    target = _target(folder, item.relative)
    revision = (
        item.revision
        if isinstance(item, DesiredDownload)
        else hashlib.sha256(item.data).hexdigest()
    )
    previous = owned.get(item.relative)
    if (
        previous is not None
        and not previous.bundle
        and previous.revision == revision
        and target.is_file()
        and target.stat().st_size == previous.size
    ):
        report.kept += 1
        return
    if previous is None and target.exists():
        # Someone else's file already occupies this name; never overwrite it.
        report.failed.append(
            f"{item.relative}: a file you added already uses this name"
        )
        return
    progress(item.relative, index, total)
    try:
        if isinstance(item, DesiredBytes):
            write_atomic(target, item.data)
            report.written += 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            item.fetch(target)
            report.downloaded += 1
    except CancelledError:
        _save_manifest(folder, owned)
        raise
    except (ConnectedError, OSError) as error:
        report.failed.append(f"{item.relative}: {error}")
        return
    owned[item.relative] = _Owned(revision, target.stat().st_size)


def _refresh_bundle(
    folder: Path,
    item: DesiredBundle,
    owned: dict[str, _Owned],
    report: RefreshReport,
    cancelled: Event,
    index: int,
    total: int,
    progress: Callable[[str, int, int], None],
) -> None:
    members = {
        key: value for key, value in owned.items() if value.bundle == item.relative
    }
    if members and all(
        value.revision == item.revision
        and (target := _target(folder, key)).is_file()
        and target.stat().st_size == value.size
        for key, value in members.items()
    ):
        report.kept += 1
        return
    progress(item.relative, index, total)
    folder.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=folder, prefix=".bundle-"))
    try:
        try:
            item.fetch(staging)
        except CancelledError:
            _save_manifest(folder, owned)
            raise
        except (ConnectedError, OSError) as error:
            report.failed.append(f"{item.relative}: {error}")
            return
        produced: list[tuple[str, Path]] = []
        for path in sorted(staging.rglob("*")):
            if cancelled.is_set():
                raise CancelledError
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                report.failed.append(f"{item.relative}: unexpected item in download")
                return
            if path.is_file():
                inner = path.relative_to(staging).parts
                key = relative_path(item.relative, *inner)
                produced.append((key, path))
        if not produced:
            report.failed.append(
                f"{item.relative}: the download contained no usable files"
            )
            return
        for key, _path in produced:
            target = _target(folder, key)
            if key not in members and target.exists():
                report.failed.append(f"{key}: a file you added already uses this name")
                return
        for key in sorted(set(members) - {key for key, _ in produced}):
            _remove_owned(folder, key, owned, report, count=False)
        for key, path in produced:
            target = _target(folder, key)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, target)
            owned[key] = _Owned(item.revision, target.stat().st_size, item.relative)
        report.downloaded += 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _remove_owned(
    folder: Path,
    key: str,
    owned: dict[str, _Owned],
    report: RefreshReport,
    *,
    count: bool = True,
) -> None:
    target = _target(folder, key)
    try:
        if target.is_file() and target.stat().st_size == owned[key].size:
            target.unlink()
            if count:
                report.removed += 1
        elif target.exists():
            report.failed.append(f"{key}: changed outside iOpenPod, kept")
            return
    except OSError as error:
        report.failed.append(f"{key}: {error}")
        return
    del owned[key]
    _prune_empty(folder, target.parent)


def _prune_empty(root: Path, directory: Path) -> None:
    root = root.resolve()
    current = directory
    while True:
        try:
            resolved = current.resolve()
        except OSError:
            return
        if resolved == root or root not in resolved.parents:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def m3u8_playlist(name: str, entries: Iterable[tuple[str, int, str]]) -> bytes:
    """An extended M3U8 with relative paths (``#EXTINF`` seconds, title)."""

    lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    for relative, seconds, title in entries:
        lines.append(f"#EXTINF:{max(-1, seconds)},{' '.join(title.split())}")
        lines.append(relative)
    return ("\n".join(lines) + "\n").encode("utf-8")


__all__ = [
    "MANIFEST_NAME",
    "Desired",
    "DesiredBundle",
    "DesiredBytes",
    "DesiredDownload",
    "RefreshReport",
    "m3u8_playlist",
    "refresh_folder",
    "relative_path",
    "safe_component",
    "write_atomic",
]
