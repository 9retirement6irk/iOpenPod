"""Grimmory single sign-on (OIDC) for a desktop app.

Grimmory's web app signs in with the Authorization Code flow and PKCE against the
identity provider Grimmory is configured for, then hands the code to Grimmory
(``POST /api/v1/auth/oidc/callback``), which exchanges it, validates the ID token,
and answers with Grimmory's own access and refresh tokens.

iOpenPod does the same as a native app (RFC 8252): it opens the provider's sign-in
page in the browser and receives the code on a loopback address,
``http://127.0.0.1:53817/oauth2-callback``. Grimmory only accepts http(s) redirect
addresses that end in ``/oauth2-callback`` and whose origin matches the request's
``Origin``, so iOpenPod sends that origin with the callback. The identity provider
must list the loopback address as an allowed redirect for Grimmory's client, which
is the one setup step a server admin does once.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, urlencode, urlsplit

from .http import CancelledError, ConnectedError, json_body

if TYPE_CHECKING:
    from collections.abc import Callable
    from threading import Event

    from .grimmory import GrimmoryClient, GrimmoryTokens

SSO_PORT = 53817
CALLBACK_PATH = "/oauth2-callback"
SSO_TIMEOUT = 300.0  # Grimmory's sign-in state expires after five minutes
_DEFAULT_SCOPES = "openid profile email groups offline_access"


def redirect_uri(port: int = SSO_PORT) -> str:
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


@dataclass(frozen=True, slots=True)
class SsoProvider:
    name: str
    issuer: str
    client_id: str
    scopes: str
    only: bool  # Grimmory disabled password sign-in


@dataclass(frozen=True, slots=True)
class SsoRequest:
    """One pending sign-in: what the browser opens and what Grimmory checks."""

    url: str
    state: str
    nonce: str
    verifier: str
    redirect: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    verifier = _b64(secrets.token_bytes(48))
    return verifier, _b64(hashlib.sha256(verifier.encode("ascii")).digest())


def _object(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def provider(client: GrimmoryClient) -> SsoProvider | None:
    """Grimmory's public sign-in settings; ``None`` when single sign-on is off."""

    data = _object(client.public("/api/v1/public-settings"))
    details = _object(data.get("oidcProviderDetails"))
    issuer = str(details.get("issuerUri") or "").strip()
    client_id = str(details.get("clientId") or "").strip()
    if not data.get("oidcEnabled") or not issuer or not client_id:
        return None
    return SsoProvider(
        str(details.get("providerName") or "").strip() or "Single sign-on",
        issuer.rstrip("/"),
        client_id,
        str(details.get("scopes") or "").strip() or _DEFAULT_SCOPES,
        bool(data.get("oidcForceOnlyMode")),
    )


def start(
    client: GrimmoryClient, sso: SsoProvider, *, port: int = SSO_PORT
) -> SsoRequest:
    """Ask Grimmory for a state, find the provider's sign-in page, build its URL."""

    state = str(_object(client.public("/api/v1/auth/oidc/state")).get("state") or "")
    if not state:
        raise ConnectedError("Grimmory didn't start a single sign-on.")
    discovery = _object(
        json_body(
            client.transport.send(
                "GET",
                f"{sso.issuer}/.well-known/openid-configuration",
                {"Accept": "application/json"},
            )
        )
    )
    endpoint = str(discovery.get("authorization_endpoint") or "")
    if urlsplit(endpoint).scheme not in {"http", "https"}:
        raise ConnectedError(
            f"{sso.name} didn't describe its sign-in page. Check Grimmory's OIDC issuer address."
        )
    verifier, challenge = pkce_pair()
    nonce = _b64(secrets.token_bytes(24))
    redirect = redirect_uri(port)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": sso.client_id,
            "redirect_uri": redirect,
            "scope": sso.scopes,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "nonce": nonce,
        }
    )
    separator = "&" if "?" in endpoint else "?"
    return SsoRequest(f"{endpoint}{separator}{query}", state, nonce, verifier, redirect)


def finish(client: GrimmoryClient, request: SsoRequest, code: str) -> GrimmoryTokens:
    """Hand the provider's code to Grimmory and keep the Grimmory tokens it returns."""

    parts = urlsplit(request.redirect)
    origin = f"{parts.scheme}://{parts.netloc}"
    body = json.dumps(
        {
            "code": code,
            "codeVerifier": request.verifier,
            "redirectUri": request.redirect,
            "nonce": request.nonce,
            "state": request.state,
        }
    ).encode()
    try:
        response = client.transport.send(
            "POST",
            f"{client.base}/api/v1/auth/oidc/callback",
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                # Grimmory compares the redirect's origin with the request's.
                "Origin": origin,
            },
            body,
        )
    except ConnectedError as error:
        raise _explain(error, origin) from None
    return client.accept_tokens(_object(json_body(response)))


