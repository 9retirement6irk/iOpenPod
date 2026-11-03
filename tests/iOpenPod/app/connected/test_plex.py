"""Plex sign-in, browsing, Selection layout, mapped copies, plays, and ratings."""

from pathlib import Path
from threading import Event
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest
from tests.iOpenPod.app.connected.support import FakeTransport, reply

from iOpenPod.app.connected.config import (
    PathMapping,
    PlexConfig,
    PlexPick,
    PlexPickKind,
)
from iOpenPod.app.connected.http import ConnectedError
from iOpenPod.app.connected.materialize import (
    DesiredBytes,
    DesiredDownload,
    refresh_folder,
)
from iOpenPod.app.connected.plex import (
    PlexClient,
    PlexServer,
    PlexServerChoice,
    PlexTrack,
    ipod_player,
    map_server_path,
    parse_track,
)
from iOpenPod.app.connected.plex_match import PlexIndex
from iOpenPod.app.connected.plex_ratings import plan_ratings, plex_as_ipod
from iOpenPod.app.connected.plex_source import plan_plex, track_relative
from iPodDB.library import Track

SERVER = PlexServer("machine", "Media", "http://plex.local:32400", "server-token")


def row(
    key: str,
    title: str,
    *,
    album: str = "OK Computer",
    artist: str = "Radiohead",
    index: int = 1,
    duration: int = 200_000,
    file: str = "/mnt/media/music/a.flac",
    size: int = 3,
    rating: float | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "ratingKey": key,
        "type": "track",
        "title": title,
        "grandparentTitle": artist,
        "parentTitle": album,
        "index": index,
        "parentIndex": 1,
        "duration": duration,
        "updatedAt": 10,
        "parentThumb": f"/library/metadata/{album}/thumb/5",
        "Media": [
            {
                "container": "flac",
                "Part": [
                    {
                        "key": f"/library/parts/{key}/1/file.flac",
                        "file": file,
                        "size": size,
                    }
                ],
            }
        ],
    }
    if rating is not None:
        data["userRating"] = rating
    return data


def container(rows: list[dict[str, object]], **extra: object) -> dict[str, object]:
    return {"MediaContainer": {"Metadata": rows, "size": len(rows), **extra}}


def test_pin_sign_in_and_server_choice_prefers_local_addresses() -> None:
    transport = FakeTransport(
        {
            "POST https://plex.tv/api/v2/pins": {"id": 7, "code": "abcd"},
            "GET https://plex.tv/api/v2/pins/7": {"authToken": "account-token"},
            "GET https://plex.tv/api/v2/user": {"title": "will"},
            "GET https://clients.plex.tv/api/v2/resources": [
                {
                    "provides": "server",
                    "clientIdentifier": "machine",
                    "name": "Media",
                    "owned": True,
                    "accessToken": "server-token",
                    "connections": [
                        {"uri": "https://relay.plex.direct", "relay": True},
                        {
                            "uri": "https://10-0-0-2.plex.direct:32400",
                            "local": True,
                            "address": "10.0.0.2",
                            "port": 32400,
                        },
                    ],
                },
                {"provides": "player", "clientIdentifier": "tv"},
            ],
            "GET https://10-0-0-2.plex.direct:32400/identity": ConnectedError(
                "HTTP 500", 500
            ),
            "GET http://10.0.0.2:32400/identity": {
                "MediaContainer": {"machineIdentifier": "machine"}
            },
        }
    )
    client = PlexClient("iopenpod-test", transport, clock=lambda: 100.0)
    pin = client.start_sign_in()
    assert "clientID=iopenpod-test" in pin.auth_url and "code=abcd" in pin.auth_url
    assert client.poll_sign_in(pin) == ("account-token", "will")
    (choice,) = client.servers("account-token")
    assert (
        choice.candidates[0].startswith("https://10-0-0-2")
        and choice.candidates[-1] == "https://relay.plex.direct"
    )
    server = client.connect(choice)
    assert server.uri == "http://10.0.0.2:32400" and server.token == "server-token"
    expired = PlexClient("x", transport, clock=lambda: 100_000.0)
    with pytest.raises(ConnectedError, match="expired"):
        expired.poll_sign_in(pin)


