"""Publish iPod Extras (Notes and Calendars) through one Storage Transaction.

iPod Extras are plain files the firmware reads from top-level ``Notes`` and
``Calendars`` folders. They are not Library media, so they are published here
rather than through Sync. A versioned Extras Manifest at
``iPod_Control/iOpenPod/extras-v1.json`` records the files iOpenPod owns, with
their SHA-256, so a later publication:

* writes new and changed owned files,
* removes owned files that are no longer wanted, only if their bytes are unchanged,
* never overwrites or removes a file it did not write.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from storage import (
    DeviceEntryKind,
    DevicePath,
    FileContent,
    StorageError,
    StorageTransaction,
    TransactionRemoval,
    TransactionWrite,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from storage import FileFingerprint, FilesystemSession

MANIFEST_PATH = DevicePath("iPod_Control/iOpenPod/extras-v1.json")
_MANIFEST_VERSION = 1
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_EXTRAS_FILES = 5_000


class ExtrasSection(StrEnum):
    NOTES = "notes"
    CALENDARS = "calendars"

    @property
    def folder(self) -> str:
        return "Notes" if self is ExtrasSection.NOTES else "Calendars"


@dataclass(frozen=True, slots=True)
class ExtrasFile:
    relative: str  # "/"-separated, beneath the section's folder
    data: bytes


@dataclass(frozen=True, slots=True)
class ExtrasResult:
    written: int
    unchanged: int
    removed: int
    issues: tuple[str, ...] = ()


@dataclass(slots=True)
class PreparedExtras:
    transaction: StorageTransaction | None
    directories: tuple[DevicePath, ...]
    emptied: tuple[DevicePath, ...]
    written: int = 0
    unchanged: int = 0
    removed: int = 0
    issues: list[str] = field(default_factory=list[str])


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_manifest(session: FilesystemSession) -> dict[str, dict[str, str]]:
    """Owned files per section: ``{section: {device path: sha256}}``."""

    if not session.exists(MANIFEST_PATH):
        return {}
    try:
        raw: object = json.loads(
            session.read(MANIFEST_PATH, max_bytes=_MAX_MANIFEST_BYTES)
        )
    except ValueError:
        return {}
    if not isinstance(raw, dict) or raw.get("version") != _MANIFEST_VERSION:
        return {}
    sections = cast("dict[str, object]", raw).get("sections")
    if not isinstance(sections, dict):
        return {}
    owned: dict[str, dict[str, str]] = {}
    for name, files in cast("dict[object, object]", sections).items():
        if not isinstance(name, str) or not isinstance(files, dict):
            continue
        owned[name] = {
            path: digest
            for path, digest in cast("dict[object, object]", files).items()
            if isinstance(path, str)
            and isinstance(digest, str)
            and len(digest) == 64
            and _owned_path_ok(path, name)
        }
    return owned


def _owned_path_ok(path: str, section: str) -> bool:
    try:
        parts = DevicePath(path).parts
    except ValueError:
        return False
    try:
        expected = ExtrasSection(section).folder.casefold()
    except ValueError:
        return False
    return len(parts) >= 2 and parts[0].casefold() == expected


def _section_root(session: FilesystemSession, section: ExtrasSection) -> DevicePath:
    """The existing top-level folder with this name in any capitalization."""

    for entry in session.list_root():
        if (
            entry.kind is DeviceEntryKind.DIRECTORY
            and entry.path.name.casefold() == section.folder.casefold()
        ):
            return entry.path
    return DevicePath(section.folder)


def count_foreign_files(session: FilesystemSession, section: ExtrasSection) -> int:
    """Files in the section folder that iOpenPod did not write (the iPod's limit counts them)."""

    root = _section_root(session, section)
    if not session.exists(root):
        return 0
    owned = {path.casefold() for path in read_manifest(session).get(section.value, {})}
    count = 0
    pending = [root]
    while pending and count < MAX_EXTRAS_FILES:
        for entry in session.list_directory(pending.pop()):
            if entry.kind is DeviceEntryKind.DIRECTORY:
                pending.append(entry.path)
            elif (
                not entry.path.name.startswith(".")
                and str(entry.path).casefold() not in owned
            ):
                count += 1
    return count


def prepare_extras(
    session: FilesystemSession,
    section: ExtrasSection,
    files: tuple[ExtrasFile, ...],
    *,
    checkpoint: Callable[[], None],
) -> PreparedExtras:
    if len(files) > MAX_EXTRAS_FILES:
        raise ValueError("Too many iPod Extras files in one publication.")
    root = _section_root(session, section)
    manifest = read_manifest(session)
    owned = dict(manifest.get(section.value, {}))
    writes: list[TransactionWrite] = []
    removals: list[TransactionRemoval] = []
    directories: set[DevicePath] = set()
    desired: dict[str, ExtrasFile] = {}
    result = PreparedExtras(None, (), ())
    for item in files:
        checkpoint()
        path = DevicePath.from_parts((*root.parts, *DevicePath(item.relative).parts))
        key = str(path)
        if key.casefold() in {existing.casefold() for existing in desired}:
            result.issues.append(f"{item.relative}: duplicate name, skipped")
            continue
        desired[key] = item
        digest = _sha(item.data)
        content = FileContent(len(item.data), digest)
        owned_key = next((k for k in owned if k.casefold() == key.casefold()), None)
        if session.exists(path):
            current = session.fingerprint(path)
            if owned_key is None:
                result.issues.append(
                    f"{key}: a file you added already uses this name, left alone"
                )
                continue
            if current.sha256 == digest:
                result.unchanged += 1
                owned.pop(owned_key)
                owned[key] = digest
                continue
            if current.sha256 != owned[owned_key]:
                result.issues.append(
                    f"{key}: changed on the iPod since it was written, left alone"
                )
                continue
            writes.append(TransactionWrite(path, item.data, content, expected=current))
            owned.pop(owned_key)
        else:
            writes.append(TransactionWrite(path, item.data, content))
            if owned_key is not None:
                owned.pop(owned_key)
        owned[key] = digest
        parent = path.parent
        while parent is not None and parent.parts:
            directories.add(parent)
            parent = parent.parent
        result.written += 1
    emptied: set[DevicePath] = set()
    wanted = {key.casefold() for key in desired}
    for key, digest in list(owned.items()):
        checkpoint()
        if key.casefold() in wanted:
            continue
        path = DevicePath(key)
        owned.pop(key)
        if not session.exists(path):
            continue
        current = session.fingerprint(path)
        if current.sha256 != digest:
            result.issues.append(
                f"{key}: changed on the iPod since it was written, left alone"
            )
            continue
        removals.append(TransactionRemoval(path, current))
        result.removed += 1
        parent = path.parent
        while parent is not None and len(parent.parts) > 1:
            emptied.add(parent)
            parent = parent.parent
    manifest[section.value] = owned
    manifest_bytes = json.dumps(
        {"version": _MANIFEST_VERSION, "sections": manifest}, indent=1, sort_keys=True
    ).encode("utf-8")
    manifest_current: FileFingerprint | None = (
        session.fingerprint(MANIFEST_PATH) if session.exists(MANIFEST_PATH) else None
    )
    if (
        writes
        or removals
        or manifest_current is None
        or manifest_current.sha256 != _sha(manifest_bytes)
    ):
        writes.append(
            TransactionWrite(
                MANIFEST_PATH,
                manifest_bytes,
                FileContent(len(manifest_bytes), _sha(manifest_bytes)),
                expected=manifest_current,
            )
        )
        directories.add(DevicePath("iPod_Control/iOpenPod"))
        # The manifest write carries its expected fingerprint, so a concurrent
        # change aborts the transaction before anything is published.
        result.transaction = StorageTransaction(tuple(writes), tuple(removals))
        session.validate_transaction(result.transaction)
    result.directories = tuple(sorted(directories, key=lambda item: len(item.parts)))
    result.emptied = tuple(sorted(emptied, key=lambda item: -len(item.parts)))
    return result


def remove_empty_folders(
    session: FilesystemSession, folders: tuple[DevicePath, ...]
) -> None:
    """After commit, tidy folders that publication emptied (deepest first)."""

    for folder in folders:
        try:
            if session.exists(folder) and not session.list_directory(folder):
                session.remove_empty_directory(folder)
        except StorageError:
            continue


__all__ = [
    "MANIFEST_PATH",
    "ExtrasFile",
    "ExtrasResult",
    "ExtrasSection",
    "PreparedExtras",
    "count_foreign_files",
    "prepare_extras",
    "read_manifest",
    "remove_empty_folders",
]