def _explain(error: ConnectedError, origin: str) -> ConnectedError:
    text = str(error)
    if error.status == 400 and "redirect" in text.casefold():
        return ConnectedError(
            "Grimmory refused iOpenPod's sign-in address. Update Grimmory, or ask its admin to check the OIDC settings.",
            400,
        )
    if error.status == 400:
        return ConnectedError(
            "The sign-in took longer than five minutes or was already used. Try again.",
            400,
        )
    if error.status == 403 and "provisioned" in text.casefold():
        return ConnectedError(
            "Your sign-in worked, but Grimmory has no account for you and won't create one. Ask its admin to add you or turn on automatic accounts.",
            403,
        )
    if error.status == 403:
        return ConnectedError(
            f"Grimmory refused the request from {origin}. If its admin limited allowed origins (app.cors.allowed-origins), add {origin}.",
            403,
        )
    if error.status == 502:
        return ConnectedError(
            f"Grimmory couldn't finish the sign-in with your identity provider. Ask its admin to allow {origin}{CALLBACK_PATH} as a redirect address for Grimmory's client.",
            502,
        )
    return error


# --- loopback receiver --------------------------------------------------------------------


_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>iOpenPod</title>
<body style="font:16px system-ui,sans-serif;margin:3em auto;max-width:32em;padding:0 1em">
<h1 style="font-size:1.4em">{title}</h1><p>{text}</p></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    server: _Receiver

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path != CALLBACK_PATH:
            self.send_error(404)
            return
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        accepted = self.server.offer(query)
        if accepted and "code" in query:
            title, text = (
                "You're signed in",
                "Return to iOpenPod. You can close this tab.",
            )
        elif accepted:
            title, text = (
                "Sign-in didn't finish",
                escape(query.get("error_description") or query.get("error") or ""),
            )
        else:
            title, text = (
                "This sign-in has expired",
                "Start again from iOpenPod.",
            )
        page = _PAGE.format(title=title, text=text).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(page)


class _Receiver(HTTPServer):
    def __init__(self, port: int, state: str) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.state = state
        self.result: dict[str, str] | None = None
        self.arrived = threading.Event()

    def offer(self, query: dict[str, str]) -> bool:
        # Only the answer to this sign-in counts; anything else is ignored.
        if self.arrived.is_set() or query.get("state") != self.state:
            return False
        self.result = query
        self.arrived.set()
        return True


class LoopbackReceiver:
    """Listen on 127.0.0.1 for exactly one answer carrying the expected state."""

    def __init__(self, state: str, port: int = SSO_PORT) -> None:
        try:
            self._server = _Receiver(port, state)
        except OSError:
            raise ConnectedError(
                f"Another app is using port {port}, which iOpenPod needs for single sign-on. Close it and try again."
            ) from None
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="grimmory-sso",
            daemon=True,
        )
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def wait(self, cancelled: Event, timeout: float = SSO_TIMEOUT) -> str:
        """Return the code, or raise when cancelled, refused, or out of time."""

        try:
            waited = 0.0
            while not self._server.arrived.wait(0.25):
                waited += 0.25
                if cancelled.is_set():
                    raise CancelledError
                if waited >= timeout:
                    raise ConnectedError(
                        "No answer from the sign-in page within five minutes. Try again."
                    )
            result = self._server.result or {}
            if result.get("code"):
                return result["code"]
            reason = result.get("error_description") or result.get("error") or ""
            raise ConnectedError(
                "Single sign-on didn't finish"
                + (f": {' '.join(reason.split())[:200]}" if reason else ".")
            )
        finally:
            self.close()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def sign_in(
    client: GrimmoryClient,
    sso: SsoProvider,
    cancelled: Event,
    open_page: Callable[[str], None],
    *,
    port: int = SSO_PORT,
    timeout: float = SSO_TIMEOUT,
) -> GrimmoryTokens:
    """The whole flow: listen, open the browser, wait for the code, ask Grimmory."""

    request = start(client, sso, port=port)
    receiver = LoopbackReceiver(request.state, port)
    try:
        open_page(request.url)
    except BaseException:
        receiver.close()
        raise
    code = receiver.wait(cancelled, timeout)
    return finish(client, request, code)


__all__ = [
    "CALLBACK_PATH",
    "SSO_PORT",
    "LoopbackReceiver",
    "SsoProvider",
    "SsoRequest",
    "finish",
    "pkce_pair",
    "provider",
    "redirect_uri",
    "sign_in",
    "start",
]
