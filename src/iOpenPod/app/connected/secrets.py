"""Connected Source secrets live in the native OS vault, never in preferences.

Plex tokens, Immich API keys, Memos access tokens, Grimmory sign-in tokens and
private calendar subscription links can each grant access to a person's data, so
they are kept beside the Scrobbling credentials in the platform keyring.
"""

from __future__ import annotations

import json
import threading
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


# Every Connected Source secret lives in this one vault item. One item means the
# OS asks for permission once, where one item per service asked once for each.
_VAULT_ITEM = "all"


class SystemSecretStore:
    """Keep every Connected Source secret in one native vault item.

    The item is read once and remembered for the session, so the OS vault (and
    its permission prompt) is consulted once however often secrets are needed.
    Items saved one per service by earlier versions are gathered into it the
    first time, then left alone.
    """

    def __init__(self) -> None:
        self._values: dict[str, Secret] | None = None
        self._lock = threading.Lock()

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

    def _all(self) -> dict[str, Secret]:
        """Every secret, read from the vault once per session."""

        if self._values is not None:
            return self._values
        backend = self._backend()
        raw = backend.get_password(_VAULT_SERVICE, _VAULT_ITEM)
        if raw is not None:
            document: object = json.loads(raw)
            if not isinstance(document, dict):
                raise ValueError
            values = {
                str(key): _secret(value)
                for key, value in cast("dict[object, object]", document).items()
            }
        else:
            # First run of this layout: gather the per-service items once.
            values = {}
            for key in SecretKey:
                legacy = backend.get_password(_VAULT_SERVICE, key.value)
                if legacy is not None:
                    values[key.value] = _secret(json.loads(legacy))
            backend.set_password(
                _VAULT_SERVICE, _VAULT_ITEM, json.dumps(values, sort_keys=True)
            )
        self._values = values
        return values

    def load(self, key: SecretKey) -> Secret | None:
        try:
            with self._lock:
                value = self._all().get(key.value)
            return None if value is None else dict(value)
        except ConnectedError:
            raise
        except Exception:
            raise ConnectedError(
                "Could not read saved sign-in details from your OS keyring. Connect again."
            ) from None

    def save(self, key: SecretKey, secret: Secret) -> None:
        try:
            with self._lock:
                values = {**self._all(), key.value: dict(secret)}
                self._backend().set_password(
                    _VAULT_SERVICE, _VAULT_ITEM, json.dumps(values, sort_keys=True)
                )
                self._values = values
        except ConnectedError:
            raise
        except Exception:
            raise ConnectedError(
                "Could not save sign-in details in your OS keyring."
            ) from None

    def remove(self, key: SecretKey) -> None:
        try:
            with self._lock:
                current = self._all()
                if key.value not in current:
                    return
                values = {k: v for k, v in current.items() if k != key.value}
                self._backend().set_password(
                    _VAULT_SERVICE, _VAULT_ITEM, json.dumps(values, sort_keys=True)
                )
                self._values = values
        except ConnectedError:
            raise
        except Exception:
            raise ConnectedError(
                "Could not remove sign-in details from your OS keyring."
            ) from None


def _secret(value: object) -> Secret:
    if not isinstance(value, dict):
        raise ValueError
    items = cast("dict[object, object]", value)
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in items.items()):
        raise ValueError
    return cast("Secret", dict(items))


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
