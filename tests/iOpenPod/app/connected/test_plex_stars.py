"""Scrobble now's Plex step: set Plex stars from the iPod's without a review."""

from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

from tests.iOpenPod.app.connected.test_plex import ipod, plex
from tests.iOpenPod.app.test_library_write_controller import wait_for
from tests.iOpenPod.GUI.application_shell_test_support import APPLICATION

from iOpenPod.app.connected.config import ConnectedConfigStore
from iOpenPod.app.connected.http import ConnectedError
from iOpenPod.app.connected.plex import PlexServer, PlexTrack
from iOpenPod.app.connected.secrets import MemorySecretStore, SecretKey
from iOpenPod.app.connected_controller import ConnectedController, StarsSent
from iOpenPod.app.scrobbling.models import Credentials
from storage import AtomicHostFile


def test_ipod_stars_go_to_plex_and_plex_only_stars_wait(tmp_path: Path) -> None:
    secrets = MemorySecretStore()
    controller = ConnectedController(
        MagicMock(),
        ConnectedConfigStore(AtomicHostFile(tmp_path / "connected.json")),
        MagicMock(),
        MagicMock(),
        secrets=secrets,
    )
    results: list[tuple[str, object]] = []
    controller.result.connect(lambda op, value: results.append((op, value)))
    try:
        # No server chosen: nothing to do, and nothing is said.
        assert controller.plex_credentials() is None
        assert not controller.plex_push_stars((ipod(10, "A", 100),))

        secrets.save(
            SecretKey.PLEX,
            {
                "server_token": "tok",
                "server_uri": "http://plex:32400",
                "server_id": "s",
            },
        )
        assert controller.plex_credentials() == Credentials(
            "Plex", "tok", "http://plex:32400", "s"
        )
        rated: list[tuple[str, float]] = []

        def library(
            _server: PlexServer, _cancelled: Event, *, skip: object = ()
        ) -> tuple[PlexTrack, ...]:
            return (
                plex("1", "A", "Band", "Album", 200_000, rating=4.0),
                plex("2", "B", "Band", "Album", 200_000, rating=6.0),
                plex("3", "C", "Band", "Album", 200_000),
            )

        def rate(_server: PlexServer, key: str, rating: float) -> None:
            if key == "3":
                raise ConnectedError("HTTP 500", 500)
            rated.append((key, rating))

        controller.plex.library_tracks = library  # type: ignore[method-assign,assignment]
        controller.plex.rate = rate  # type: ignore[method-assign,assignment]
        tracks = (ipod(10, "A", 100), ipod(11, "B", 0), ipod(12, "C", 80))
        assert controller.plex_push_stars(tracks)
        wait_for(lambda: not controller.busy)
        APPLICATION.processEvents()
        assert rated == [("1", 10.0)]
        assert results == [("plex-stars", StarsSent(1, 1, 1))]
    finally:
        controller.shutdown()
