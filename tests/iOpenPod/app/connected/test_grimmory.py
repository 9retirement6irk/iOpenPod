"""Grimmory's native API: sign-in, browsing, downloads, and listening progress."""

import json
import struct
from collections.abc import Mapping
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from tests.iOpenPod.app.connected.support import FakeTransport, reply

from iOpenPod.app.connected.config import (
    ConnectedConfig,
    ConnectedConfigStore,
    GrimmoryConfig,
    GrimmoryMark,
    GrimmoryPick,
)
from iOpenPod.app.connected.grimmory import (
    GrimmoryClient,
    GrimmoryCollection,
    GrimmoryLink,
    GrimmoryProgress,
    GrimmoryTokens,
    parse_link,
    plan_grimmory,
)
from iOpenPod.app.connected.grimmory_progress import ipod_books, plan_progress
from iOpenPod.app.connected.http import ConnectedError, HttpResponse
from iOpenPod.app.connected.materialize import refresh_folder
from iPodDB.library import Track, TrackMetadata

BASE = "http://g"
TOKENS = {"accessToken": "a1", "refreshToken": "r1"}


def m4a() -> bytes:
    def box(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", 8 + len(payload)) + kind + payload

    mvhd = box(
        b"mvhd",
        bytes(4)
        + bytes(8)
        + struct.pack(">II", 1000, 5000)
        + struct.pack(">I", 0x00010000)
        + struct.pack(">H", 0x0100)
        + bytes(10)
        + bytes(36)
        + bytes(24)
        + struct.pack(">I", 2),
    )
    return box(b"ftyp", b"M4A " + bytes(4) + b"M4A isom") + box(b"moov", mvhd)


MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 400


def signed_in(transport: FakeTransport) -> GrimmoryClient:
    return GrimmoryClient(BASE, GrimmoryTokens("a1", "r1"), transport)


# --- sign-in -----------------------------------------------------------------------------


def test_sign_in_keeps_tokens_not_the_password() -> None:
    seen: list[GrimmoryTokens] = []
    bodies: list[object] = []

    def login(
        _m: str, _u: str, _h: Mapping[str, str], body: bytes | None
    ) -> HttpResponse:
        bodies.append(json.loads(body or b"{}"))
        return reply(TOKENS)

    transport = FakeTransport({f"POST {BASE}/api/v1/auth/login": login})
    client = GrimmoryClient(
        f"{BASE}/api/v1/", transport=transport, on_tokens=seen.append
    )
    assert client.sign_in(" reader ", "pw") == GrimmoryTokens("a1", "r1")
    assert bodies == [{"username": "reader", "password": "pw"}]
    assert seen == [GrimmoryTokens("a1", "r1")]


def test_rejected_password_explains_single_sign_on() -> None:
    transport = FakeTransport(
        {f"POST {BASE}/api/v1/auth/login": ConnectedError("HTTP 401", 401)}
    )
    with pytest.raises(ConnectedError, match="single sign-on"):
        GrimmoryClient(BASE, transport=transport).sign_in("reader", "bad")


def test_expired_access_token_is_refreshed_once() -> None:
    seen: list[GrimmoryTokens] = []
    attempts: list[str] = []

    def libraries(
        _m: str, _u: str, headers: Mapping[str, str], _b: bytes | None
    ) -> HttpResponse:
        attempts.append(headers["Authorization"])
        if headers["Authorization"] == "Bearer a1":
            raise ConnectedError("HTTP 401", 401)
        return reply([{"id": 1, "name": "Audio", "bookCount": 3}])

    transport = FakeTransport(
        {
            f"GET {BASE}/api/v1/libraries": libraries,
            f"GET {BASE}/api/v1/app/shelves": [{"id": 4, "name": "Commute"}],
            f"POST {BASE}/api/v1/auth/refresh": {
                "accessToken": "a2",
                "refreshToken": "r2",
            },
        }
    )
    client = GrimmoryClient(
        BASE, GrimmoryTokens("a1", "r1"), transport, on_tokens=seen.append
    )
    assert client.collections() == (
        (
            GrimmoryCollection("library", "1", "Audio", 3),
            GrimmoryCollection("shelf", "4", "Commute", 0),
        ),
        (),
    )
    assert attempts == ["Bearer a1", "Bearer a2"]
    assert seen == [GrimmoryTokens("a2", "r2")]


def test_expired_refresh_token_asks_to_sign_in_again() -> None:
    transport = FakeTransport(
        {
            f"GET {BASE}/api/v1/libraries": ConnectedError("HTTP 401", 401),
            f"POST {BASE}/api/v1/auth/refresh": ConnectedError("HTTP 401", 401),
        }
    )
    with pytest.raises(ConnectedError, match="Sign in again"):
        signed_in(transport).collections()


def test_a_failing_list_does_not_undo_sign_in() -> None:
    """Grimmory 3.5's app library list answers HTTP 500; shelves still show."""

    error = ConnectedError(
        "The server answered HTTP 500: An unexpected error occurred.", 500
    )
    transport = FakeTransport(
        {
            f"GET {BASE}/api/v1/libraries": error,
            f"GET {BASE}/api/v1/app/libraries": error,
            f"GET {BASE}/api/v1/app/shelves": [{"id": 4, "name": "Commute"}],
        }
    )
    collections, issues = signed_in(transport).collections()
    assert collections == (GrimmoryCollection("shelf", "4", "Commute", 0),)
    (issue,) = issues
    assert "listing libraries" in issue and "HTTP 500" in issue


def test_single_file_download_falls_back_to_the_player_stream(tmp_path: Path) -> None:
    from iOpenPod.app.connected.grimmory import AudiobookInfo

    transport = FakeTransport()
    transport.files[f"{BASE}/api/v1/audiobooks/7/stream"] = b"audio"
    info = AudiobookInfo("7", "70", "Dune", 5_000, False, ())
    signed_in(transport).download_book(info, tmp_path / "book")
    assert (tmp_path / "book").read_bytes() == b"audio"
    assert [call[1] for call in transport.calls] == [
        f"{BASE}/api/v1/books/7/files/70/download",
        f"{BASE}/api/v1/audiobooks/7/stream?bookType=AUDIOBOOK",
    ]


# --- browsing ----------------------------------------------------------------------------


def test_audiobooks_page_through_one_collection() -> None:
    pages = {
        "0": {
            "content": [
                {"id": 7, "title": "Dune", "authors": ["Frank Herbert"]},
                {"id": 9, "title": "Folder Book", "seriesName": "Saga"},
            ],
            "hasNext": True,
        },
        "1": {"content": [{"id": 11, "title": ""}], "hasNext": False},
    }
    urls: list[str] = []

    def books(
        _m: str, url: str, _h: Mapping[str, str], _b: bytes | None
    ) -> HttpResponse:
        urls.append(url)
        page = url.split("page=")[1].split("&")[0]
        return reply(pages[page])

    transport = FakeTransport({f"GET {BASE}/api/v1/app/books": books})
    found = signed_in(transport).audiobooks(
        GrimmoryCollection("shelf", "4", "Commute"), "du"
    )
    assert [(b.book_id, b.title, b.author, b.series) for b in found] == [
        ("7", "Dune", "Frank Herbert", ""),
        ("9", "Folder Book", "", "Saga"),
        ("11", "Untitled audiobook", "", ""),
    ]
    assert all("fileType=AUDIOBOOK" in u and "shelfId=4" in u for u in urls)
    assert "search=du" in urls[0]


# --- downloads ---------------------------------------------------------------------------


def downloads() -> FakeTransport:
    transport = FakeTransport(
        {
            f"GET {BASE}/api/v1/audiobooks/9/info": {
                "bookId": 9,
                "bookFileId": 90,
                "title": "Folder Book",
                "durationMs": 120_000,
                "folderBased": True,
                "tracks": [
                    {
                        "index": 0,
                        "fileName": "a.mp3",
                        "title": "Opening",
                        "durationMs": 60_000,
                        "cumulativeStartMs": 0,
                    },
                    {
                        "index": 1,
                        "fileName": "b.mp3",
                        "title": "Ending",
                        "durationMs": 60_000,
                        "cumulativeStartMs": 60_000,
                    },
                ],
            },
            f"GET {BASE}/api/v1/audiobooks/7/info": {
                "bookId": 7,
                "bookFileId": 70,
                "title": "Dune",
                "durationMs": 5_000,
                "folderBased": False,
            },
            f"GET {BASE}/api/v1/app/books/7": {
                "id": 7,
                "files": [{"id": 70, "fileName": "Dune.m4a"}],
            },
            f"GET {BASE}/api/v1/audiobooks/9/cover": reply(
                body=b"\xff\xd8\xff\xe0cover", content_type="image/jpeg"
            ),
            f"GET {BASE}/api/v1/audiobooks/7/cover": ConnectedError("HTTP 404", 404),
        }
    )
    transport.files[f"{BASE}/api/v1/audiobooks/9/track/"] = MP3
    transport.files[f"{BASE}/api/v1/books/7/files/70/download"] = m4a()
    return transport


def test_downloads_are_tagged_with_their_grimmory_link(tmp_path: Path) -> None:
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4

    transport = downloads()
    config = GrimmoryConfig(
        picks=(
            GrimmoryPick("9", "Folder Book"),
            GrimmoryPick("7", "Dune", "Frank Herbert"),
        )
    )
    report = refresh_folder(
        tmp_path,
        plan_grimmory(signed_in(transport), config, cancelled=Event()),
        cancelled=Event(),
    )
    assert report.downloaded == 2, report.failed
    folder = tmp_path / "Folder Book"
    assert sorted(p.name for p in folder.iterdir()) == [
        "01 Opening.mp3",
        "02 Ending.mp3",
        "cover.jpg",
    ]
    tags: Any = ID3(folder / "02 Ending.mp3")  # type: ignore[no-untyped-call]
    assert tags.getall("TXXX:media_type")[0].text == ["audiobook"]
    assert tags.getall("COMM")[0].text == ["grimmory:9:90:1:60000:120000"]
    book = MP4(tmp_path / "Frank Herbert - Dune" / "Dune.m4b")  # type: ignore[no-untyped-call]
    assert book["stik"] == [2]
    assert book["\xa9cmt"] == ["grimmory:7:70:0:0:5000"]
    assert not (tmp_path / "Frank Herbert - Dune" / "cover.jpg").exists()
    assert {call[1] for call in transport.calls if call[0] == "DOWNLOAD"} == {
        f"{BASE}/api/v1/audiobooks/9/track/0/stream?bookType=AUDIOBOOK",
        f"{BASE}/api/v1/audiobooks/9/track/1/stream?bookType=AUDIOBOOK",
        f"{BASE}/api/v1/books/7/files/70/download",
    }


def test_links_round_trip_through_comments() -> None:
    link = GrimmoryLink("9", "90", 1, 60_000, 120_000)
    assert parse_link(f"Notes. {link.comment}") == link
    assert parse_link("an ordinary comment") is None


# --- progress over the API ---------------------------------------------------------------


def test_progress_is_read_and_saved_like_grimmorys_player() -> None:
    posted: list[object] = []

    def save(
        _m: str, _u: str, _h: Mapping[str, str], body: bytes | None
    ) -> HttpResponse:
        posted.append(json.loads(body or b"{}"))
        return HttpResponse(200, b"", "application/json")

    transport = FakeTransport(
        {
            f"GET {BASE}/api/v1/app/books/9/progress": {
                "audiobookProgress": {
                    "positionMs": 90_000,
                    "trackIndex": 1,
                    "percentage": 75.0,
                    "updatedAt": "2026-10-30T12:00:00Z",
                }
            },
            f"POST {BASE}/api/v1/books/progress": save,
        }
    )
    client = signed_in(transport)
    progress = client.progress("9")
    assert (progress.position_ms, progress.track_index) == (90_000, 1)
    assert progress.updated == 1_793_361_600
    client.save_progress(GrimmoryLink("9", "90", 1, 60_000, 120_000), 90_000, 1, 30_000)
    assert posted == [
        {
            "bookId": 9,
            "audiobookProgress": {
                "positionMs": 90_000,
                "trackPositionMs": 30_000,
                "percentage": 75.0,
                "trackIndex": 1,
            },
            "fileProgress": {
                "bookFileId": 90,
                "positionData": "90000",
                "progressPercent": 75.0,
                "positionHref": "1",
            },
        }
    ]


# --- exchanging progress -----------------------------------------------------------------


def book_track(
    track_id: int,
    index: int,
    start: int,
    *,
    bookmark: int = 0,
    played: bool = False,
    last_played: int = 0,
) -> Track:
    link = GrimmoryLink("9", "90", index, start, 120_000)
    return Track(
        track_id,
        f"Part {index + 1}",
        "Author",
        "Folder Book",
        60_000,
        metadata=TrackMetadata(
            comment=link.comment,
            bookmark_time_ms=bookmark,
            played=played,
            last_played=last_played,
            remember_position=True,
        ),
    )


def remote(position: int, updated: int = 0) -> dict[str, GrimmoryProgress | None]:
    return {"9": GrimmoryProgress(position, None, 0.0, updated)}


def test_ipod_listening_moves_grimmory() -> None:
    books = ipod_books(
        (
            book_track(1, 0, 0, played=True, last_played=100),
            book_track(2, 1, 60_000, bookmark=30_000, last_played=200),
            Track(3, "Song", "Artist", "Album", 1_000),
        )
    )
    plan = plan_progress(
        books, remote(10_000), {"9": GrimmoryMark("9", 10_000, 10_000)}
    )
    assert plan.to_ipod == ()
    (push,) = plan.to_grimmory
    assert (push.position_ms, push.track_index, push.track_position_ms) == (
        90_000,
        1,
        30_000,
    )
    assert plan.marks == (GrimmoryMark("9", 90_000, 90_000),)


def test_grimmory_listening_moves_the_ipod_bookmark() -> None:
    books = ipod_books(
        (
            book_track(1, 0, 0, bookmark=10_000, last_played=100),
            book_track(2, 1, 60_000),
        )
    )
    plan = plan_progress(
        books, remote(75_000, 500), {"9": GrimmoryMark("9", 10_000, 10_000)}
    )
    assert plan.to_grimmory == ()
    (pull,) = plan.to_ipod
    assert {change.track_id: change.edits for change in pull.changes} == {
        1: (("metadata.bookmark_time_ms", 0), ("metadata.played", True)),
        2: (("metadata.bookmark_time_ms", 15_000), ("metadata.last_played", 500)),
    }
    # The iPod hasn't moved until the person saves the Library changes.
    assert plan.marks == (GrimmoryMark("9", 10_000, 75_000),)
    # Not saved: the next comparison offers the same move again rather than undoing it.
    again = plan_progress(
        books, remote(75_000, 500), {m.book_id: m for m in plan.marks}
    )
    assert again.to_grimmory == ()
    assert again.to_ipod[0].position_ms == 75_000


def test_when_both_moved_the_latest_listen_wins() -> None:
    books = ipod_books((book_track(1, 0, 0, bookmark=40_000, last_played=1_000),))
    mark = {"9": GrimmoryMark("9", 10_000, 10_000)}
    assert plan_progress(books, remote(20_000, 900), mark).to_grimmory
    assert plan_progress(books, remote(20_000, 1_100), mark).to_ipod


def test_matching_positions_and_missing_books() -> None:
    books = ipod_books((book_track(1, 0, 0, bookmark=40_000),))
    plan = plan_progress(books, remote(42_000), {})
    assert (plan.to_grimmory, plan.to_ipod, plan.unchanged) == ((), (), 1)
    gone = plan_progress(books, {"9": None}, {})
    assert gone.missing == 1 and not gone.marks


def test_send_only_leaves_grimmory_moves_for_later() -> None:
    books = ipod_books((book_track(1, 0, 0, bookmark=10_000),))
    plan = plan_progress(
        books, remote(75_000, 5), {"9": GrimmoryMark("9", 10_000, 10_000)}, pull=False
    )
    assert (plan.to_grimmory, plan.to_ipod, plan.marks) == ((), (), ())


def test_marks_and_progress_choice_are_saved(tmp_path: Path) -> None:
    from storage import AtomicHostFile

    store = ConnectedConfigStore(AtomicHostFile(tmp_path / "connected.json"))
    config = ConnectedConfig(
        grimmory=GrimmoryConfig(
            url=BASE,
            username="reader",
            picks=(GrimmoryPick("9", "Folder Book"),),
            sync_progress=False,
            marks=(GrimmoryMark("9", 1_000, 2_000),),
        )
    )
    store.save(config)
    assert store.load().grimmory == config.grimmory
    assert "password" not in (tmp_path / "connected.json").read_text()
