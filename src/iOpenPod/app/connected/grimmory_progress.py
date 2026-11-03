"""Exchange audiobook listening progress between iPod Tracks and Grimmory.

Each audiobook file iOpenPod downloads from Grimmory carries a Grimmory Link in its
comment, so the iPod Track knows its book, track index and where the track starts
inside the whole book. The iPod's position in a book is the bookmark of the track
listened to most recently, plus that track's start.

A Progress Mark remembers, per book, the iPod and Grimmory positions seen the last
time the two sides agreed. Whichever side moved since then wins; when both moved,
the more recent listen wins, or the further position when the iPod has no time.
Grimmory positions are applied to the iPod as Library changes, which the person
saves with Review Changes, so nothing writes the iPod behind their back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .grimmory import GrimmoryLink, GrimmoryProgress, parse_link

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from iPodDB.library import Track

    from .config import GrimmoryMark

SAME_POSITION_MS = 5_000
type FieldValue = int | bool


@dataclass(frozen=True, slots=True)
class BookTrack:
    track_id: int
    link: GrimmoryLink
    length_ms: int
    bookmark_ms: int
    last_played: int
    played: bool
    remember_position: bool


@dataclass(frozen=True, slots=True)
class IPodBook:
    book_id: str
    title: str
    tracks: tuple[BookTrack, ...]  # ordered by start

    @property
    def position(self) -> tuple[int, int, BookTrack | None]:
        """``(book position ms, last listened Unix time, track)`` on the iPod."""

        started = [t for t in self.tracks if t.bookmark_ms > 0 or t.played]
        if not started:
            return 0, 0, None
        recent = max(started, key=lambda t: (t.last_played, _absolute(t)))
        return _absolute(recent), recent.last_played, recent


def _absolute(track: BookTrack) -> int:
    if track.bookmark_ms > 0:
        return track.link.start_ms + track.bookmark_ms
    return track.link.start_ms + track.length_ms  # finished this track


@dataclass(frozen=True, slots=True)
class ProgressPush:
    """Send the iPod's position to Grimmory."""

    title: str
    link: GrimmoryLink
    position_ms: int
    track_index: int | None
    track_position_ms: int


@dataclass(frozen=True, slots=True)
class TrackChange:
    track_id: int
    edits: tuple[tuple[str, FieldValue], ...]


@dataclass(frozen=True, slots=True)
class ProgressPull:
    """Move the iPod's bookmark to Grimmory's position."""

    title: str
    book_id: str
    position_ms: int
    changes: tuple[TrackChange, ...]


@dataclass(frozen=True, slots=True)
class ProgressPlan:
    to_grimmory: tuple[ProgressPush, ...]
    to_ipod: tuple[ProgressPull, ...]
    # Marks to record once the plan is applied, by book id.
    marks: tuple[GrimmoryMark, ...]
    unchanged: int
    missing: int  # books on the iPod that Grimmory no longer has


def ipod_books(tracks: Iterable[Track]) -> tuple[IPodBook, ...]:
    grouped: dict[str, list[BookTrack]] = {}
    titles: dict[str, str] = {}
    for track in tracks:
        link = parse_link(track.metadata.comment)
        if link is None:
            continue
        grouped.setdefault(link.book_id, []).append(
            BookTrack(
                track.track_id,
                link,
                track.length_ms,
                track.metadata.bookmark_time_ms,
                track.metadata.last_played,
                track.metadata.played,
                track.metadata.remember_position,
            )
        )
        titles.setdefault(link.book_id, track.album or track.title)
    return tuple(
        IPodBook(
            book_id,
            titles[book_id],
            tuple(sorted(entries, key=lambda t: (t.link.start_ms, t.link.track_index))),
        )
        for book_id, entries in sorted(grouped.items())
    )


def _differs(a: int, b: int) -> bool:
    return abs(a - b) > SAME_POSITION_MS


def _push(book: IPodBook, position: int, track: BookTrack) -> ProgressPush:
    folder_based = len(book.tracks) > 1 or track.link.track_index > 0
    return ProgressPush(
        book.title,
        track.link,
        position,
        track.link.track_index if folder_based else None,
        position - track.link.start_ms,
    )


def _pull(book: IPodBook, position: int, updated: int) -> ProgressPull:
    target = book.tracks[0]
    for track in book.tracks:
        if track.link.start_ms <= position:
            target = track
    changes: list[TrackChange] = []
    for track in book.tracks:
        edits: list[tuple[str, FieldValue]] = []
        if track is target:
            offset = max(0, position - track.link.start_ms)
            if track.length_ms:
                offset = min(offset, max(0, track.length_ms - 1_000))
            edits.append(("metadata.bookmark_time_ms", offset))
            if updated > track.last_played:
                edits.append(("metadata.last_played", updated))
            if not track.remember_position:
                edits.append(("metadata.remember_position", True))
            if track.played and offset < track.length_ms - 1_000:
                edits.append(("metadata.played", False))
        elif track.link.start_ms < target.link.start_ms:
            if track.bookmark_ms:
                edits.append(("metadata.bookmark_time_ms", 0))
            if not track.played:
                edits.append(("metadata.played", True))
        else:
            if track.bookmark_ms:
                edits.append(("metadata.bookmark_time_ms", 0))
            if track.played:
                edits.append(("metadata.played", False))
        if edits:
            changes.append(TrackChange(track.track_id, tuple(edits)))
    return ProgressPull(book.title, book.book_id, position, tuple(changes))


def plan_progress(
    books: Iterable[IPodBook],
    remote: Mapping[str, GrimmoryProgress | None],
    marks: Mapping[str, GrimmoryMark],
    *,
    pull: bool = True,
) -> ProgressPlan:
    """Decide which side moves for each book; ``remote[book] is None`` means gone."""

    from .config import GrimmoryMark

    pushes: list[ProgressPush] = []
    pulls: list[ProgressPull] = []
    new_marks: list[GrimmoryMark] = []
    unchanged = missing = 0
    for book in books:
        if book.book_id not in remote:
            continue
        theirs = remote[book.book_id]
        if theirs is None:
            missing += 1
            continue
        ours, listened, track = book.position
        mark = marks.get(book.book_id)
        if not _differs(ours, theirs.position_ms):
            unchanged += 1
            new_marks.append(GrimmoryMark(book.book_id, ours, theirs.position_ms))
            continue
        if mark is None:
            ipod_moved, grimmory_moved = ours > 0, theirs.position_ms > 0
        else:
            ipod_moved = _differs(ours, mark.ipod_ms)
            grimmory_moved = _differs(theirs.position_ms, mark.grimmory_ms)
        if ipod_moved and grimmory_moved:
            if listened and theirs.updated:
                send = listened > theirs.updated
            else:
                send = ours > theirs.position_ms
        else:
            # Neither moved yet they differ: an earlier pull was never saved to the iPod.
            send = ipod_moved
        if send and track is not None:
            pushes.append(_push(book, ours, track))
            new_marks.append(GrimmoryMark(book.book_id, ours, ours))
        elif not send and pull:
            pulls.append(_pull(book, theirs.position_ms, theirs.updated))
            # Remember the iPod as seen now: until the pull is saved it hasn't moved.
            new_marks.append(GrimmoryMark(book.book_id, ours, theirs.position_ms))
    return ProgressPlan(
        tuple(pushes), tuple(pulls), tuple(new_marks), unchanged, missing
    )


__all__ = [
    "SAME_POSITION_MS",
    "BookTrack",
    "IPodBook",
    "ProgressPlan",
    "ProgressPull",
    "ProgressPush",
    "TrackChange",
    "ipod_books",
    "plan_progress",
]
