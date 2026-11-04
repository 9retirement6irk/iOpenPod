"""Plex movie and TV libraries chosen in setup join Sync as videos."""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from threading import Event

from tests.iOpenPod.app.connected.support import FakeTransport
from tests.iOpenPod.app.connected.test_plex import container
from tests.iOpenPod.app.connected.test_remote_source import (
    JPEG,
    SERVER,
    StubPlex,
)
from tests.iOpenPod.app.test_sync_plan import _host_library

from iOpenPod.app.connected.config import PlexVideoUse
from iOpenPod.app.connected.http import ConnectedError
from iOpenPod.app.connected.plex import PlexClient, PlexSection, PlexTrack
from iOpenPod.app.connected.remote_source import ConnectedRemote, video_relative
from iOpenPod.app.host_media_library import HostMediaFileKind, HostMediaSource
from iOpenPod.app.remote_media import RemoteCatalog, merge_remote
from iPodDB.library import MediaKind, MediaType


def part(key: str, size: int = 9_000) -> list[dict[str, object]]:
    return [
        {
            "container": "mkv",
            "Part": [
                {
                    "key": f"/library/parts/{key}/file.mkv",
                    "file": f"/v/{key}.mkv",
                    "size": size,
                }
            ],
        }
    ]


MOVIE = {
    "ratingKey": "10",
    "type": "movie",
    "title": "Heat",
    "year": 1995,
    "summary": "A heist.",
    "duration": 10_000,
    "thumb": "/poster/10",
    "Genre": [{"tag": "Crime"}],
    "updatedAt": 5,
    "Media": part("10"),
}
CLIP = {
    "ratingKey": "11",
    "type": "movie",
    "title": "Radiohead - Karma Police",
    "year": 1997,
    "duration": 4_000,
    "thumb": "/poster/11",
    "updatedAt": 5,
    "Media": part("11"),
}
EPISODE = {
    "ratingKey": "20",
    "type": "episode",
    "title": "Pilot",
    "grandparentTitle": "The Office",
    "parentTitle": "Season 1",
    "parentIndex": 1,
    "index": 2,
    "year": 2005,
    "summary": "Day one.",
    "duration": 1_300_000,
    "parentThumb": "/season/1",
    "updatedAt": 7,
    "Media": part("20"),
}


def test_video_libraries_are_listed_for_their_use() -> None:
    sections = f"GET {SERVER.uri}/library/sections"
    transport = FakeTransport(
        {
            sections: container(
                [],
                Directory=[
                    {"key": "1", "type": "artist", "title": "Music"},
                    {"key": "3", "type": "movie", "title": "Films"},
                    {"key": "4", "type": "movie", "title": "Clips"},
                    {"key": "5", "type": "show", "title": "TV"},
                    {"key": "6", "type": "photo", "title": "Photos"},
                ],
            ),
            f"{sections}/3/all?type=1": container([MOVIE]),
            f"{sections}/4/all?type=1": container([CLIP]),
            f"{sections}/5/all?type=4": container([EPISODE]),
        }
    )
    client = PlexClient("t", transport)
    assert client.sections(SERVER) == (
        PlexSection("1", "Music", "artist"),
        PlexSection("3", "Films", "movie"),
        PlexSection("4", "Clips", "movie"),
        PlexSection("5", "TV", "show"),
    )
    assert client.music_sections(SERVER) == (("1", "Music"),)
    movie, clip, episode = client.library_videos(
        SERVER,
        {
            "3": PlexVideoUse.MOVIES.value,
            "4": PlexVideoUse.MUSIC_VIDEOS.value,
            "5": PlexVideoUse.TV_SHOWS.value,
        },
        Event(),
    )
    assert (movie.title, movie.year, movie.media, movie.section) == (
        "Heat",
        1995,
        "movies",
        "3",
    )
    assert (movie.genre, movie.summary, movie.thumb) == (
        "Crime",
        "A heist.",
        "/poster/10",
    )
    assert (clip.artist, clip.title, clip.media) == (
        "Radiohead",
        "Karma Police",
        "music_videos",
    )
    assert (episode.show, episode.disc_number, episode.track_number) == (
        "The Office",
        1,
        2,
    )
    assert episode.thumb == "/season/1" and episode.extension == ".mkv"
    # Libraries not chosen are never read.
    assert client.library_videos(SERVER, {}, Event()) == ()
    assert not any("/sections/1/all" in call[1] for call in transport.calls)


