"""Match iPod Tracks to Plex Tracks by metadata, conservatively.

Plays and ratings are only exchanged with a Plex Track that is the single
confident candidate. Ambiguous or missing matches are reported, never guessed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .plex import PlexTrack

DURATION_TOLERANCE_MS = 3_000
_BRACKETS = re.compile(
    r"[\(\[][^\)\]]*(?:remaster|version|edit|mono|stereo|deluxe|bonus)[^\)\]]*[\)\]]",
    re.I,
)
_FEAT = re.compile(r"\s+(?:feat\.?|featuring|ft\.?)\s+.*$", re.I)
_PUNCT = re.compile(r"[^\w]+")


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKD", text).casefold()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = _BRACKETS.sub(" ", value)
    value = _FEAT.sub("", value)
    value = value.replace("&", " and ")
    value = _PUNCT.sub(" ", value).strip()
    return re.sub(r"^the ", "", value)


@dataclass(frozen=True, slots=True)
class MatchResult:
    track: PlexTrack | None
    ambiguous: bool = False


class PlexIndex:
    def __init__(self, tracks: Iterable[PlexTrack]) -> None:
        self._by_title: dict[str, list[PlexTrack]] = {}
        for track in tracks:
            self._by_title.setdefault(normalize(track.title), []).append(track)

    def match(
        self, title: str, artist: str, album: str, duration_ms: int
    ) -> MatchResult:
        candidates = self._by_title.get(normalize(title), [])
        if not candidates:
            return MatchResult(None)
        wanted_artist, wanted_album = normalize(artist), normalize(album)

        def artist_ok(track: PlexTrack) -> bool:
            names = {normalize(track.artist), normalize(track.album_artist)}
            return not wanted_artist or wanted_artist in names

        def duration_ok(track: PlexTrack) -> bool:
            return (
                duration_ms <= 0
                or track.duration_ms <= 0
                or abs(track.duration_ms - duration_ms) <= DURATION_TOLERANCE_MS
            )

        for narrowed in (
            [
                t
                for t in candidates
                if artist_ok(t)
                and normalize(t.album) == wanted_album
                and duration_ok(t)
            ],
            [
                t
                for t in candidates
                if artist_ok(t) and normalize(t.album) == wanted_album
            ],
            [t for t in candidates if artist_ok(t) and duration_ok(t)],
        ):
            if len(narrowed) == 1:
                return MatchResult(narrowed[0])
            if len(narrowed) > 1:
                # Several copies of one recording on the server: same album and
                # duration means any of them is the same song; pick the first.
                albums = {normalize(t.album) for t in narrowed}
                if len(albums) == 1 and all(duration_ok(t) for t in narrowed):
                    return MatchResult(narrowed[0])
                return MatchResult(None, ambiguous=True)
        return MatchResult(None)


__all__ = ["MatchResult", "PlexIndex", "normalize"]
