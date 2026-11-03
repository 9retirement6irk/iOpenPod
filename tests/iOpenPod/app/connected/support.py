"""A scripted Transport so Connected Source clients run without a network."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from iOpenPod.app.connected.http import ConnectedError, HttpResponse

if TYPE_CHECKING:
    from pathlib import Path
    from threading import Event

type Handler = Callable[[str, str, Mapping[str, str], bytes | None], HttpResponse]


def reply(
    value: object = None,
    *,
    status: int = 200,
    body: bytes | None = None,
    content_type: str = "application/json",
) -> HttpResponse:
    if status >= 400:
        raise ConnectedError(f"HTTP {status}", status)
    data = (
        body
        if body is not None
        else json.dumps(value if value is not None else {}).encode()
    )
    return HttpResponse(status, data, content_type)


@dataclass
class FakeTransport:
    """Answers by the longest matching ``METHOD url-prefix`` route."""

    routes: dict[str, Handler | object] = field(
        default_factory=dict[str, "Handler | object"]
    )
    calls: list[tuple[str, str, dict[str, str]]] = field(
        default_factory=list[tuple[str, str, dict[str, str]]]
    )
    files: dict[str, bytes] = field(default_factory=dict[str, bytes])

    def _route(self, method: str, url: str) -> Handler | object:
        best = ""
        for key in self.routes:
            route_method, _, prefix = key.partition(" ")
            if (
                route_method == method
                and url.startswith(prefix)
                and len(prefix) > len(best)
            ):
                best = prefix
        if not best:
            raise ConnectedError(f"HTTP 404 for {method} {url}", 404)
        return self.routes[f"{method} {best}"]

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        *,
        timeout: float = 30,
        limit: int = 0,
    ) -> HttpResponse:
        self.calls.append((method, url, dict(headers)))
        handler = self._route(method, url)
        if callable(handler):
            return cast("Handler", handler)(method, url, headers, body)
        if isinstance(handler, HttpResponse):
            return handler
        if isinstance(handler, ConnectedError):
            raise handler
        return reply(handler)

    def download(
        self,
        url: str,
        headers: Mapping[str, str],
        destination: Path,
        *,
        cancelled: Event | None = None,
        progress: Callable[[int, int], None] | None = None,
        timeout: float = 120,
    ) -> str:
        self.calls.append(("DOWNLOAD", url, dict(headers)))
        for prefix, data in sorted(self.files.items(), key=lambda item: -len(item[0])):
            if url.startswith(prefix):
                destination.write_bytes(data)
                return "application/octet-stream"
        raise ConnectedError(f"HTTP 404 for {url}", 404)
