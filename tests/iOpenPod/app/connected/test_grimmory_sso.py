"""Grimmory single sign-on through iopenpod://oauth2-callback, like its mobile apps."""

import base64
import hashlib
import threading
from collections.abc import Mapping
from threading import Event
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from tests.iOpenPod.app.connected.support import FakeTransport, reply

from iOpenPod.app.connected import grimmory_sso
from iOpenPod.app.connected.grimmory import GrimmoryClient, GrimmoryTokens
from iOpenPod.app.connected.http import CancelledError, ConnectedError, HttpResponse

BASE = "http://g"
ISSUER = "https://id.example/application/o/grimmory"
SETTINGS = {
    "oidcEnabled": True,
    "oidcForceOnlyMode": False,
    "oidcProviderDetails": {
        "providerName": "Authentik",
        "issuerUri": ISSUER + "/",
        "clientId": "grimmory-web",
        "scopes": "",
    },
}


def server(posted: list[tuple[str, Mapping[str, str]]]) -> FakeTransport:
    def callback(
        _m: str, url: str, headers: Mapping[str, str], _body: bytes | None
    ) -> HttpResponse:
        posted.append((url, dict(headers)))
        return reply({"accessToken": "a1", "refreshToken": "r1"})

    return FakeTransport(
        {
            f"GET {BASE}/api/v1/public-settings": SETTINGS,
            f"GET {BASE}/api/v1/auth/oidc/state": {"state": "s-123"},
            f"GET {ISSUER}/.well-known/openid-configuration": {
                "authorization_endpoint": "https://id.example/authorize?tenant=1"
            },
            f"POST {BASE}/api/v1/auth/oidc/mobile/callback": callback,
        }
    )


def link(state: str, **values: str) -> str:
    return f"{grimmory_sso.CALLBACK}?{urlencode({'state': state, **values})}"


def test_provider_reads_public_settings() -> None:
    client = GrimmoryClient(BASE, transport=server([]))
    assert grimmory_sso.provider(client) == grimmory_sso.SsoProvider(
        "Authentik",
        ISSUER,
        "grimmory-web",
        "openid profile email groups offline_access",
        False,
    )
    off = FakeTransport({f"GET {BASE}/api/v1/public-settings": {"oidcEnabled": False}})
    assert grimmory_sso.provider(GrimmoryClient(BASE, transport=off)) is None


def test_full_sign_in_returns_through_iopenpod_link() -> None:
    posted: list[tuple[str, Mapping[str, str]]] = []
    seen: list[GrimmoryTokens] = []
    client = GrimmoryClient(BASE, transport=server(posted), on_tokens=seen.append)
    sso = grimmory_sso.provider(client)
    assert sso is not None
    inbox = grimmory_sso.SsoInbox()
    opened: list[str] = []
    accepted: list[bool] = []

    def browser(url: str) -> None:
        opened.append(url)
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert query["redirect_uri"] == "iopenpod://oauth2-callback"

        def returns() -> None:
            # The operating system hands these to the running app, in any order.
            accepted.append(inbox.offer(link("forged", code="evil")))
            accepted.append(inbox.offer("iopenpod://elsewhere?state=s-123&code=x"))
            accepted.append(inbox.offer(link(query["state"], code="c-9")))
            accepted.append(inbox.offer(link(query["state"], code="again")))

        threading.Timer(0.05, returns).start()

    tokens = grimmory_sso.sign_in(client, sso, inbox, Event(), browser)
    assert tokens == GrimmoryTokens("a1", "r1") == client.tokens
    assert seen == [tokens]
    assert accepted == [False, False, True, False]

    query = {k: v[0] for k, v in parse_qs(urlsplit(opened[0]).query).items()}
    assert urlsplit(opened[0]).netloc == "id.example" and query["tenant"] == "1"
    assert query["client_id"] == "grimmory-web"
    assert query["code_challenge_method"] == "S256"
    assert query["state"] == "s-123"
    (url, _headers) = posted[0]
    sent = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert sent["code"] == "c-9"
    assert sent["state"] == "s-123"
    assert sent["nonce"] == query["nonce"]
    assert sent["redirect_uri"] == "iopenpod://oauth2-callback"
    digest = hashlib.sha256(sent["code_verifier"].encode()).digest()
    assert (
        base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        == query["code_challenge"]
    )
    # A link after the sign-in finished is not taken.
    assert not inbox.offer(link("s-123", code="late"))


def test_provider_errors_cancelling_and_waiting_too_long() -> None:
    inbox = grimmory_sso.SsoInbox()
    inbox.expect("s")
    assert inbox.offer(link("s", error="access_denied"))
    with pytest.raises(ConnectedError, match="access_denied"):
        inbox.wait(Event())

    inbox.expect("s")
    cancelled = Event()
    cancelled.set()
    with pytest.raises(CancelledError):
        inbox.wait(cancelled)

    inbox.expect("s")
    with pytest.raises(ConnectedError, match="identity provider"):
        inbox.wait(Event(), timeout=0.3)
    assert not inbox.offer(link("s", code="late"))  # nobody waits any more


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        (400, "Invalid redirect URI", "Mobile Redirect URIs"),
        (400, "Invalid or expired OIDC state parameter", "five minutes"),
        (403, "OIDC user 'w' is not provisioned", "no account"),
        (502, "Failed to exchange authorization code", "allowed redirect"),
    ],
)
def test_grimmory_refusals_are_explained(
    status: int, message: str, expected: str
) -> None:
    transport = FakeTransport(
        {
            f"POST {BASE}/api/v1/auth/oidc/mobile/callback": ConnectedError(
                f"The server answered HTTP {status}: {message}.", status
            )
        }
    )
    request = grimmory_sso.SsoRequest("https://id/authorize", "s", "n", "v")
    with pytest.raises(ConnectedError, match=expected):
        grimmory_sso.finish(GrimmoryClient(BASE, transport=transport), request, "c")
