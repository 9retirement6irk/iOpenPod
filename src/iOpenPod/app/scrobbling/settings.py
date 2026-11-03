"""Only non-secret scrobbling preferences belong in global settings."""

from iOpenPod.app.core.settings.definitions import SettingDefinition
from iOpenPod.app.core.settings.service import SettingsService

from .models import Account, Service

SCROBBLE_DURING_SYNC = SettingDefinition(
    "sync/scrobble", bool, True, device_overridable=True
)
LASTFM_USERNAME = SettingDefinition("scrobbling/lastfm-username", str, "")
LISTENBRAINZ_USERNAME = SettingDefinition("scrobbling/listenbrainz-username", str, "")
PLEX_USERNAME = SettingDefinition("scrobbling/plex-username", str, "")


def account_setting(service: Service) -> SettingDefinition[str]:
    return {
        Service.LASTFM: LASTFM_USERNAME,
        Service.LISTENBRAINZ: LISTENBRAINZ_USERNAME,
        Service.PLEX: PLEX_USERNAME,
    }[service]


def configured_accounts(settings: SettingsService) -> tuple[Account, ...]:
    return tuple(
        Account(service, username)
        for service in Service
        if (username := settings.get(account_setting(service)))
    )
