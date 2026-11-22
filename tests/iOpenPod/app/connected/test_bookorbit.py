"""BookOrbit's REST API: API key, audiobook list, downloads, and listening progress.

``OrbitServer`` answers the way BookOrbit's server does (server/src/modules/audiobook
in bookorbit/bookorbit): playback state is keyed by asset, guarded by a revision
(409 on a stale base) and a manifest revision (412 when the files changed), and a
read-only API key is refused for anything but reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from threading import Event
from typing import TYPE_CHECKING, Any

import pytest
from tests.iOpenPod.app.connected.support import FakeTransport, reply
from tests.iOpenPod.app.connected.test_grimmory import MP3, m4a
from tests.iOpenPod.app.connected.test_remote_source import JPEG, source

from iOpenPod.app.connected.bookorbit import (
    AUDIO_FORMATS,
    BookOrbitClient,
    manifest_from,
)
from iOpenPod.app.connected.config import (
    ConnectedConfig,
    ConnectedConfigStore,
    GrimmoryMark,
)
from iOpenPod.app.connected.grimmory import GrimmoryLink, parse_link
from iOpenPod.app.connected.grimmory_progress import ipod_books, plan_progress
from iOpenPod.app.connected.http import ConnectedError, HttpResponse
from iOpenPod.app.connected.remote_source import ConnectedRemote
from iPodDB.library import MediaType, Track, TrackMetadata

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from iOpenPod.app.connected_controller import ConnectedController

BASE = "http://orbit:3000"
KEY = "bok_" + "A" * 49
ASSET_A = "aud_00000000-0000-4000-8000-00000000000a"
ASSET_B = "aud_00000000-0000-4000-8000-00000000000b"


@dataclass
class OrbitServer:
    """BookOrbit's audiobook endpoints, enough to exercise iOpenPod's client."""

    read_only: bool = False
    revision: str = "a" * 64
    # book 5: two MP3 parts of 60 s; book 6: one M4B.
    assets: dict[int, list[dict[str, object]]] = field(
        default_factory=lambda: {
            5: [
                {
                    "assetId": ASSET_A,
                    "fileId": 51,
                    "sequence": 0,
                    "format": "mp3",
                    "durationMs": 60_000,
                    "sizeBytes": len(MP3),
                    "etag": "e51",
                },
                {
                    "assetId": ASSET_B,
                    "fileId": 52,
                    "sequence": 1,
                    "format": "mp3",
                    "durationMs": 60_000,
                    "sizeBytes": len(MP3),
                    "etag": "e52",
                },
            ],
            6: [
                {
                    "assetId": "aud_00000000-0000-4000-8000-000000000006",
                    "fileId": 61,
                    "sequence": 0,
                    "format": "m4a",
                    "durationMs": 5_000,
                    "sizeBytes": 100,
                    "etag": "e61",
                }
            ],
        }
    )
    states: dict[int, dict[str, Any]] = field(default_factory=dict[int, dict[str, Any]])
    queries: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def transport(self) -> FakeTransport:
        transport = FakeTransport(
            {
                f"GET {BASE}/api/v1/auth/me": self._me,
                f"POST {BASE}/api/v1/books/query": self._query,
                f"GET {BASE}/api/v1/audiobooks/": self._get,
                f"PUT {BASE}/api/v1/audiobooks/": self._put,
                f"GET {BASE}/api/v1/books/5/cover": reply(
                    body=JPEG, content_type="image/jpeg"
                ),
            }
        )
        transport.files[f"{BASE}/api/v1/audiobooks/5/assets/"] = MP3
        transport.files[f"{BASE}/api/v1/audiobooks/6/assets/"] = m4a()
        return transport

    @staticmethod
    def _authorized(headers: Mapping[str, str]) -> None:
        if headers.get("X-Api-Key") != KEY:
            raise ConnectedError("HTTP 401", 401)

    def _me(
        self, _m: str, _u: str, headers: Mapping[str, str], _b: bytes | None
    ) -> HttpResponse:
        self._authorized(headers)
        return reply({"username": "will"})

    def _query(
        self, _m: str, _u: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResponse:
        self._authorized(headers)
        query = json.loads(body or b"{}")
        self.queries.append(query)
        rule = query["filter"]["rules"][0]
        assert (rule["field"], rule["operator"]) == ("format", "includesAny")
        page, size = query["pagination"]["page"], query["pagination"]["size"]
        assert size <= 200
        books = [
            {
                "id": 5,
                "title": "Two Parts",
                "authors": ["A. Writer"],
                "updatedAt": "u5",
            },
            {"id": 6, "title": "One File", "authors": [], "updatedAt": "u6"},
        ]
        size = 1  # a server that pages one book at a time
        return reply(
            {
                "items": books[page * size : (page + 1) * size],
                "total": 2,
                "page": page,
                "size": size,
            }
        )

    def manifest(self, book: int) -> dict[str, object]:
        assets = self.assets[book]
        return {
            "schema": "bookorbit.audiobook-manifest",
            "schemaVersion": 2,
            "revision": self.revision,
            "book": {
                "id": book,
                "title": {5: "Two Parts", 6: "One File"}[book],
                "authors": ["A. Writer"] if book == 5 else [],
                "narrators": ["N. Reader"],
                "hasCover": book == 5,
            },
            "assets": assets,
            "chapters": [],
            "totalDurationMs": sum(int(str(a["durationMs"])) for a in assets),
        }

    def _get(
        self, _m: str, url: str, headers: Mapping[str, str], _b: bytes | None
    ) -> HttpResponse:
        self._authorized(headers)
        book = int(url.split("/audiobooks/")[1].split("/")[0])
        if book not in self.assets:
            raise ConnectedError("HTTP 404", 404)
        if url.endswith("/manifest"):
            return reply(self.manifest(book))
        state = self.states.get(book)
        if state is None:
            return HttpResponse(200, b"null", "application/json")
        return reply(state)

    def _put(
        self, _m: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResponse:
        self._authorized(headers)
        if self.read_only:
            raise ConnectedError("This API key is read-only (HTTP 403)", 403)
        book = int(url.split("/audiobooks/")[1].split("/")[0])
        dto = json.loads(body or b"{}")
        datetime.fromisoformat(dto["capturedAt"].replace("Z", "+00:00"))
        if dto["manifestRevision"] != self.revision:
            raise ConnectedError("Audiobook manifest has changed", 412)
        ids = [a["assetId"] for a in self.assets[book]]
        if dto["assetId"] not in ids:
            raise ConnectedError("assetId does not belong to this audiobook", 400)
        asset = self.assets[book][ids.index(dto["assetId"])]
        if dto["positionMs"] > int(str(asset["durationMs"])) + 1_000:
            raise ConnectedError("positionMs exceeds the asset duration", 400)
        previous = self.states.get(book)
        if (previous["revision"] if previous else 0) != dto["baseRevision"]:
            raise ConnectedError("Playback state revision conflict", 409)
        self.states[book] = {
            "assetId": dto["assetId"],
            "positionMs": dto["positionMs"],
            "percentage": 0,
            "completed": False,
            "capturedAt": dto["capturedAt"],
            "revision": (previous["revision"] if previous else 0) + 1,
            "manifestRevision": self.revision,
        }
        return reply(self.states[book])


def client(
    server: OrbitServer, transport: FakeTransport | None = None
) -> BookOrbitClient:
    return BookOrbitClient(f"{BASE}/api/v1/", KEY, transport or server.transport())


# --- account and browsing -------------------------------------------------------------


def test_the_api_key_is_sent_as_a_header_and_checked() -> None:
    server = OrbitServer()
    transport = server.transport()
    assert client(server, transport).username() == "will"
    method, url, headers = transport.calls[0]
    assert (method, url, headers["X-Api-Key"]) == ("GET", f"{BASE}/api/v1/auth/me", KEY)
    with pytest.raises(ConnectedError, match="didn't accept the API key") as caught:
        BookOrbitClient(BASE, "bok_wrong", server.transport()).username()
    assert caught.value.status == 401
    with pytest.raises(ConnectedError, match="API key first"):
        BookOrbitClient(BASE, " ", server.transport()).username()


def test_audiobooks_are_listed_page_by_page_with_the_audio_filter() -> None:
    server = OrbitServer()
    books = client(server).audiobooks()
    assert [(b.book_id, b.title, b.author, b.updated) for b in books] == [
        ("5", "Two Parts", "A. Writer", "u5"),
        ("6", "One File", "", "u6"),
    ]
    assert [q["pagination"]["page"] for q in server.queries] == [0, 1]
    assert server.queries[0]["filter"]["rules"][0]["value"] == list(AUDIO_FORMATS)


def test_the_manifest_places_each_file_in_the_book() -> None:
    manifest = client(OrbitServer()).manifest("5")
    assert manifest.starts() == (0, 60_000)
    assert manifest.total_ms == 120_000
    assert (manifest.authors, manifest.narrators) == ("A. Writer", "N. Reader")
    assert manifest.link(1) == GrimmoryLink("5", "52", 1, 60_000, 120_000, "bookorbit")
    assert manifest.locate(90_000) == (1, 30_000)
    assert manifest.locate(500_000) == (1, 60_000)  # past the end: end of last part
    # Assets arrive in any order and are played by their sequence.
    shuffled = OrbitServer().manifest(5)
    shuffled["assets"] = list(reversed(shuffled["assets"]))  # type: ignore[call-overload]
    assert [a.file_id for a in manifest_from(shuffled).assets] == ["51", "52"]


def test_links_are_told_apart_from_grimmory_links() -> None:
    orbit = GrimmoryLink("5", "52", 1, 60_000, 120_000, "bookorbit")
    assert orbit.comment == "bookorbit:5:52:1:60000:120000"
    assert parse_link(orbit.comment, "bookorbit") == orbit
    assert parse_link(orbit.comment) is None  # not a Grimmory book
    both = f"grimmory:9:90:0:0:5 {orbit.comment}"
    assert parse_link(both, "bookorbit") == orbit
    assert parse_link(both) == GrimmoryLink("9", "90", 0, 0, 5)


# --- downloads ----------------------------------------------------------------------------


def test_audiobooks_join_sync_and_download_tagged(tmp_path: Path) -> None:
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4

    server = OrbitServer()
    transport = server.transport()
    remote = ConnectedRemote(
        tmp_path / "Connected",
        tmp_path / "catalog.json",
        bookorbit=client(server, transport),
    )
    one, two, single = remote.catalog(Event()).items
    folder = tmp_path / "Connected" / "BookOrbit"
    assert (
        one.path.path.relative_to(folder).as_posix()
        == "A. Writer - Two Parts/01 Two Parts.mp3"
    )
    assert single.path.path.relative_to(folder).as_posix() == "One File/One File.m4b"
    assert (
        one.media_type,
        one.album,
        one.artist,
        one.track_number,
        one.total_tracks,
    ) == (
        MediaType.AUDIOBOOK,
        "Two Parts",
        "A. Writer",
        1,
        2,
    )
    assert (one.title, single.title) == ("Two Parts (1/2)", "One File")
    assert two.comment == "bookorbit:5:52:1:60000:120000"
    assert two.cover is not None and single.cover is None  # only book 5 has one

    got = remote.materialize(source(two), lambda: None, lambda _p: None)
    tags: Any = ID3(got.path.path)  # type: ignore[no-untyped-call]
    assert tags.getall("COMM")[0].text == ["bookorbit:5:52:1:60000:120000"]
    assert tags.getall("TXXX:media_type")[0].text == ["audiobook"]
    got = remote.materialize(source(single), lambda: None, lambda _p: None)
    book: Any = MP4(got.path.path)  # type: ignore[no-untyped-call]
    assert (book["stik"], book["\xa9cmt"]) == ([2], ["bookorbit:6:61:0:0:5000"])
    assert {c[1] for c in transport.calls if c[0] == "DOWNLOAD"} == {
        f"{BASE}/api/v1/audiobooks/5/assets/{ASSET_B}/content",
        f"{BASE}/api/v1/audiobooks/6/assets/aud_00000000-0000-4000-8000-000000000006/content",
    }

    # Manifests are remembered until the book changes.
    def manifests() -> int:
        return sum(c[1].endswith("/manifest") for c in transport.calls)

    assert manifests() == 2
    ConnectedRemote(
        tmp_path / "Connected",
        tmp_path / "catalog.json",
        bookorbit=client(server, transport),
    ).catalog(Event())
    assert manifests() == 2
    # A file replaced in BookOrbit is a new revision: the cached copy is stale.
    server.assets[5][1] = {**server.assets[5][1], "etag": "e52-new"}
    original = server._query

    def changed(*args: Any) -> HttpResponse:
        response = original(*args)
        data = json.loads(response.body)
        for row in data["items"]:
            row["updatedAt"] += "-new"
        return reply(data)

    transport.routes[f"POST {BASE}/api/v1/books/query"] = changed
    relisted = ConnectedRemote(
        tmp_path / "Connected",
        tmp_path / "catalog.json",
        bookorbit=client(server, transport),
    ).catalog(Event())
    assert manifests() == 4
    assert not relisted.items[1].cached


def test_an_unreachable_server_shows_the_last_list(tmp_path: Path) -> None:
    server = OrbitServer()
    transport = server.transport()
    args = (tmp_path / "Connected", tmp_path / "catalog.json")
    assert (
        len(
            ConnectedRemote(*args, bookorbit=client(server, transport))
            .catalog(Event())
            .items
        )
        == 3
    )
    transport.routes[f"POST {BASE}/api/v1/books/query"] = ConnectedError(
        "HTTP 502", 502
    )
    catalog = ConnectedRemote(*args, bookorbit=client(server, transport)).catalog(
        Event()
    )
    assert len(catalog.items) == 3
    assert "Showing your audiobooks as last listed" in catalog.issues[0]


# --- listening progress ----------------------------------------------------------------


def test_progress_is_a_position_in_the_whole_book() -> None:
    server = OrbitServer()
    orbit = client(server)
    assert orbit.progress("5").position_ms == 0  # never listened
    server.states[5] = {
        "assetId": ASSET_B,
        "positionMs": 15_000,
        "revision": 3,
        "manifestRevision": server.revision,
        "capturedAt": "2026-11-20T10:00:00.000Z",
    }
    progress = orbit.progress("5")
    assert (progress.position_ms, progress.track_index) == (75_000, 1)
    assert progress.updated == int(
        datetime.fromisoformat("2026-11-20T10:00:00+00:00").timestamp()
    )
    with pytest.raises(ConnectedError) as gone:
        orbit.progress("404")
    assert gone.value.status == 404


def test_saving_progress_follows_bookorbits_revision_rules() -> None:
    server = OrbitServer()
    orbit = client(server)
    link = GrimmoryLink("5", "52", 1, 60_000, 120_000, "bookorbit")
    orbit.save_progress(link, 90_000, 1, 30_000)
    state = server.states[5]
    assert (state["assetId"], state["positionMs"], state["revision"]) == (
        ASSET_B,
        30_000,
        1,
    )
    # Another player wrote meanwhile: the next save builds on its revision.
    server.states[5] = {**state, "revision": 7}
    orbit.save_progress(link, 100_000, 1, 40_000)
    assert (server.states[5]["positionMs"], server.states[5]["revision"]) == (40_000, 8)
    # The part this file came from was removed: place the position by time.
    server.assets[5] = [server.assets[5][0]]
    server.revision = "b" * 64
    orbit.save_progress(link, 30_000, 1, 30_000)
    assert (server.states[5]["assetId"], server.states[5]["positionMs"]) == (
        ASSET_A,
        30_000,
    )


def test_a_conflict_is_retried_once() -> None:
    server = OrbitServer()
    transport = server.transport()
    put = server._put
    attempts: list[int] = []

    def racing(*args: Any) -> HttpResponse:
        attempts.append(1)
        if len(attempts) == 1:  # another device saves just before us
            server.states[5] = {
                "assetId": ASSET_A,
                "positionMs": 1,
                "revision": 4,
                "manifestRevision": server.revision,
                "capturedAt": "2026-01-01T00:00:00Z",
            }
            raise ConnectedError("Playback state revision conflict", 409)
        return put(*args)

    transport.routes[f"PUT {BASE}/api/v1/audiobooks/"] = racing
    client(server, transport).save_progress(
        GrimmoryLink("5", "51", 0, 0, 120_000, "bookorbit"), 20_000, 0, 20_000
    )
    assert len(attempts) == 2
    assert server.states[5]["revision"] == 5


def test_a_read_only_key_explains_what_to_change() -> None:
    server = OrbitServer(read_only=True)
    with pytest.raises(ConnectedError, match="read-only") as caught:
        client(server).save_progress(
            GrimmoryLink("5", "51", 0, 0, 120_000, "bookorbit"), 1_000, 0, 1_000
        )
    assert "write access" in str(caught.value)


def orbit_track(
    track_id: int,
    index: int,
    *,
    bookmark: int = 0,
    played: bool = False,
    last_played: int = 0,
) -> Track:
    link = GrimmoryLink(
        "5", str(51 + index), index, index * 60_000, 120_000, "bookorbit"
    )
    return Track(
        track_id,
        f"Two Parts ({index + 1}/2)",
        "A. Writer",
        "Two Parts",
        60_000,
        metadata=TrackMetadata(
            comment=link.comment,
            bookmark_time_ms=bookmark,
            played=played,
            last_played=last_played,
            remember_position=True,
        ),
    )


def test_ipod_and_bookorbit_progress_round_trip() -> None:
    server = OrbitServer()
    orbit = client(server)
    grimmory_track = replace(
        orbit_track(9, 0), metadata=TrackMetadata(comment="grimmory:9:90:0:0:5")
    )
    tracks = (
        orbit_track(1, 0, played=True, last_played=100),
        orbit_track(2, 1, bookmark=20_000, last_played=200),
        grimmory_track,
    )
    (book,) = ipod_books(tracks, "bookorbit")  # the Grimmory book is left out
    assert book.position[0] == 80_000

    # iPod listened further: send it to BookOrbit.
    plan = plan_progress((book,), {"5": orbit.progress("5")}, {})
    (push,) = plan.to_grimmory
    orbit.save_progress(
        push.link, push.position_ms, push.track_index, push.track_position_ms
    )
    assert orbit.progress("5").position_ms == 80_000
    assert plan.marks == (GrimmoryMark("5", 80_000, 80_000),)

    # Then BookOrbit's player moved on to 1:45: the iPod bookmark follows.
    server.states[5] = {**server.states[5], "positionMs": 45_000, "revision": 2}
    plan = plan_progress((book,), {"5": orbit.progress("5")}, {"5": plan.marks[0]})
    assert plan.to_grimmory == ()
    (pull,) = plan.to_ipod
    assert pull.position_ms == 105_000
    assert {c.track_id: dict(c.edits) for c in pull.changes}[2][
        "metadata.bookmark_time_ms"
    ] == 45_000


def test_bookorbit_settings_are_saved(tmp_path: Path) -> None:
    from iOpenPod.app.connected.config import BookOrbitConfig
    from storage import AtomicHostFile

    store = ConnectedConfigStore(AtomicHostFile(tmp_path / "connected.json"))
    config = ConnectedConfig(
        bookorbit=BookOrbitConfig(
            "http://orbit:3000",
            "will",
            sync_progress=False,
            marks=(GrimmoryMark("5", 1, 2),),
        )
    )
    store.save(config)
    assert store.load().bookorbit == config.bookorbit
    assert KEY not in (tmp_path / "connected.json").read_text()


# --- the Connected controller ------------------------------------------------------------


def orbit_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: OrbitServer
) -> ConnectedController:
    from tests.iOpenPod.app.connected.test_sync_extras import controller

    transport = server.transport()
    monkeypatch.setattr(
        "iOpenPod.app.connected_controller.BookOrbitClient",
        lambda url, key: BookOrbitClient(url, key, transport),
    )
    monkeypatch.setattr(
        "iOpenPod.app.connected_controller.load_host_media_folders", lambda _s: ()
    )
    made, _coordinator = controller(tmp_path)
    made.update_config(replace(made.config, folder_root=str(tmp_path / "Connected")))
    return made


def test_connecting_keeps_the_key_in_the_vault_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.iOpenPod.app.test_library_write_controller import wait_for
    from tests.iOpenPod.GUI.application_shell_test_support import APPLICATION

    from iOpenPod.app.connected.secrets import SecretKey

    made = orbit_controller(tmp_path, monkeypatch, OrbitServer())
    results: list[tuple[str, object]] = []
    said: list[str] = []
    made.result.connect(lambda op, value: results.append((op, value)))
    made.message.connect(said.append)
    try:
        made.bookorbit_connect(BASE, "bok_wrong")
        wait_for(lambda: not made.busy)
        APPLICATION.processEvents()
        assert said and "didn't accept the API key" in said[-1]
        assert made.config.bookorbit.username == ""
        assert made.secret(SecretKey.BOOKORBIT) == {}

        made.bookorbit_connect(f" {BASE} ", f" {KEY} ")
        wait_for(lambda: not made.busy)
        APPLICATION.processEvents()
        assert results[-1] == ("bookorbit-connect", ("will", 2))
        assert (made.config.bookorbit.url, made.config.bookorbit.username) == (
            BASE,
            "will",
        )
        assert made.secret(SecretKey.BOOKORBIT) == {"api_key": KEY}
        assert KEY not in (tmp_path / "connected.json").read_text()
        assert made.streams

        made.forget(SecretKey.BOOKORBIT)
        assert made.config.bookorbit.username == ""
        assert not made.streams
    finally:
        made.shutdown()


def test_before_sync_progress_is_sent_and_books_are_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.iOpenPod.app.test_library_write_controller import wait_for
    from tests.iOpenPod.GUI.application_shell_test_support import APPLICATION

    from iOpenPod.app.connected.config import BookOrbitConfig
    from iOpenPod.app.connected.secrets import SecretKey
    from iOpenPod.app.connected_controller import SyncPreparation

    server = OrbitServer()
    made = orbit_controller(tmp_path, monkeypatch, server)
    results: list[tuple[str, object]] = []
    made.result.connect(lambda op, value: results.append((op, value)))
    try:
        made.save_secret(SecretKey.BOOKORBIT, {"api_key": KEY})
        made.update_config(
            replace(made.config, bookorbit=BookOrbitConfig(BASE, "will"))
        )
        tracks = (
            orbit_track(1, 0, played=True, last_played=100),
            orbit_track(2, 1, bookmark=20_000, last_played=200),
        )
        assert made.prepare_for_sync(immich=False, tracks=tracks)
        wait_for(lambda: not made.busy)
        APPLICATION.processEvents()
        operation, value = results[-1]
        assert operation == "prepare-sync"
        assert isinstance(value, SyncPreparation)
        assert value.progress_sent == 1
        assert len(value.catalog.items) == 3
        assert (server.states[5]["assetId"], server.states[5]["positionMs"]) == (
            ASSET_B,
            20_000,
        )
        assert made.config.bookorbit.marks == (GrimmoryMark("5", 80_000, 80_000),)
        assert made.config.grimmory.marks == ()

        # Switched off: positions are no longer sent before Sync.
        made.set_bookorbit_sync_progress(False)
        server.states.clear()
        assert made.prepare_for_sync(immich=False, tracks=tracks)
        wait_for(lambda: not made.busy)
        APPLICATION.processEvents()
        assert server.states == {}
    finally:
        made.shutdown()
