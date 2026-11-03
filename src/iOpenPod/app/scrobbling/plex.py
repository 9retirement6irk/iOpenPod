"""Plex as a Scrobbling service: each captured iPod play becomes a Plex play.

Plays are reported as a finished listen on the matched Plex Track, attributed to
a Plex player named after the iPod (platform "iPod"), so they appear in Plex Play
History and raise the Track's play count. Listens that match no Plex Track are
rejected and remain pending, so they can be delivered if the Track is added later.

Credentials map onto the shared Scrobbling record as: ``username`` = Plex account
name, ``token`` = the server access token, ``api_key`` = server base URL, and
``api_secret`` = server machine identifier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from iOpenPod.app.connected.http import ConnectedError
from iOpenPod.app.connected.plex import (
    PlexClient,
    PlexServer,
    client_identifier,
    ipod_player,
)
from iOpenPod.app.connected.plex_match import PlexIndex

from .models import Credentials, Listen, RejectedListen, ScrobbleError, Submission

if TYPE_CHECKING:
    from collections.abc import Callable

_NOT_FOUND = 404
_AMBIGUOUS = 409


class PlexScrobbleClient:
    batch_size = 20

    def __init__(
        self,
        client: PlexClient | None = None,
        *,
        pause: Callable[[float], None] | None = None,
    ) -> None:
        self._client = client or PlexClient(client_identifier("scrobbling"))
        self._pause = pause
        self._index: tuple[str, PlexIndex] | None = None
        self._device = ("", "iPod")

    def for_device(self, identity: str, name: str) -> PlexScrobbleClient:
        self._device = (identity, name or "iPod")
        return self

    def _server(self, credentials: Credentials) -> PlexServer:
        if (
            not credentials.token
            or not credentials.api_key
            or not credentials.api_secret
        ):
            raise ScrobbleError("Reconnect Plex in the Plex page to send plays.")
        return PlexServer(
            credentials.api_secret,
            credentials.username,
            credentials.api_key,
            credentials.token,
        )

    def submit(
        self, entries: tuple[Listen, ...], credentials: Credentials
    ) -> Submission:
        server = self._server(credentials)
        try:
            if self._index is None or self._index[0] != server.uri:
                self._index = (
                    server.uri,
                    PlexIndex(self._client.library_tracks(server)),
                )
        except ConnectedError as error:
            raise ScrobbleError(str(error)) from None
        index = self._index[1]
        player = ipod_player(*self._device)
        accepted: list[bool] = []
        rejections: list[RejectedListen] = []
        for position, entry in enumerate(entries):
            match = index.match(
                entry.title, entry.artist, entry.album, entry.duration * 1000
            )
            if match.track is None:
                accepted.append(False)
                rejections.append(
                    RejectedListen(
                        entry,
                        _AMBIGUOUS if match.ambiguous else _NOT_FOUND,
                        "Several Plex Tracks match this song."
                        if match.ambiguous
                        else "No matching Track in your Plex music library.",
                    )
                )
                continue
            options = {} if self._pause is None else {"pause": self._pause}
            try:
                self._client.record_play(server, match.track, player, **options)
            except ConnectedError as error:
                # Keep receipts for plays already delivered; retry the rest later.
                for remaining in entries[position:]:
                    accepted.append(False)
                    rejections.append(RejectedListen(remaining, 503, str(error)))
                break
            accepted.append(True)
        return Submission(tuple(accepted), tuple(rejections))


__all__ = ["PlexScrobbleClient"]
