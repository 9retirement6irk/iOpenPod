"""Grimmory single sign-on: PKCE, the loopback answer, and Grimmory's callback."""

import json
import socket
import urllib.request
from collections.abc import Mapping
from threading import Event
from urllib.error import HTTPError
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


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def answer(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:
        body: bytes = response.read()
    return body.decode()


def server(posted: list[tuple[dict[str, str], object]]) -> FakeTransport:
    def callback(
        _m: str, _u: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResponse:
        posted.append((dict(headers), json.loads(body or b"{}")))
        return reply({"accessToken": "a1", "refreshToken": "r1"})

    return FakeTransport(
        {
            f"GET {BASE}/api/v1/public-settings": SETTINGS,
            f"GET {BASE}/api/v1/auth/oidc/state": {"state": "s-123"},
            f"GET {ISSUER}/.well-known/openid-configuration": {
                "authorization_endpoint": "https://id.example/authorize?tenant=1"
            },
            f"POST {BASE}/api/v1/auth/oidc/callback": callback,
        }
    )


def test_provider_reads_public_settings() -> None:
    client = GrimmoryClient(BASE, transport=server([]))
    sso = grimmory_sso.provider(client)
    assert sso == grimmory_sso.SsoProvider(
        "Authentik",
        ISSUER,
        "grimmory-web",
        "openid profile email groups offline_access",
        False,
    )
    off = FakeTransport({f"GET {BASE}/api/v1/public-settings": {"oidcEnabled": False}})
    assert grimmory_sso.provider(GrimmoryClient(BASE, transport=off)) is None


def test_full_sign_in_through_the_loopback_address() -> None:
    port = free_port()
    posted: list[tuple[dict[str, str], object]] = []
    seen: list[GrimmoryTokens] = []
    client = GrimmoryClient(BASE, transport=server(posted), on_tokens=seen.append)
    sso = grimmory_sso.provider(client)
    assert sso is not None
    opened: list[str] = []
    pages: list[str] = []

    def browser(url: str) -> None:
        opened.append(url)
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert query["redirect_uri"] == f"http://127.0.0.1:{port}/oauth2-callback"
        base = query["redirect_uri"]
        # A stray request with the wrong state is ignored, not accepted.
        pages.append(answer(f"{base}?{urlencode({'code': 'evil', 'state': 'x'})}"))
        pages.append(
            answer(f"{base}?{urlencode({'code': 'c-9', 'state': query['state']})}")
        )

    tokens = grimmory_sso.sign_in(client, sso, Event(), browser, port=port)
    assert tokens == GrimmoryTokens("a1", "r1") == client.tokens
    assert seen == [tokens]
    assert "expired" in pages[0] and "signed in" in pages[1]

    url = urlsplit(opened[0])
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "id.example" and query["tenant"] == "1"
    assert query["client_id"] == "grimmory-web"
    assert query["code_challenge_method"] == "S256"
    assert query["state"] == "s-123"
    assert "offline_access" in query["scope"]
    (headers, body) = posted[0]
    assert headers["Origin"] == f"http://127.0.0.1:{port}"
    assert isinstance(body, dict)
    assert body["code"] == "c-9"
    assert body["state"] == "s-123"
    assert body["nonce"] == query["nonce"]
    assert body["redirectUri"] == query["redirect_uri"]
    # The challenge sent to the provider is the S256 hash of the verifier Grimmory gets.
    import base64
    import hashlib

    digest = hashlib.sha256(str(body["codeVerifier"]).encode()).digest()
    assert (
        base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        == query["code_challenge"]
    )
    # The listener has stopped.
    with pytest.raises(OSError):
        answer(f"http://127.0.0.1:{port}/oauth2-callback")


def test_provider_errors_and_cancelling() -> None:
    port = free_port()
    receiver = grimmory_sso.LoopbackReceiver("s", port)
    answer(
        f"http://127.0.0.1:{port}/oauth2-callback?"
        + urlencode({"state": "s", "error": "access_denied"})
    )
    with pytest.raises(ConnectedError, match="access_denied"):
        receiver.wait(Event())

    cancelled = Event()
    cancelled.set()
    with pytest.raises(CancelledError):
        grimmory_sso.LoopbackReceiver("s", port).wait(cancelled)

    receiver = grimmory_sso.LoopbackReceiver("s", port)
    with pytest.raises(HTTPError):
        answer(f"http://127.0.0.1:{port}/elsewhere")
    with pytest.raises(ConnectedError, match="five minutes"):
        receiver.wait(Event(), timeout=0.3)


def test_busy_port_is_named() -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        with pytest.raises(ConnectedError, match=f"port {port}"):
            grimmory_sso.LoopbackReceiver("s", port)


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        (400, "Invalid redirect URI", "sign-in address"),
        (400, "Invalid or expired OIDC state parameter", "five minutes"),
        (403, "OIDC user 'w' is not provisioned", "no account"),
        (403, "Invalid CORS request", "app.cors.allowed-origins"),
        (502, "Failed to exchange authorization code", "redirect address"),
    ],
)
def test_grimmory_refusals_are_explained(
    status: int, message: str, expected: str
) -> None:
    transport = FakeTransport(
        {
            f"POST {BASE}/api/v1/auth/oidc/callback": ConnectedError(
                f"The server answered HTTP {status}: {message}.", status
            )
        }
    )
    request = grimmory_sso.SsoRequest(
        "https://id/authorize", "s", "n", "v", grimmory_sso.redirect_uri()
    )
    with pytest.raises(ConnectedError, match=expected):
        grimmory_sso.finish(GrimmoryClient(BASE, transport=transport), request, "c")
