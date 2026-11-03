"""Plan two-way star ratings between the Active iPod Library and Plex.

Rules, compared at the iPod's whole-star precision:

* A song rated on the iPod sends its stars to Plex when Plex differs (the iPod wins).
* A song rated only in Plex gets those stars on the iPod; Plex half-stars round to
  the nearest whole star, while Plex keeps its own half-star.
* Copies of one song on the iPod are treated together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from iPodDB.library import MediaKind

from .plex_match import PlexIndex

if TYPE_CHECKING:
    from collections.abc import Iterable

    from iPodDB.library import Track

    from .plex import PlexTrack


@dataclass(frozen=True, slots=True)
class RatingToPlex:
    track: PlexTrack
    rating: float  # Plex 0-10 scale
    title: str


@dataclass(frozen=True, slots=True)
class RatingToIPod:
    track_id: int
    rating: int  # iPod 0-100 scale
    title: str


@dataclass(frozen=True, slots=True)
class RatingPlan:
    to_plex: tuple[RatingToPlex, ...]
    to_ipod: tuple[RatingToIPod, ...]
    unmatched: int
    ambiguous: int


def plex_as_ipod(rating: float | None) -> int:
    if rating is None or rating <= 0:
        return 0
    return min(100, max(20, int(rating / 2 + 0.5) * 20))


def plan_ratings(
    tracks: Iterable[Track], plex_tracks: Iterable[PlexTrack]
) -> RatingPlan:
    index = PlexIndex(plex_tracks)
    groups: dict[str, tuple[PlexTrack, list[Track]]] = {}
    unmatched = ambiguous = 0
    for track in tracks:
        if track.media_kind not in (MediaKind.MUSIC, MediaKind.MUSIC_VIDEO):
            continue
        match = index.match(track.title, track.artist, track.album, track.length_ms)
        if match.track is None:
            if match.ambiguous:
                ambiguous += 1
            else:
                unmatched += 1
            continue
        groups.setdefault(match.track.rating_key, (match.track, []))[1].append(track)
    to_plex: list[RatingToPlex] = []
    to_ipod: list[RatingToIPod] = []
    for plex_track, copies in groups.values():
        ipod = max(_whole(copy.rating) for copy in copies)
        plex = plex_as_ipod(plex_track.user_rating)
        if ipod > 0:
            if plex != ipod:
                to_plex.append(RatingToPlex(plex_track, ipod / 10, plex_track.title))
            target = ipod
        elif plex > 0:
            target = plex
        else:
            continue
        to_ipod.extend(
            RatingToIPod(copy.track_id, target, copy.title)
            for copy in copies
            if _whole(copy.rating) != target
        )
    return RatingPlan(tuple(to_plex), tuple(to_ipod), unmatched, ambiguous)


def _whole(rating: int) -> int:
    """iPod ratings are stored in steps of 20; tolerate stray values."""

    if rating <= 0:
        return 0
    return min(100, max(20, round(rating / 20) * 20))


__all__ = ["RatingPlan", "RatingToIPod", "RatingToPlex", "plan_ratings", "plex_as_ipod"]
