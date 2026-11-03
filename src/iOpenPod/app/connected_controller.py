"""Qt adapter for Connected Sources: background work, state, and signals.

Network and Host-folder work runs on a worker thread, one job at a time. iPod
Extras publication additionally reserves the Active iPod exclusively, like game
installation. Previews load on a separate small pool so browsing stays quick.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal

from iOpenPod.app.connected import calendars as calendar_files
from iOpenPod.app.connected.config import (
    CalendarFile,
    ConnectedConfig,
    ConnectedConfigStore,
    GrimmoryPick,
    ImmichPick,
    PlexPick,
)
from iOpenPod.app.connected.grimmory import GrimmoryClient, plan_grimmory
from iOpenPod.app.connected.http import CancelledError, ConnectedError
from iOpenPod.app.connected.immich import ImmichClient, plan_immich
from iOpenPod.app.connected.materialize import (
    Desired,
    RefreshReport,
    refresh_folder,
    safe_component,
)
from iOpenPod.app.connected.memos import (
    MemosClient,
    only_mine,
    plan_notes,
    summarize,
)
from iOpenPod.app.connected.plex import (
    PlexClient,
    PlexPin,
    PlexServer,
    PlexServerChoice,
    client_identifier,
)
from iOpenPod.app.connected.plex_ratings import RatingPlan, plan_ratings
from iOpenPod.app.connected.plex_source import plan_plex
from iOpenPod.app.connected.secrets import SecretKey, SecretStore, SystemSecretStore
from iOpenPod.app.host_media_folders import (
    HostMediaFolder,
    HostMediaType,
    load_host_media_folders,
    same_host_media_folder,
    save_host_media_folders,
)
from iOpenPod.app.services.ipod_extras import ExtrasFile, ExtrasResult, ExtrasSection
from storage import HostPath

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from iOpenPod.app.core.settings.service import SettingsService
    from iOpenPod.app.device_controller import DeviceController
    from iOpenPod.app.services.device_coordinator import DeviceCoordinator
    from iPodDB.library import Track

_LOGGER = logging.getLogger(__name__)
PLEX_FOLDER = "Plex"
IMMICH_FOLDER = "Immich"
AUDIOBOOKS_FOLDER = "Audiobooks"
_SIGN_IN_POLL = 2.0


class ConnectedSource:
    PLEX = "plex"
    IMMICH = "immich"
    GRIMMORY = "grimmory"


_FOLDER_TYPES: dict[str, tuple[str, frozenset[HostMediaType]]] = {
    ConnectedSource.PLEX: (
        PLEX_FOLDER,
        frozenset({HostMediaType.AUDIO, HostMediaType.PLAYLISTS}),
    ),
    ConnectedSource.IMMICH: (IMMICH_FOLDER, frozenset({HostMediaType.PHOTOS})),
    ConnectedSource.GRIMMORY: (AUDIOBOOKS_FOLDER, frozenset({HostMediaType.AUDIO})),
}


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    source: str
    report: RefreshReport
    notes: tuple[str, ...] = ()


class _Signals(QObject):
    done = Signal(object, object)  # result, error
    progress = Signal(str)


class _Job(QRunnable):
    def __init__(self, work: Callable[[_Job], object]) -> None:
        super().__init__()
        self.work = work
        self.cancelled = threading.Event()
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.work(self)
        except CancelledError:
            self.signals.done.emit(None, "Cancelled.")
        except ConnectedError as error:
            self.signals.done.emit(None, str(error))
        except Exception as error:
            _LOGGER.exception("Connected Source work failed")
            self.signals.done.emit(None, f"Something went wrong: {error}")
        else:
            self.signals.done.emit(result, None)


class _PreviewSignals(QObject):
    loaded = Signal(str, bytes)


class _Preview(QRunnable):
    def __init__(self, key: str, load: Callable[[], bytes]) -> None:
        super().__init__()
        self.key = key
        self.load = load
        self.signals = _PreviewSignals()

    def run(self) -> None:
        try:
            data = self.load()
        except Exception:
            data = b""
        self.signals.loaded.emit(self.key, data)


class ConnectedController(QObject):
    changed = Signal()
    message = Signal(str)
    progress = Signal(str)
    signInUrl = Signal(str)
    result = Signal(str, object)  # operation name, value
    previewLoaded = Signal(str, bytes)

    def __init__(
        self,
        settings: SettingsService,
        config_store: ConnectedConfigStore,
        devices: DeviceController,
        coordinator: DeviceCoordinator,
        parent: QObject | None = None,
        *,
        secrets: SecretStore | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._store = config_store
        self._devices = devices
        self._coordinator = coordinator
        self._secrets = secrets or SystemSecretStore()
        self._config = config_store.load()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._previews = QThreadPool(self)
        self._previews.setMaxThreadCount(4)
        self._job: _Job | None = None
        self._operation = ""
        self._exclusive = False
        self._closed = False
        self.plex = PlexClient(client_identifier("desktop"))
        self._plex_server: PlexServer | None = None
        self._plex_pin: PlexPin | None = None
        self._plex_choices: tuple[PlexServerChoice, ...] = ()
        self._preview_cache: dict[str, bytes] = {}
        self._calendar_links: list[tuple[str, str]] | None = None

    # --- state ---------------------------------------------------------------------

    @property
    def config(self) -> ConnectedConfig:
        return self._config

    @property
    def busy(self) -> bool:
        return self._job is not None

    @property
    def operation(self) -> str:
        return self._operation

    @property
    def plex_server(self) -> PlexServer | None:
        return self._plex_server

    @property
    def plex_choices(self) -> tuple[PlexServerChoice, ...]:
        return self._plex_choices

    def folder(self, source: str) -> Path:
        return self._config.root() / _FOLDER_TYPES[source][0]

    def update_config(self, config: ConnectedConfig) -> None:
        self._config = config
        try:
            self._store.save(config)
        except OSError as error:
            self.message.emit(
                self.tr("Could not save Connected Source choices: {error}").format(
                    error=error
                )
            )
        self.changed.emit()

    def secret(self, key: SecretKey) -> dict[str, str]:
        try:
            return self._secrets.load(key) or {}
        except ConnectedError as error:
            self.message.emit(str(error))
            return {}

    def save_secret(self, key: SecretKey, value: dict[str, str]) -> bool:
        try:
            self._secrets.save(key, value)
        except ConnectedError as error:
            self.message.emit(str(error))
            return False
        return True

    def forget(self, key: SecretKey) -> None:
        try:
            self._secrets.remove(key)
        except ConnectedError as error:
            self.message.emit(str(error))
        if key is SecretKey.PLEX:
            self._plex_server = None
            self.update_config(
                replace(
                    self._config,
                    plex=replace(
                        self._config.plex, server_id="", server_name="", username=""
                    ),
                )
            )
        self.changed.emit()

    # --- job plumbing ----------------------------------------------------------------

    def _start(
        self,
        operation: str,
        work: Callable[[_Job], object],
        *,
        exclusive: bool = False,
    ) -> bool:
        if self._closed or self._job is not None:
            self.message.emit(
                self.tr("Another Connected Source task is still running.")
            )
            return False
        if exclusive:
            if not self._devices.begin_exclusive_operation():
                self.message.emit(
                    self.tr("The iPod is busy. Try again when it's idle.")
                )
                return False
            self._exclusive = True
        job = _Job(work)
        job.signals.done.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        job.signals.progress.connect(self.progress, Qt.ConnectionType.QueuedConnection)
        self._job = job
        self._operation = operation
        self.changed.emit()
        self._pool.start(job)
        return True

    def _finished(self, value: object, error: object) -> None:
        operation = self._operation
        self._job = None
        self._operation = ""
        if self._exclusive:
            self._exclusive = False
            self._devices.finish_exclusive_operation()
        if self._closed:
            return
        if isinstance(error, str) and error:
            self.message.emit(error)
        else:
            self.result.emit(operation, value)
        self.changed.emit()

    def run(self, operation: str, work: Callable[[_Job], object]) -> bool:
        """Run one page-supplied browsing task on the Connected Source worker."""

        return self._start(operation, work)

    def cancel(self) -> None:
        if self._job is not None:
            self._job.cancelled.set()

    def preview(self, key: str, load: Callable[[], bytes]) -> None:
        if key in self._preview_cache:
            self.previewLoaded.emit(key, self._preview_cache[key])
            return
        task = _Preview(key, load)
        task.signals.loaded.connect(
            self._preview_done, Qt.ConnectionType.QueuedConnection
        )
        self._previews.start(task)

    def _preview_done(self, key: str, data: bytes) -> None:
        if data:
            self._preview_cache[key] = data
        self.previewLoaded.emit(key, data)

    def shutdown(self) -> None:
        self._closed = True
        self.cancel()
        self._pool.waitForDone()
        self._previews.clear()
        self._previews.waitForDone()
        if self._exclusive:
            self._exclusive = False
            self._devices.finish_exclusive_operation()

    # --- Host Media Library registration ----------------------------------------------

    def register_folder(self, source: str) -> None:
        """Make the source's folder a Sync media folder with the right media types."""

        name, kinds = _FOLDER_TYPES[source]
        path = self._config.root() / name
        path.mkdir(parents=True, exist_ok=True)
        wanted = HostMediaFolder(HostPath(path), media_types=kinds)
        folders = load_host_media_folders(self._settings)
        if any(same_host_media_folder(folder, wanted) for folder in folders):
            return
        save_host_media_folders(self._settings, (*folders, wanted))

    # --- Plex --------------------------------------------------------------------------

    def plex_sign_in(self) -> None:
        def work(job: _Job) -> object:
            pin = self.plex.start_sign_in()
            self._plex_pin = pin
            job.signals.progress.emit(pin.auth_url)
            while not job.cancelled.is_set():
                approved = self.plex.poll_sign_in(pin)
                if approved is not None:
                    token, username = approved
                    choices = self.plex.servers(token)
                    return (token, username, choices)
                time.sleep(_SIGN_IN_POLL)
            raise CancelledError

        if self._start("plex-sign-in", work) and self._job is not None:
            self._job.signals.progress.disconnect(self.progress)
            self._job.signals.progress.connect(
                self.signInUrl, Qt.ConnectionType.QueuedConnection
            )

    def plex_signed_in(
        self, token: str, username: str, choices: tuple[PlexServerChoice, ...]
    ) -> None:
        self._plex_choices = choices
        secret = self.secret(SecretKey.PLEX)
        secret.update(account_token=token, username=username)
        self.save_secret(SecretKey.PLEX, secret)
        self.update_config(
            replace(self._config, plex=replace(self._config.plex, username=username))
        )

    def plex_list_servers(self) -> None:
        token = self.secret(SecretKey.PLEX).get("account_token", "")
        if not token:
            self.message.emit(self.tr("Sign in to Plex first."))
            return
        self._start("plex-servers", lambda _job: self.plex.servers(token))

    def set_plex_choices(self, choices: tuple[PlexServerChoice, ...]) -> None:
        self._plex_choices = choices
        self.changed.emit()

    def plex_choose_server(self, choice: PlexServerChoice) -> None:
        self._start("plex-connect", lambda _job: self.plex.connect(choice))

    def plex_connect_manual(self, address: str) -> None:
        token = self.secret(SecretKey.PLEX).get("account_token", "")
        self._start(
            "plex-connect", lambda _job: self.plex.connect_manual(address, token)
        )

    def plex_connected(self, server: PlexServer) -> None:
        self._plex_server = server
        secret = self.secret(SecretKey.PLEX)
        secret.update(
            server_token=server.token, server_uri=server.uri, server_id=server.id
        )
        self.save_secret(SecretKey.PLEX, secret)
        self.update_config(
            replace(
                self._config,
                plex=replace(
                    self._config.plex, server_id=server.id, server_name=server.name
                ),
            )
        )
        self.register_folder(ConnectedSource.PLEX)

    def plex_restore(self) -> bool:
        """Reuse the saved server without a network round trip."""

        if self._plex_server is not None:
            return True
        secret = self.secret(SecretKey.PLEX)
        if secret.get("server_token") and secret.get("server_uri"):
            self._plex_server = PlexServer(
                secret.get("server_id", ""),
                self._config.plex.server_name or secret["server_uri"],
                secret["server_uri"],
                secret["server_token"],
            )
            return True
        return False

    def run_plex(
        self, operation: str, work: Callable[[PlexServer, _Job], object]
    ) -> None:
        if not self.plex_restore() or self._plex_server is None:
            self.message.emit(self.tr("Choose a Plex server first."))
            return
        server = self._plex_server
        self._start(operation, lambda job: work(server, job))

    def set_plex_picks(self, picks: tuple[PlexPick, ...]) -> None:
        self.update_config(
            replace(self._config, plex=replace(self._config.plex, picks=picks))
        )

    def plex_ratings_plan(self, tracks: tuple[Track, ...]) -> None:
        """Compare the Library Workspace's Tracks with Plex ratings."""

        if self._devices.active_ipod is None:
            self.message.emit(self.tr("Connect an iPod to sync star ratings."))
            return

        def work(server: PlexServer, job: _Job) -> RatingPlan:
            job.signals.progress.emit(self.tr("Reading your Plex music library…"))
            return plan_ratings(tracks, self.plex.library_tracks(server, job.cancelled))

        self.run_plex("plex-ratings-plan", work)

    def plex_send_ratings(self, plan: RatingPlan) -> None:
        def work(server: PlexServer, job: _Job) -> int:
            for index, change in enumerate(plan.to_plex, start=1):
                if job.cancelled.is_set():
                    raise CancelledError
                job.signals.progress.emit(
                    self.tr("Setting Plex stars {index} of {total}…").format(
                        index=index, total=len(plan.to_plex)
                    )
                )
                self.plex.rate(server, change.track.rating_key, change.rating)
            return len(plan.to_plex)

        self.run_plex("plex-ratings-send", work)

    # --- refresh (download selections into Connected Source Folders) --------------------

    def refresh(self, sources: tuple[str, ...]) -> bool:
        config = self._config

        def work(job: _Job) -> tuple[RefreshOutcome, ...]:
            return tuple(
                self._refresh_source(source, config, job) for source in sources
            )

        return self._start("refresh", work)

    def refresh_before_sync(self) -> bool:
        """Refresh every source with a Selection; returns False when nothing to do."""

        sources = tuple(
            source
            for source, picks in (
                (ConnectedSource.PLEX, self._config.plex.picks),
                (ConnectedSource.IMMICH, self._config.immich.picks),
                (ConnectedSource.GRIMMORY, self._config.grimmory.picks),
            )
            if picks
        )
        return bool(sources) and self.refresh(sources)

    def _refresh_source(
        self, source: str, config: ConnectedConfig, job: _Job
    ) -> RefreshOutcome:
        emit = job.signals.progress.emit
        folder = config.root() / _FOLDER_TYPES[source][0]
        notes: list[str] = []
        items: tuple[Desired, ...]
        if source == ConnectedSource.PLEX:
            if not self.plex_restore() or self._plex_server is None:
                raise ConnectedError("Choose a Plex server before downloading.")
            plan = plan_plex(
                self.plex,
                self._plex_server,
                config.plex,
                cancelled=job.cancelled,
                progress=emit,
            )
            items = plan.items
            notes.extend(plan.skipped)
            label = "Plex"
        elif source == ConnectedSource.IMMICH:
            key = self.secret(SecretKey.IMMICH).get("api_key", "")
            immich = ImmichClient(config.immich.url, key)
            items = plan_immich(
                immich, config.immich, cancelled=job.cancelled, progress=emit
            )
            label = "Immich"
        else:
            secret = self.secret(SecretKey.GRIMMORY)
            client = GrimmoryClient(
                config.grimmory.url,
                config.grimmory.username,
                secret.get("password", ""),
            )
            items = plan_grimmory(
                client, config.grimmory, cancelled=job.cancelled, progress=emit
            )
            label = "Grimmory"

        def report(name: str, index: int, total: int) -> None:
            if name:
                emit(f"{label}: {index + 1} of {total} · {name.rsplit('/', 1)[-1]}")

        outcome = refresh_folder(
            folder, items, cancelled=job.cancelled, progress=report
        )
        return RefreshOutcome(source, outcome, tuple(notes))

    # --- Immich -------------------------------------------------------------------------

    def immich(self) -> ImmichClient:
        return ImmichClient(
            self._config.immich.url, self.secret(SecretKey.IMMICH).get("api_key", "")
        )

    def immich_connect(self, url: str, api_key: str) -> None:
        key = api_key.strip() or self.secret(SecretKey.IMMICH).get("api_key", "")

        def work(_job: _Job) -> object:
            client = ImmichClient(url, key)
            user = client.ping()
            return (user, client.albums(), client.people())

        self.update_config(
            replace(self._config, immich=replace(self._config.immich, url=url.strip()))
        )
        if api_key.strip():
            self.save_secret(SecretKey.IMMICH, {"api_key": api_key.strip()})
        self.register_folder(ConnectedSource.IMMICH)
        self._start("immich-connect", work)

    def set_immich_picks(self, picks: tuple[ImmichPick, ...]) -> None:
        self.update_config(
            replace(self._config, immich=replace(self._config.immich, picks=picks))
        )

    # --- Grimmory -----------------------------------------------------------------------

    def grimmory(self) -> GrimmoryClient:
        return GrimmoryClient(
            self._config.grimmory.url,
            self._config.grimmory.username,
            self.secret(SecretKey.GRIMMORY).get("password", ""),
        )

    def grimmory_connect(self, url: str, username: str, password: str) -> None:
        self.update_config(
            replace(
                self._config,
                grimmory=replace(
                    self._config.grimmory, url=url.strip(), username=username.strip()
                ),
            )
        )
        if password:
            self.save_secret(SecretKey.GRIMMORY, {"password": password})
        self.register_folder(ConnectedSource.GRIMMORY)

        def work(_job: _Job) -> object:
            client = self.grimmory()
            client.check()
            return (client.shelves("libraries"), client.shelves("shelves"))

        self._start("grimmory-connect", work)

    def set_grimmory_picks(self, picks: tuple[GrimmoryPick, ...]) -> None:
        self.update_config(
            replace(self._config, grimmory=replace(self._config.grimmory, picks=picks))
        )

    # --- Memos → Notes -----------------------------------------------------------------

    def memos(self) -> MemosClient:
        return MemosClient(
            self._config.memos.url, self.secret(SecretKey.MEMOS).get("token", "")
        )

    def memos_connect(self, url: str, token: str) -> None:
        self.update_config(
            replace(self._config, memos=replace(self._config.memos, url=url.strip()))
        )
        if token.strip():
            self.save_secret(SecretKey.MEMOS, {"token": token.strip()})
        config = self._config.memos

        def work(job: _Job) -> object:
            client = self.memos()
            user = client.current_user()
            memos = client.memos(
                include_archived=config.include_archived, cancelled=job.cancelled
            )
            if config.only_mine:
                memos = only_mine(memos, user)
            return summarize(memos, (user[1] if user else "") or "Memos user")

        self._start("memos-connect", work)

    def publish_notes(self) -> None:
        active = self._devices.active_ipod
        if active is None:
            self.message.emit(self.tr("Connect an iPod to send notes."))
            return
        config = self._config.memos

        def work(job: _Job) -> object:
            client = self.memos()
            job.signals.progress.emit(self.tr("Reading memos…"))
            memos = client.memos(
                include_archived=config.include_archived, cancelled=job.cancelled
            )
            if config.only_mine:
                memos = only_mine(memos, client.current_user())
            others = self._coordinator.count_foreign_extras(active, ExtrasSection.NOTES)
            plan = plan_notes(memos, config, other_notes=others)
            job.signals.progress.emit(self.tr("Writing notes to the iPod…"))
            result = self._coordinator.publish_extras(
                active,
                ExtrasSection.NOTES,
                tuple(ExtrasFile(item.relative, item.data) for item in plan.files),
                job.cancelled,
            )
            return (plan, result)

        self._start("notes-publish", work, exclusive=True)

    # --- Calendars ----------------------------------------------------------------------

    def calendar_links(self) -> list[tuple[str, str]]:
        """``(link, name)`` pairs; links can embed secret tokens, so they live in the vault."""

        if self._calendar_links is None:
            secret = self.secret(SecretKey.CALENDARS)
            links: list[tuple[str, str]] = []
            for index in range(int(secret.get("count", "0") or 0)):
                link = secret.get(f"url{index}", "")
                if link:
                    links.append((link, secret.get(f"name{index}", "")))
            self._calendar_links = links
        return list(self._calendar_links)

    def set_calendar_links(self, links: list[tuple[str, str]]) -> None:
        value = {"count": str(len(links))}
        for index, (link, name) in enumerate(links):
            value[f"url{index}"] = link
            value[f"name{index}"] = name
        if self.save_secret(SecretKey.CALENDARS, value):
            self._calendar_links = list(links)
            self.changed.emit()

    def calendar_directory(self) -> Path:
        return self._store.directory / "connected-calendars"

    def add_calendar_file(self, source: Path) -> None:
        data = source.read_bytes()
        if len(data) > calendar_files.MAX_CALENDAR_BYTES:
            self.message.emit(self.tr("That calendar is larger than 50 MB."))
            return
        text = data.decode("utf-8-sig", errors="replace")
        try:
            info = calendar_files.calendar_info(text)
        except ConnectedError as error:
            self.message.emit(str(error))
            return
        directory = self.calendar_directory()
        directory.mkdir(parents=True, exist_ok=True)
        stem = safe_component(source.stem, "Calendar", limit=60)
        filename, counter = f"{stem}.ics", 2
        existing = {item.filename.casefold() for item in self._config.calendars.files}
        while filename.casefold() in existing:
            filename, counter = f"{stem} ({counter}).ics", counter + 1
        (directory / filename).write_text(text, encoding="utf-8")
        files = (
            *self._config.calendars.files,
            CalendarFile(info.name or source.stem, filename),
        )
        self.update_config(
            replace(
                self._config, calendars=replace(self._config.calendars, files=files)
            )
        )

    def remove_calendar_file(self, filename: str) -> None:
        (self.calendar_directory() / filename).unlink(missing_ok=True)
        files = tuple(
            item for item in self._config.calendars.files if item.filename != filename
        )
        self.update_config(
            replace(
                self._config, calendars=replace(self._config.calendars, files=files)
            )
        )

    def publish_calendars(self) -> None:
        active = self._devices.active_ipod
        if active is None:
            self.message.emit(self.tr("Connect an iPod to send calendars."))
            return
        config = self._config.calendars
        links = self.calendar_links()
        directory = self.calendar_directory()

        def work(job: _Job) -> object:
            sources: list[tuple[str, str]] = []
            issues: list[str] = []
            for item in config.files:
                try:
                    sources.append(
                        (
                            item.name,
                            (directory / item.filename).read_text(encoding="utf-8"),
                        )
                    )
                except OSError as error:
                    issues.append(f"{item.name}: {error}")
            for index, (link, name) in enumerate(links, start=1):
                job.signals.progress.emit(
                    self.tr("Downloading calendar {index} of {total}…").format(
                        index=index, total=len(links)
                    )
                )
                try:
                    text = calendar_files.fetch_calendar(link)
                except ConnectedError as error:
                    issues.append(f"{calendar_files.redact(link)}: {error}")
                    continue
                sources.append(
                    (
                        name
                        or calendar_files.calendar_info(text).name
                        or f"Calendar {index}",
                        text,
                    )
                )
            files: list[ExtrasFile] = []
            used: set[str] = set()
            dropped = 0
            for name, text in sources:
                trimmed = calendar_files.trim_calendar(text, config.trim_days)
                dropped += trimmed.dropped
                body = calendar_files.rename_calendar(trimmed.text, name)
                stem = safe_component(name, "Calendar", limit=60)
                filename, counter = f"{stem}.ics", 2
                while filename.casefold() in used:
                    filename, counter = f"{stem} ({counter}).ics", counter + 1
                used.add(filename.casefold())
                files.append(ExtrasFile(filename, body.encode("utf-8")))
            job.signals.progress.emit(self.tr("Writing calendars to the iPod…"))
            result = self._coordinator.publish_extras(
                active, ExtrasSection.CALENDARS, tuple(files), job.cancelled
            )
            merged = ExtrasResult(
                result.written,
                result.unchanged,
                result.removed,
                (*issues, *result.issues),
            )
            return (len(files), dropped, merged)

        self._start("calendars-publish", work, exclusive=True)


__all__ = ["ConnectedController", "ConnectedSource", "RefreshOutcome"]
