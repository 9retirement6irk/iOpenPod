"""Captured iPod plays become Plex plays; failures keep receipts and stay pending."""

from collections.abc import Mapping

import pytest

from iOpenPod.app.connected.http import ConnectedError
from iOpenPod.app.connected.plex import PlexClient, PlexServer, PlexTrack, parse_track
from iOpenPod.app.scrobbling.clients import client_for
from iOpenPod.app.scrobbling.models import Credentials, Listen, ScrobbleError, Service
from iOpenPod.app.scrobbling.plex import PlexScrobbleClient
from iOpenPod.app.scrobbling.settings import account_setting


def track(key: str, title: str) -> PlexTrack:
    value = parse_track(
        {
            "ratingKey": key,
            "title": title,
            "grandparentTitle": "Band",
            "parentTitle": "Album",
            "duration": 200_000,
            "Media": [{"Part": [{"key": f"/library/parts/{key}/f.mp3", "size": 10}]}],
        }
    )
    assert value is not None
    return value


class FakePlex(PlexClient):
    def __init__(self, fail_on: str = "") -> None:
        super().__init__("test")
        self.plays: list[tuple[str, str]] = []
        self.fail_on = fail_on
        self.library_reads = 0

    def library_tracks(
        self, server: PlexServer, cancelled: object = None, *, skip: object = ()
    ) -> tuple[PlexTrack, ...]:
        self.library_reads += 1
        return (track("1", "One"), track("2", "Two"), track("3", "Three"))

    def record_play(
        self,
        server: PlexServer,
        track: PlexTrack,
        player: Mapping[str, str],
        **_: object,
    ) -> None:
        if track.title == self.fail_on:
            raise ConnectedError("Plex went away")
        self.plays.append((track.rating_key, player["X-Plex-Device-Name"]))


CREDENTIALS = Credentials("will", "server-token", "http://plex:32400", "machine")


def listen(title: str) -> Listen:
    return Listen("Band", title, "Album", 1_700_000_000, 200)


def test_plays_are_attributed_to_the_ipod_and_unknown_songs_stay_pending() -> None:
    plex = FakePlex()
    client = PlexScrobbleClient(plex).for_device("serial", "Will's iPod")
    result = client.submit(
        (listen("One"), listen("Missing"), listen("Three")), CREDENTIALS
    )
    assert result.accepted == (True, False, True)
    assert plex.plays == [("1", "Will's iPod"), ("3", "Will's iPod")]
    assert result.rejections[0].code == 404
    client.submit((listen("Two"),), CREDENTIALS)
    assert plex.library_reads == 1  # the library index is reused across batches


def test_mid_batch_failure_keeps_delivered_receipts() -> None:
    plex = FakePlex(fail_on="Two")
    result = PlexScrobbleClient(plex).submit(
        (listen("One"), listen("Two"), listen("Three")), CREDENTIALS
    )
    assert result.accepted == (True, False, False)
    assert plex.plays == [("1", "iPod")]


def test_missing_server_details_ask_to_reconnect() -> None:
    with pytest.raises(ScrobbleError, match="Reconnect Plex"):
        PlexScrobbleClient(FakePlex()).submit((listen("One"),), Credentials("will", ""))


def test_plex_is_a_scrobbling_service() -> None:
    assert Service.PLEX.label == "Plex"
    assert account_setting(Service.PLEX).key == "scrobbling/plex-username"
    assert isinstance(client_for(Service.PLEX), PlexScrobbleClient)
