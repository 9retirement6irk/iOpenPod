"""Connected Source secrets share one vault item, read once per session."""

import json

import keyring
import pytest

from iOpenPod.app.connected.http import ConnectedError
from iOpenPod.app.connected.secrets import SecretKey, SystemSecretStore

SERVICE = "iOpenPod 2 Connected Sources"


class Vault:
    """A native vault that counts reads: each read can be an OS prompt."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.reads: list[str] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.reads.append(username)
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[service, username] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[service, username]


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> Vault:
    backend = Vault()
    monkeypatch.setattr(Vault, "__module__", "keyring.backends.macOS")
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    return backend


def test_everything_is_one_item_read_once(vault: Vault) -> None:
    store = SystemSecretStore()
    store.save(SecretKey.PLEX, {"account_token": "p"})
    store.save(SecretKey.IMMICH, {"api_key": "i"})
    assert set(vault.values) == {(SERVICE, "all")}
    assert json.loads(vault.values[SERVICE, "all"]) == {
        "immich": {"api_key": "i"},
        "plex": {"account_token": "p"},
    }

    # A new session: however many secrets are asked for, the vault is read once.
    vault.reads.clear()
    later = SystemSecretStore()
    for _ in range(3):
        assert later.load(SecretKey.PLEX) == {"account_token": "p"}
        assert later.load(SecretKey.IMMICH) == {"api_key": "i"}
        assert later.load(SecretKey.MEMOS) is None
    assert vault.reads == ["all"]

    later.remove(SecretKey.PLEX)
    later.remove(SecretKey.GRIMMORY)  # nothing saved: nothing to do
    assert later.load(SecretKey.PLEX) is None
    assert SystemSecretStore().load(SecretKey.IMMICH) == {"api_key": "i"}
    # A caller can't change what the session remembers.
    later.load(SecretKey.IMMICH)["api_key"] = "changed"  # type: ignore[index]
    assert later.load(SecretKey.IMMICH) == {"api_key": "i"}


def test_per_service_items_from_earlier_versions_are_gathered_once(
    vault: Vault,
) -> None:
    vault.values[SERVICE, "plex"] = json.dumps({"account_token": "p"})
    vault.values[SERVICE, "calendars"] = json.dumps({"count": "1", "url0": "u"})
    store = SystemSecretStore()
    assert store.load(SecretKey.PLEX) == {"account_token": "p"}
    assert store.load(SecretKey.CALENDARS) == {"count": "1", "url0": "u"}
    assert json.loads(vault.values[SERVICE, "all"]) == {
        "calendars": {"count": "1", "url0": "u"},
        "plex": {"account_token": "p"},
    }

    # From then on only the one item is read, even if the old ones remain.
    vault.reads.clear()
    assert SystemSecretStore().load(SecretKey.PLEX) == {"account_token": "p"}
    assert vault.reads == ["all"]


def test_unreadable_vault_is_reported_without_the_secret(vault: Vault) -> None:
    vault.values[SERVICE, "all"] = "broken-secret"
    with pytest.raises(ConnectedError) as caught:
        SystemSecretStore().load(SecretKey.PLEX)
    assert "broken-secret" not in str(caught.value)
