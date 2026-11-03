"""Small, credential-safe HTTP access shared by Connected Sources."""

from __future__ import annotations

import json
import os
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import certifi

from iOpenPod.app.core.version import get_version

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from email.message import Message
    from http.client import HTTPResponse
    from threading import Event

MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024 * 1024
_CHUNK = 256 * 1024


class ConnectedError(Exception):
    """A safe, credential-free message suitable for presentation."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CancelledError(ConnectedError):
    """The user cancelled the running Connected Source work."""

    def __init__(self) -> None:
        super().__init__("Cancelled.")


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    content_type: str


class _SameOriginRedirects(HTTPRedirectHandler):
    """Follow redirects only within one origin, so credentials never travel."""

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> Request | None:
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]


class Transport(Protocol):
    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        *,
        timeout: float = 30,
        limit: int = MAX_TEXT_BYTES,
    ) -> HttpResponse: ...

    def download(
        self,
        url: str,
        headers: Mapping[str, str],
        destination: Path,
        *,
        cancelled: Event | None = None,
        progress: Callable[[int, int], None] | None = None,
        timeout: float = 120,
    ) -> str: ...


def _user_agent() -> str:
    return f"iOpenPod/{get_version()} Connected Sources"


class UrllibTransport:
    """The production transport: certifi trust, same-origin redirects, size limits."""

    def __init__(self) -> None:
        self._opener = build_opener(
            HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
            _SameOriginRedirects(),
        )

    def _open(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HTTPResponse:
        scheme = urlsplit(url).scheme.casefold()
        if scheme not in {"http", "https"}:
            raise ConnectedError("Only http:// and https:// addresses are supported.")
        request = Request(
            url,
            data=body,
            method=method,
            headers={"User-Agent": _user_agent(), **headers},
        )
        try:
            response: HTTPResponse = self._opener.open(request, timeout=timeout)
        except HTTPError as error:
            detail = _error_detail(error)
            error.close()
            raise ConnectedError(
                f"The server answered HTTP {error.code}{': ' + detail if detail else ''}.",
                error.code,
            ) from None
        except (URLError, OSError, ValueError) as error:
            reason = getattr(error, "reason", error)
            raise ConnectedError(
                f"Could not reach {urlsplit(url).netloc or url}: {reason}"
            ) from None
        return response

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
        *,
        timeout: float = 30,
        limit: int = MAX_TEXT_BYTES,
    ) -> HttpResponse:
        with self._open(method, url, headers, body, timeout) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ConnectedError("The server sent more data than expected.")
            return HttpResponse(
                response.status, data, response.headers.get("Content-Type") or ""
            )

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
        """Stream into a sibling temporary file, then atomically replace the target."""

        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=destination.parent, prefix=".download-", suffix=".part"
        )
        temporary_path = Path(temporary)
        try:
            with (
                os.fdopen(descriptor, "wb") as stream,
                self._open("GET", url, headers, None, timeout) as response,
            ):
                total = int(response.headers.get("Content-Length") or 0)
                received = 0
                while chunk := response.read(_CHUNK):
                    if cancelled is not None and cancelled.is_set():
                        raise CancelledError
                    stream.write(chunk)
                    received += len(chunk)
                    if progress is not None:
                        progress(received, total)
                if total and received != total:
                    raise ConnectedError("The download ended early. Try again.")
                stream.flush()
                os.fsync(stream.fileno())
                content_type = response.headers.get("Content-Type") or ""
            os.replace(temporary_path, destination)
            return content_type
        finally:
            temporary_path.unlink(missing_ok=True)


def _error_detail(error: HTTPError) -> str:
    try:
        raw = error.read(4096)
    except OSError:
        return ""
    try:
        value: object = json.loads(raw)
    except ValueError:
        return ""
    if isinstance(value, dict):
        for key in ("message", "error", "detail"):
            message = value.get(key)
            if isinstance(message, str) and message.strip():
                return " ".join(message.split())[:200]
    return ""


def json_body(response: HttpResponse) -> object:
    try:
        return json.loads(response.body) if response.body else {}
    except ValueError:
        raise ConnectedError("The server sent a reply that is not JSON.") from None


def normalize_base_url(value: str, *, default_scheme: str = "http") -> str:
    """Return ``scheme://host[:port][/path]`` without a trailing slash."""

    text = value.strip().rstrip("/")
    if not text:
        raise ConnectedError("Enter the server's address.")
    if "://" not in text:
        text = f"{default_scheme}://{text}"
    parts = urlsplit(text)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        raise ConnectedError("Enter an address like https://server.example.com.")
    if parts.query or parts.fragment:
        raise ConnectedError("Enter the server address without ? or # parts.")
    return f"{parts.scheme.casefold()}://{parts.netloc}{parts.path.rstrip('/')}"


__all__ = [
    "MAX_JSON_BYTES",
    "CancelledError",
    "ConnectedError",
    "HttpResponse",
    "Transport",
    "UrllibTransport",
    "json_body",
    "normalize_base_url",
]