def test_videos_are_cached_one_folder_each_and_seasons_together() -> None:
    from iOpenPod.app.connected.plex import parse_video

    movie = parse_video(MOVIE, "movies")
    clip = parse_video(CLIP, "music_videos")
    episode = parse_video(EPISODE, "tv_shows")
    assert movie is not None and clip is not None and episode is not None
    assert video_relative(movie) == "Movies/Heat (1995) [10]/Heat (1995).mkv"
    assert video_relative(clip) == (
        "Music Videos/Radiohead - Karma Police [11]/Radiohead - Karma Police.mkv"
    )
    assert video_relative(episode) == (
        "TV Shows/The Office/Season 01/S01E02 - Pilot [20].mkv"
    )


class VideoPlex(StubPlex):
    def __init__(self, videos: tuple[PlexTrack, ...]) -> None:
        super().__init__(())
        self.videos = videos
        self.uses: dict[str, str] = {}

    def library_videos(
        self, _server: object, uses: Mapping[str, str], _cancelled: Event
    ) -> tuple[PlexTrack, ...]:
        self.uses = dict(uses)
        return tuple(v for v in self.videos if uses.get(v.section) == v.media)

    def playlist_tracks(
        self, _server: object, _key: str, _cancelled: Event
    ) -> tuple[PlexTrack, ...]:
        return ()


def videos() -> tuple[PlexTrack, ...]:
    from iOpenPod.app.connected.plex import parse_video

    found = (
        replace(video, section=section)
        for row, use, section in (
            (MOVIE, "movies", "3"),
            (EPISODE, "tv_shows", "5"),
        )
        if (video := parse_video(row, use)) is not None
    )
    return tuple(found)


def test_chosen_video_libraries_become_remote_videos(tmp_path: Path) -> None:
    client = VideoPlex(videos())
    uses: Iterable[tuple[str, str]] = (("3", "movies"), ("5", "tv_shows"))
    remote = ConnectedRemote(
        tmp_path / "Connected",
        tmp_path / "catalog.json",
        plex=client,  # type: ignore[arg-type]
        server=SERVER,
        plex_videos=dict(uses),
    )
    catalog = remote.catalog(Event())
    movie, episode = catalog.items
    assert client.uses == {"3": "movies", "5": "tv_shows"}
    assert movie.media_type is MediaType.VIDEO and movie.video
    assert movie.description == "A heist." and movie.size_bytes == 9_000
    assert movie.cover is not None and Path(movie.cover.path.path).read_bytes() == JPEG
    assert episode.media_type is MediaType.TV_SHOW
    assert (episode.show, episode.season_number, episode.episode_number) == (
        "The Office",
        1,
        2,
    )

    library = merge_remote(_host_library((), (), ()), catalog)
    by_title = {track.title: track for track in library.snapshot.tracks}
    assert by_title["Heat"].media_kind is MediaKind.MOVIE
    office = by_title["Pilot"]
    assert office.media_kind is MediaKind.TV_SHOW
    assert (
        office.show,
        office.episode,
        office.season_number,
        office.episode_number,
    ) == (
        "The Office",
        "S01E02",
        1,
        2,
    )
    assert office.metadata.description == "Day one."
    assert {source.kind for source in library.sources} == {HostMediaFileKind.VIDEO}

    # Downloads keep the video kind, so Sync converts them as video.
    source = next(s for s in library.sources if s.path == movie.path)
    got = remote.materialize(source, lambda: None, lambda _p: None)
    assert got.kind is HostMediaFileKind.VIDEO and got.remote
    assert isinstance(got, HostMediaSource)

    # Offline, the last listing is shown, still limited to the chosen libraries.
    client.offline = True

    def unreachable(*_args: object, **_kwargs: object) -> tuple[PlexTrack, ...]:
        from iOpenPod.app.connected.http import ConnectedError

        raise ConnectedError("Could not reach plex")

    client.library_videos = unreachable  # type: ignore[method-assign]
    only_movies = ConnectedRemote(
        tmp_path / "Connected",
        tmp_path / "catalog.json",
        plex=client,  # type: ignore[arg-type]
        server=SERVER,
        plex_videos={"3": "movies"},
    )
    offline = only_movies.catalog(Event())
    assert [item.title for item in offline.items] == ["Heat"]
    assert offline.issues
    assert RemoteCatalog() == RemoteCatalog()


