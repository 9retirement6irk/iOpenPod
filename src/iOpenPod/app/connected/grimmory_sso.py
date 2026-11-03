"""Grimmory single sign-on (OIDC) for a desktop app, the way its mobile apps do it.

iOpenPod signs in with the Authorization Code flow and PKCE against the identity
provider Grimmory is configured for. The provider returns to iOpenPod through a
custom-scheme link, ``iopenpod://oauth2-callback?code=...&state=...``, which the
operating system hands to the running app (see ``iOpenPod.app.app_links``). The
code then goes to Grimmory's mobile callback (``POST
/api/v1/auth/oidc/mobile/callback``), which exchanges it, validates the ID token and
answers with Grimmory's own access and refresh tokens.

Setup, once per server: Grimmory accepts a custom-scheme address only when it is
listed under Settings → Authentication → Mobile Redirect URIs (only
``grimmory://oauth2-callback`` is listed by default), and the identity provider
must allow the same address as a redirect for Grimmory's client.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, urlencode, urlsplit

from .http import CancelledError, ConnectedError, json_body

if TYPE_CHECKING:
    from collections.abc import Callable
    from threading import Event

    from .grimmory import GrimmoryClient, GrimmoryTokens

CALLBACK = "iopenpod://oauth2-callback"
SSO_TIMEOUT = 300.0  # Grimmory's sign-in state expires after five minutes
_DEFAULT_SCOPES = "openid profile email groups offline_access"


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
    redirect: str = CALLBACK


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


def start(client: GrimmoryClient, sso: SsoProvider) -> SsoRequest:
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
    query = urlencode(
        {
            "response_type": "code",
            "client_id": sso.client_id,
            "redirect_uri": CALLBACK,
            "scope": sso.scopes,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "nonce": nonce,
        }
    )
    separator = "&" if "?" in endpoint else "?"
    return SsoRequest(f"{endpoint}{separator}{query}", state, nonce, verifier)


def finish(client: GrimmoryClient, request: SsoRequest, code: str) -> GrimmoryTokens:
    """Hand the provider's code to Grimmory and keep the Grimmory tokens it returns."""

    query = urlencode(
        {
            "code": code,
            "code_verifier": request.verifier,
            "redirect_uri": request.redirect,
            "nonce": request.nonce,
            "state": request.state,
        }
    )
    try:
        response = client.transport.send(
            "POST",
            f"{client.base}/api/v1/auth/oidc/mobile/callback?{query}",
            {"Accept": "application/json"},
            b"",
        )
    except ConnectedError as error:
        raise _explain(error) from None
    return client.accept_tokens(_object(json_body(response)))


def _explain(error: ConnectedError) -> ConnectedError:
    text = str(error).casefold()
    if error.status == 400 and "redirect" in text:
        return ConnectedError(
            f"Grimmory doesn't allow iOpenPod's sign-in address yet. In Grimmory, add {CALLBACK} under Settings → Authentication → Mobile Redirect URIs, then try again.",
            400,
        )
    if error.status == 400:
        return ConnectedError(
            "The sign-in took longer than five minutes or was already used. Try again.",
            400,
        )
    if error.status == 403 and "provisioned" in text:
        return ConnectedError(
            "Your sign-in worked, but Grimmory has no account for you and won't create one. Ask its admin to add you or turn on automatic accounts.",
            403,
        )
    if error.status == 502:
        return ConnectedError(
            f"Grimmory couldn't finish the sign-in with your identity provider. Check that {CALLBACK} is an allowed redirect address for Grimmory's client there.",
            502,
        )
    return error


class SsoInbox:
    """Where the running app drops ``iopenpod://oauth2-callback`` links.

    The sign-in job waits here; the GUI thread offers each link that arrives. Only
    a link carrying the expected state is accepted, so a stale or forged link is
    ignored rather than ending the sign-in.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._arrived = threading.Event()
        self._state = ""
        self._result: dict[str, str] = {}

    def expect(self, state: str) -> None:
        with self._lock:
            self._state = state
            self._result = {}
            self._arrived.clear()

    def offer(self, link: str) -> bool:
        parts = urlsplit(link)
        if parts.scheme.casefold() != "iopenpod" or parts.netloc != "oauth2-callback":
            return False
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        with self._lock:
            if not self._state or self._arrived.is_set():
                return False
            if not secrets.compare_digest(query.get("state", ""), self._state):
                return False
            self._result = query
            self._arrived.set()
            return True

    def cancel(self) -> None:
        with self._lock:
            self._state = ""

    def wait(self, cancelled: Event, timeout: float = SSO_TIMEOUT) -> str:
        """Return the code, or raise when cancelled, refused, or out of time."""

        try:
            waited = 0.0
            while not self._arrived.wait(0.25):
                waited += 0.25
                if cancelled.is_set():
                    raise CancelledError
                if waited >= timeout:
                    raise ConnectedError(
                        f"No answer from the sign-in page within five minutes. If your browser showed an error about the redirect address, add {CALLBACK} to Grimmory's client in your identity provider."
                    )
            with self._lock:
                result = dict(self._result)
            if result.get("code"):
                return result["code"]
            reason = result.get("error_description") or result.get("error") or ""
            raise ConnectedError(
                "Single sign-on didn't finish"
                + (f": {' '.join(reason.split())[:200]}" if reason else ".")
            )
        finally:
            self.cancel()


def sign_in(
    client: GrimmoryClient,
    sso: SsoProvider,
    inbox: SsoInbox,
    cancelled: Event,
    open_page: Callable[[str], None],
    *,
    timeout: float = SSO_TIMEOUT,
) -> GrimmoryTokens:
    """The whole flow: open the browser, wait for iOpenPod's link, ask Grimmory."""

    request = start(client, sso)
    inbox.expect(request.state)
    try:
        open_page(request.url)
    except BaseException:
        inbox.cancel()
        raise
    code = inbox.wait(cancelled, timeout)
    return finish(client, request, code)


__all__ = [
    "CALLBACK",
    "SsoInbox",
    "SsoProvider",
    "SsoRequest",
    "finish",
    "pkce_pair",
    "provider",
    "sign_in",
    "start",
]
