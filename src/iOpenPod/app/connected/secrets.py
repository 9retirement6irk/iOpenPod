"""Connected Source secrets live in the native OS vault, never in preferences.

Plex tokens, Immich API keys, Memos access tokens, Grimmory OPDS passwords and
private calendar subscription links can each grant access to a person's data, so
they are kept beside the Scrobbling credentials in the platform keyring.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Protocol, cast

import keyring
from keyring.backends.chainer import ChainerBackend

from .http import ConnectedError

_VAULT_SERVICE = "iOpenPod 2 Connected Sources"
_NATIVE_MODULES = frozenset(
    {
        "keyring.backends.Windows",
        "keyring.backends.macOS",
        "keyring.backends.SecretService",
        "keyring.backends.kwallet",
    }
)


class SecretKey(StrEnum):
    PLEX = "plex"
    IMMICH = "immich"
    GRIMMORY = "grimmory"
    MEMOS = "memos"
    CALENDARS = "calendars"


type Secret = dict[str, str]


class SecretStore(Protocol):
    def load(self, key: SecretKey) -> Secret | None: ...
    def save(self, key: SecretKey, secret: Secret) -> None: ...
    def remove(self, key: SecretKey) -> None: ...


class _NativeVault(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


class SystemSecretStore:
    def _backend(self) -> _NativeVault:
        backend = keyring.get_keyring()
        candidates = (
            backend.backends if isinstance(backend, ChainerBackend) else [backend]
        )
        for candidate in candidates:
            if type(candidate).__module__ in _NATIVE_MODULES:
                return cast("_NativeVault", candidate)
        raise ConnectedError(
            "A native system credential store is unavailable. Unlock or enable your OS keyring and try again."
        )

    def load(self, key: SecretKey) -> Secret | None:
        try:
            raw = self._backend().get_password(_VAULT_SERVICE, key.value)
            if raw is None:
                return None
            value: object = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            items = cast("dict[object, object]", value)
            if not all(
                isinstance(k, str) and isinstance(v, str) for k, v in items.items()
            ):
                raise ValueError
            return cast("Secret", items)
        except ConnectedError:
            raise
        except Exception:
            raise ConnectedError(
                "Could not read saved sign-in details from your OS keyring. Connect again."
            ) from None

    def save(self, key: SecretKey, secret: Secret) -> None:
        try:
            self._backend().set_password(
                _VAULT_SERVICE, key.value, json.dumps(secret, sort_keys=True)
            )
        except ConnectedError:
            raise
        except Exception:
            raise ConnectedError(
                "Could not save sign-in details in your OS keyring."
            ) from None

    def remove(self, key: SecretKey) -> None:
        try:
            backend = self._backend()
            if backend.get_password(_VAULT_SERVICE, key.value) is not None:
                backend.delete_password(_VAULT_SERVICE, key.value)
        except ConnectedError:
            raise
        except Exception:
            raise ConnectedError(
                "Could not remove sign-in details from your OS keyring."
            ) from None


class MemorySecretStore:
    """A process-local store for tests and keyring-less diagnostics."""

    def __init__(self) -> None:
        self._values: dict[SecretKey, Secret] = {}

    def load(self, key: SecretKey) -> Secret | None:
        value = self._values.get(key)
        return None if value is None else dict(value)

    def save(self, key: SecretKey, secret: Secret) -> None:
        self._values[key] = dict(secret)

    def remove(self, key: SecretKey) -> None:
        self._values.pop(key, None)


__all__ = [
    "MemorySecretStore",
    "Secret",
    "SecretKey",
    "SecretStore",
    "SystemSecretStore",
]