def clip(key: str, title: str, subtype: str) -> dict[str, object]:
    return {
        "ratingKey": key,
        "type": "clip",
        "title": title,
        "subtype": subtype,
        "extraType": 4,
        "duration": 200_000,
        "thumb": f"/thumb/{key}",
        "updatedAt": 3,
        "Media": part(key),
    }


def test_music_videos_attached_to_artists_and_songs_are_found() -> None:
    """Plex keeps videos from its Global music videos path as artist and song
    extras; music, lyric and live music videos are kept, other extras aren't."""

    from iOpenPod.app.connected.plex import PlexTrack

    metadata = f"GET {SERVER.uri}/library/metadata"
    transport = FakeTransport(
        {
            f"{metadata}/100/extras": container(
                [
                    clip("901", "Radiohead - Karma Police", "musicVideo"),
                    clip("902", "No Surprises (Lyrics)", "lyricMusicVideo"),
                    clip("903", "Interview 1997", "interview"),
                    clip("904", "Live at Glastonbury", "concert"),
                ]
            ),
            f"{metadata}/200/extras": ConnectedError("HTTP 500", 500),
            f"{metadata}/905,901": container(
                [
                    clip("905", "Paranoid Android", "liveMusicVideo"),
                    clip("901", "Radiohead - Karma Police", "musicVideo"),
                ]
            ),
        }
    )

    def song(key: str, artist: str, artist_key: str, extra: str = "") -> PlexTrack:
        return PlexTrack(
            key,
            "Song",
            artist,
            artist,
            "Album",
            1,
            1,
            1_000,
            f"/library/parts/{key}/a.flac",
            "",
            1,
            "flac",
            1,
            artist_key=artist_key,
            section="1",
            extra_key=extra,
        )

    found = PlexClient("t", transport).music_videos(
        SERVER,
        (
            song("1", "Radiohead", "100", "/library/metadata/905"),
            song("2", "Radiohead", "100", "/library/metadata/901"),
            song("3", "Broken Server", "200"),  # its extras fail; the rest still come
        ),
        Event(),
    )
    by_key = {video.rating_key: video for video in found}
    assert set(by_key) == {"901", "902", "905"}
    karma = by_key["901"]
    assert (karma.artist, karma.title, karma.media) == (
        "Radiohead",
        "Karma Police",
        "music_videos",
    )
    assert karma.extra and karma.section == "1" and karma.thumb == "/thumb/901"
    assert by_key["905"].title == "Paranoid Android"
    assert video_relative(karma) == (
        "Music Videos/Radiohead - Karma Police [901]/Radiohead - Karma Police.mkv"
    )


class ExtrasPlex(VideoPlex):
    def __init__(self, extras: tuple[PlexTrack, ...]) -> None:
        super().__init__(())
        self.extras = extras
        self.asked = 0

    def music_videos(
        self, _server: object, _tracks: object, _cancelled: Event
    ) -> tuple[PlexTrack, ...]:
        self.asked += 1
        return self.extras


def test_music_videos_join_sync_only_when_switched_on(tmp_path: Path) -> None:
    from iOpenPod.app.connected.plex import parse_video

    video = parse_video(clip("901", "Karma Police", "musicVideo"), "music_videos")
    assert video is not None
    extra = replace(video, artist="Radiohead", section="1", extra=True)
    client = ExtrasPlex((extra,))

    def remote(enabled: bool, skip: tuple[str, ...] = ()) -> ConnectedRemote:
        return ConnectedRemote(
            tmp_path / "Connected",
            tmp_path / "catalog.json",
            plex=client,  # type: ignore[arg-type]
            server=SERVER,
            plex_skip=skip,
            plex_music_videos=enabled,
        )

    assert remote(False).catalog(Event()).items == ()
    assert client.asked == 0
    (item,) = remote(True).catalog(Event()).items
    assert item.media_type is MediaType.MUSIC_VIDEO and item.artist == "Radiohead"
    assert item.video

    # Offline, the remembered videos follow the switch and the music libraries.
    def unreachable(*_args: object, **_kwargs: object) -> tuple[PlexTrack, ...]:
        raise ConnectedError("Could not reach plex")

    client.library_tracks = unreachable  # type: ignore[method-assign]
    assert [i.title for i in remote(True).catalog(Event()).items] == ["Karma Police"]
    assert remote(False).catalog(Event()).items == ()
    assert remote(True, skip=("1",)).catalog(Event()).items == ()