def test_selection_layout_dedupes_tracks_and_writes_relative_playlists(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(
        {
            f"GET {SERVER.uri}/playlists/55/items": container(
                [row("1", "Airbag", index=1), row("2", "Paranoid Android", index=2)]
            ),
            f"GET {SERVER.uri}/library/metadata/9/children": container(
                [row("1", "Airbag"), row("3", "Lucky", index=11)]
            ),
        }
    )
    transport.files[f"{SERVER.uri}/library/parts/"] = b"abc"
    transport.files[f"{SERVER.uri}/photo/:/transcode"] = b"\xff\xd8\xffcover"
    client = PlexClient("t", transport)
    config = PlexConfig(
        picks=(
            PlexPick(PlexPickKind.PLAYLIST, "55", "Road Trip"),
            PlexPick(PlexPickKind.ALBUM, "9", "OK Computer"),
        )
    )
    plan = plan_plex(client, SERVER, config, cancelled=Event())
    assert (plan.tracks, plan.playlists) == (3, 1)
    names = [item.relative for item in plan.items]
    assert "Radiohead/OK Computer/01 Airbag [1].flac" in names
    assert "Radiohead/OK Computer/cover.jpg" in names
    assert names[-1] == "Playlists/Road Trip.m3u8"
    playlist = cast("DesiredBytes", plan.items[-1]).data.decode()
    assert "../Radiohead/OK Computer/02 Paranoid Android [2].flac" in playlist
    report = refresh_folder(tmp_path, plan.items, cancelled=Event())
    assert report.downloaded == 4 and report.written == 1
    downloads = [
        call
        for call in transport.calls
        if call[0] == "DOWNLOAD" and "/library/parts/" in call[1]
    ]
    assert len(downloads) == 3
    assert all(
        "download=1" in call[1] and call[2]["X-Plex-Token"] == "server-token"
        for call in downloads
    )


def test_path_mapping_copies_local_files_instead_of_downloading(tmp_path: Path) -> None:
    share = tmp_path / "Volumes" / "Media" / "music"
    share.mkdir(parents=True)
    (share / "a.flac").write_bytes(b"abc")
    track = parse_track(row("1", "Airbag", file="/mnt/media/music/a.flac", size=3))
    assert track is not None
    transport = FakeTransport()
    client = PlexClient("t", transport)
    target = tmp_path / "out.flac"
    client.fetch_track(
        SERVER,
        track,
        target,
        mappings=(PathMapping("/mnt/media", str(tmp_path / "Volumes" / "Media")),),
    )
    assert target.read_bytes() == b"abc" and not transport.calls
    assert (
        map_server_path(
            "/mnt/media/music/x/../../etc", (PathMapping("/mnt/media", "/V"),)
        )
        is None
    )
    assert (
        map_server_path(r"D:\Music\a.mp3", (PathMapping("D:/Music", r"M:\Music"),))
        is not None
    )


def test_record_play_reports_a_finished_listen_as_the_ipod() -> None:
    track = parse_track(row("1", "Airbag", duration=10_000, size=1000))
    assert track is not None
    transport = FakeTransport(
        {
            f"POST {SERVER.uri}/playQueues": {
                "MediaContainer": {"playQueueID": 4, "playQueueSelectedItemID": 8}
            },
            f"GET {SERVER.uri}/:/timeline": {},
            f"GET {SERVER.uri}/library/parts/": reply(
                body=b"x", content_type="audio/flac"
            ),
        }
    )
    client = PlexClient("t", transport)
    player = ipod_player("serial-1", "Will's iPod")
    client.record_play(SERVER, track, player, pause=lambda _seconds: None)
    timeline = [
        parse_qs(urlsplit(url).query)
        for method, url, _h in transport.calls
        if "/:/timeline" in url
    ]
    assert [entry["state"][0] for entry in timeline][-2:] == ["playing", "stopped"]
    assert timeline[-1]["time"] == ["10000"] and timeline[0]["containerKey"] == [
        "/playQueues/4"
    ]
    headers = next(h for _m, url, h in transport.calls if "/:/timeline" in url)
    assert (
        headers["X-Plex-Platform"] == "iPod"
        and headers["X-Plex-Device-Name"] == "Will's iPod"
    )
    assert (
        headers["X-Plex-Client-Identifier"]
        == ipod_player("serial-1", "x")["X-Plex-Client-Identifier"]
    )
    ranges = [h["Range"] for _m, url, h in transport.calls if "/library/parts/" in url]
    assert ranges[0].startswith("bytes=0-") and ranges[-1].startswith("bytes=950-")


def test_record_play_falls_back_to_scrobble_when_timeline_is_unsupported() -> None:
    track = parse_track(row("1", "Airbag"))
    assert track is not None
    transport = FakeTransport(
        {
            f"POST {SERVER.uri}/playQueues": ConnectedError("HTTP 400", 400),
            f"GET {SERVER.uri}/:/timeline": ConnectedError("HTTP 404", 404),
            f"GET {SERVER.uri}/:/scrobble": {},
        }
    )
    PlexClient("t", transport).record_play(SERVER, track, {}, pause=lambda _s: None)
    assert any("/:/scrobble?" in url for _m, url, _h in transport.calls)


def plex(
    key: str,
    title: str,
    artist: str,
    album: str,
    duration: int,
    rating: float | None = None,
) -> PlexTrack:
    track = parse_track(
        row(key, title, artist=artist, album=album, duration=duration, rating=rating)
    )
    assert track is not None
    return track


def test_matching_is_conservative() -> None:
    index = PlexIndex(
        [
            plex("1", "Intro", "The xx", "xx", 127_000),
            plex("2", "Intro", "M83", "Hurry Up", 300_000),
            plex("3", "Song (2011 Remaster)", "Band", "Album", 200_000),
            plex("4", "Live", "Band", "A", 100_000),
            plex("5", "Live", "Band", "B", 180_000),
        ]
    )
    assert index.match("Intro", "xx", "xx", 127_500).track is not None
    assert index.match("intro", "The xx", "", 128_000).track.rating_key == "1"  # type: ignore[union-attr]
    assert index.match("Song", "band", "album", 200_000).track is not None
    ambiguous = index.match("Live", "Band", "C", 0)
    assert ambiguous.track is None and ambiguous.ambiguous
    assert index.match("Missing", "Band", "A", 0).track is None


def ipod(track_id: int, title: str, rating: int) -> Track:
    return Track(
        track_id=track_id,
        title=title,
        artist="Band",
        album="Album",
        length_ms=200_000,
        rating=rating,
    )


def test_ratings_ipod_wins_plex_fills_blanks_half_stars_do_not_flip() -> None:
    plexes = [
        plex("1", "A", "Band", "Album", 200_000, rating=4.0),  # differs: iPod 5★ wins
        plex("2", "B", "Band", "Album", 200_000, rating=6.0),  # Plex only → iPod 3★
        plex(
            "3", "C", "Band", "Album", 200_000, rating=7.0
        ),  # 3.5★ ≈ 4★ on iPod: no change
        plex("4", "D", "Band", "Album", 200_000),
    ]
    plan = plan_ratings(
        [
            ipod(10, "A", 100),
            ipod(11, "B", 0),
            ipod(12, "C", 80),
            ipod(13, "D", 0),
            ipod(14, "E", 60),
        ],
        plexes,
    )
    assert [(c.track.rating_key, c.rating) for c in plan.to_plex] == [("1", 10.0)]
    assert [(c.track_id, c.rating) for c in plan.to_ipod] == [(11, 60)]
    assert plan.unmatched == 1
    assert (
        plex_as_ipod(7.0) == 80 and plex_as_ipod(1.0) == 20 and plex_as_ipod(None) == 0
    )


def test_download_items_carry_refresh_revisions() -> None:
    track = plex("1", "A", "Band", "Album", 1000)
    assert track_relative(track) == "Band/Album/01 A [1].flac"
    transport = FakeTransport(
        {f"GET {SERVER.uri}/library/metadata/1": container([row("1", "A")])}
    )
    plan = plan_plex(
        PlexClient("t", transport),
        SERVER,
        PlexConfig(picks=(PlexPick(PlexPickKind.TRACK, "1", "A"),)),
        cancelled=Event(),
    )
    item, cover = plan.items
    assert isinstance(item, DesiredDownload) and item.revision.endswith("|3|10")
    assert cover.relative == "Radiohead/OK Computer/cover.jpg"


def test_choice_dataclass_is_hashable() -> None:
    assert hash(PlexServerChoice("a", "b", True, "t", ("u",)))
