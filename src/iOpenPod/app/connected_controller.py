"""Qt adapter for Connected Sources: background work, state, and signals.

Network and Host-folder work runs on a worker thread, one job at a time. iPod
Extras publication additionally reserves the Active iPod exclusively, like game
installation. Previews load on a separate small pool so browsing stays quick.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal

from iOpenPod.app.connected import calendars as calendar_files
from iOpenPod.app.connected import grimmory_sso
from iOpenPod.app.connected.config import (
    CalendarFile,
    CalendarsConfig,
    ConnectedConfig,
    ConnectedConfigStore,
    ContactsFile,
    GrimmoryMark,
    GrimmoryPick,
    ImmichPick,
    PlexPick,
    PlexVideoSection,
    PlexVideoUse,
)
from iOpenPod.app.connected.grimmory import (
    GrimmoryClient,
    GrimmoryCollection,
    GrimmoryProgress,
    GrimmoryTokens,
)
from iOpenPod.app.connected.grimmory_progress import (
    IPodBook,
    ProgressPlan,
    ipod_books,
    plan_progress,
)
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
from iOpenPod.app.connected.remote_source import ConnectedRemote
from iOpenPod.app.connected.secrets import SecretKey, SecretStore, SystemSecretStore
from iOpenPod.app.host_media_folders import (
    HostMediaFolder,
    HostMediaType,
    load_host_media_folders,
    same_host_media_folder,
    save_host_media_folders,
)
from iOpenPod.app.remote_media import RemoteCatalog
from iOpenPod.app.scrobbling.models import Credentials
from iOpenPod.app.services.ipod_extras import ExtrasFile, ExtrasSection
from storage import HostPath

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from iOpenPod.app.connected.immich import ImmichCollection
    from iOpenPod.app.connected.memos import Memo
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
    # Grimmory: listening positions sent before downloading.
    progress_sent: int = 0
    marks: tuple[GrimmoryMark, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncPreparation:
    refreshed: tuple[RefreshOutcome, ...]
    catalog: RemoteCatalog
    progress_sent: int = 0
    marks: tuple[GrimmoryMark, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtrasSent:
    notes: int = 0
    calendars: int = 0
    contacts: int = 0
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StarsSent:
    set: int
    failed: int
    plex_only: int  # Plex stars the iPod lacks; brought over from the Plex page


@dataclass(frozen=True, slots=True)
class ProgressSent:
    sent: int
    failed: tuple[str, ...]
    pulled: int
    failed_books: frozenset[str] = frozenset()


class _Signals(QObject):
    done = Signal(object, object)  # result, error
    progress = Signal(str)
    link = Signal(str)  # a sign-in page to open in the browser


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
        self._then: Callable[[object], None] | None = None
        self._operation = ""
        self._exclusive = False
        self._closed = False
        self.plex = PlexClient(client_identifier("desktop"))
        self._plex_server: PlexServer | None = None
        self._plex_pin: PlexPin | None = None
        self._plex_choices: tuple[PlexServerChoice, ...] = ()
        self._preview_cache: dict[str, bytes] = {}
        self._calendar_links: list[tuple[str, str]] | None = None
        self._remote: ConnectedRemote | None = None
        self._sso_inbox = grimmory_sso.SsoInbox()

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
        if key is SecretKey.GRIMMORY:
            self.update_config(
                replace(
                    self._config,
                    grimmory=replace(self._config.grimmory, username=""),
                )
            )
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
        then: Callable[[object], None] | None = None,
    ) -> bool:
        if self._closed or self._job is not None:
            self.message.emit(
                self.tr("Another Connected Source task is still running.")
            )
            return False
        if exclusive:
            if not self._devices.begin_exclusive_operation():
                _LOGGER.info("Connected %s not started: the iPod is busy", operation)
                self.message.emit(
                    self.tr("The iPod is busy. Try again when it's idle.")
                )
                return False
            self._exclusive = True
        job = _Job(work)
        job.signals.done.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        job.signals.progress.connect(self.progress, Qt.ConnectionType.QueuedConnection)
        job.signals.link.connect(self.signInUrl, Qt.ConnectionType.QueuedConnection)
        self._job = job
        self._then = then
        self._operation = operation
        self.changed.emit()
        self._pool.start(job)
        return True

    def _finished(self, value: object, error: object) -> None:
        operation = self._operation
        then = self._then
        self._job = None
        self._then = None
        self._operation = ""
        if self._exclusive:
            self._exclusive = False
            self._devices.finish_exclusive_operation()
        if self._closed:
            return
        if isinstance(error, str) and error:
            # Otherwise only a status line that is gone in seconds.
            _LOGGER.warning("Connected %s failed: %s", operation, error)
            self.message.emit(error)
        else:
            if then is not None:
                then(value)
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

    def unregister_streamed(self) -> None:
        """Plex and Grimmory join Sync from their catalogs, not as scanned folders."""

        streamed = [
            HostMediaFolder(HostPath(self.folder(source)))
            for source in (ConnectedSource.PLEX, ConnectedSource.GRIMMORY)
        ]
        folders = load_host_media_folders(self._settings)
        kept = tuple(
            folder
            for folder in folders
            if not any(same_host_media_folder(folder, item) for item in streamed)
        )
        if len(kept) != len(folders):
            save_host_media_folders(self._settings, kept)

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
        self.unregister_streamed()

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

    def plex_credentials(self) -> Credentials | None:
        """The chosen Plex server, as Scrobbling credentials for sending plays."""

        secret = self.secret(SecretKey.PLEX)
        if not secret.get("server_token") or not secret.get("server_uri"):
            return None
        return Credentials(
            self._config.plex.username or "Plex",
            secret["server_token"],
            secret["server_uri"],
            secret.get("server_id", ""),
        )

    def plex_push_stars(self, tracks: tuple[Track, ...]) -> bool:
        """Set Plex stars from the iPod's, without asking (part of Scrobble now).

        Only the iPod's ratings move; Plex-only stars come to the iPod through the
        Plex page's reviewed two-way sync, since they change the iPod Library.
        """

        if not self.plex_restore() or self._plex_server is None:
            return False

        def work(server: PlexServer, job: _Job) -> StarsSent:
            job.signals.progress.emit(self.tr("Comparing star ratings with Plex…"))
            plan = plan_ratings(
                tracks,
                self.plex.library_tracks(
                    server, job.cancelled, skip=self._config.plex.skipped_sections
                ),
            )
            failed = 0
            for index, change in enumerate(plan.to_plex, start=1):
                if job.cancelled.is_set():
                    raise CancelledError
                job.signals.progress.emit(
                    self.tr("Setting Plex stars {index} of {total}…").format(
                        index=index, total=len(plan.to_plex)
                    )
                )
                try:
                    self.plex.rate(server, change.track.rating_key, change.rating)
                except CancelledError:
                    raise
                except ConnectedError:
                    failed += 1
            return StarsSent(len(plan.to_plex) - failed, failed, len(plan.to_ipod))

        server = self._plex_server
        return self._start("plex-stars", lambda job: work(server, job))

    def plex_sections(self) -> None:
        """List the server's music, movie and TV libraries for the Plex page."""

        self.run_plex("plex-sections", lambda server, _job: self.plex.sections(server))

    def set_plex_music_videos(self, enabled: bool) -> None:
        """Include the music videos Plex attached to artists and songs."""

        self.update_config(
            replace(
                self._config,
                plex=replace(self._config.plex, music_videos=enabled),
            )
        )

    def set_plex_video_use(self, key: str, use: PlexVideoUse | None) -> None:
        """Use a movie or TV library for movies, TV shows or music videos, or not."""

        kept = [v for v in self._config.plex.video_sections if v.key != key]
        if use is not None:
            kept.append(PlexVideoSection(key, use))
        self.update_config(
            replace(
                self._config,
                plex=replace(
                    self._config.plex,
                    video_sections=tuple(sorted(kept, key=lambda v: v.key)),
                ),
            )
        )

    def set_plex_section_included(self, key: str, included: bool) -> None:
        skipped = [k for k in self._config.plex.skipped_sections if k != key]
        if not included:
            skipped.append(key)
        self.update_config(
            replace(
                self._config,
                plex=replace(
                    self._config.plex, skipped_sections=tuple(sorted(skipped))
                ),
            )
        )

    def plex_ratings_plan(self, tracks: tuple[Track, ...]) -> None:
        """Compare the Library Workspace's Tracks with Plex ratings."""

        if self._devices.active_ipod is None:
            self.message.emit(self.tr("Connect an iPod to sync star ratings."))
            return

        def work(server: PlexServer, job: _Job) -> RatingPlan:
            job.signals.progress.emit(self.tr("Reading your Plex music library…"))
            return plan_ratings(
                tracks,
                self.plex.library_tracks(
                    server, job.cancelled, skip=self._config.plex.skipped_sections
                ),
            )

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

    def refresh(self, sources: tuple[str, ...], tracks: tuple[Track, ...] = ()) -> bool:
        """Download Selections; with ``tracks``, first send iPod audiobook progress."""

        config = self._config
        books = ipod_books(tracks) if config.grimmory.sync_progress else ()

        def work(job: _Job) -> tuple[RefreshOutcome, ...]:
            return tuple(
                self._refresh_source(source, config, job, books) for source in sources
            )

        def then(value: object) -> None:
            outcomes = (
                cast("tuple[object, ...]", value) if isinstance(value, tuple) else ()
            )
            for outcome in outcomes:
                if isinstance(outcome, RefreshOutcome) and outcome.marks:
                    self._save_marks(outcome.marks)

        return self._start("refresh", work, then=then)

    def refresh_before_sync(self) -> bool:
        """Update Immich picks; Plex and Grimmory are listed by prepare_for_sync."""

        return bool(self._config.immich.picks) and self.refresh(
            (ConnectedSource.IMMICH,)
        )

    def _refresh_source(
        self,
        source: str,
        config: ConnectedConfig,
        job: _Job,
        books: tuple[IPodBook, ...] = (),
    ) -> RefreshOutcome:
        if source != ConnectedSource.IMMICH:
            raise ConnectedError(
                "Plex and Grimmory are no longer downloaded ahead of Sync; they appear in Sync's media list."
            )
        emit = job.signals.progress.emit
        folder = config.root() / _FOLDER_TYPES[source][0]
        key = self.secret(SecretKey.IMMICH).get("api_key", "")
        immich = ImmichClient(config.immich.url, key)
        items: tuple[Desired, ...] = plan_immich(
            immich, config.immich, cancelled=job.cancelled, progress=emit
        )

        def report(name: str, index: int, total: int) -> None:
            if name:
                emit(f"Immich: {index + 1} of {total} · {name.rsplit('/', 1)[-1]}")

        outcome = refresh_folder(
            folder, items, cancelled=job.cancelled, progress=report
        )
        return RefreshOutcome(source, outcome)

    # --- streaming Plex and Grimmory into Sync ---------------------------------------

    @property
    def remote_media(self) -> ConnectedRemote | None:
        """The downloader for the last listed catalog, for Sync Execution."""

        return self._remote

    @property
    def streams(self) -> bool:
        """Whether Plex or Grimmory is set up to appear in Sync."""

        return bool(self._config.plex.server_id) or bool(self._config.grimmory.username)

    def _make_remote(self) -> ConnectedRemote | None:
        server = self._plex_server if self.plex_restore() else None
        grimmory = self.grimmory() if self._config.grimmory.username else None
        if server is None and grimmory is None:
            return None
        return ConnectedRemote(
            self._config.root(),
            self._store.directory / "connected-catalog.json",
            plex=self.plex if server is not None else None,
            server=server,
            mappings=self._config.plex.mappings,
            plex_skip=self._config.plex.skipped_sections,
            plex_videos={
                video.key: video.use.value for video in self._config.plex.video_sections
            },
            plex_music_videos=self._config.plex.music_videos,
            grimmory=grimmory,
        )

    def prepare_for_sync(self, *, immich: bool, tracks: tuple[Track, ...] = ()) -> bool:
        """Before a Host scan: update Immich picks, send Grimmory progress, list remotes.

        Returns False when there is nothing to do, so Sync can start at once.
        """

        self.unregister_streamed()
        config = self._config
        remote = self._make_remote()
        immich = immich and bool(config.immich.picks)
        if remote is None and not immich:
            self._remote = None
            return False
        books = (
            ipod_books(tracks)
            if config.grimmory.sync_progress and config.grimmory.username
            else ()
        )

        def work(job: _Job) -> SyncPreparation:
            outcomes: list[RefreshOutcome] = []
            if immich:
                outcomes.append(
                    self._refresh_source(ConnectedSource.IMMICH, config, job)
                )
            catalog = RemoteCatalog()
            sent = 0
            marks: tuple[GrimmoryMark, ...] = ()
            notes: tuple[str, ...] = ()
            if remote is not None:
                if books:
                    sent, marks, notes = self._push_progress(
                        self.grimmory(), books, config, job
                    )
                catalog = remote.catalog(job.cancelled, job.signals.progress.emit)
            return SyncPreparation(tuple(outcomes), catalog, sent, marks, notes)

        def then(value: object) -> None:
            if isinstance(value, SyncPreparation):
                self._remote = remote
                if value.marks:
                    self._save_marks(value.marks)

        return self._start("prepare-sync", work, then=then)

    def _push_progress(
        self,
        client: GrimmoryClient,
        books: tuple[IPodBook, ...],
        config: ConnectedConfig,
        job: _Job,
    ) -> tuple[int, tuple[GrimmoryMark, ...], tuple[str, ...]]:
        job.signals.progress.emit("Grimmory: sending listening progress…")
        try:
            exchange = plan_progress(
                books,
                self._remote_progress(client, books, job),
                {mark.book_id: mark for mark in config.grimmory.marks},
                pull=False,
            )
        except CancelledError:
            raise
        except ConnectedError as error:
            return 0, (), (f"Grimmory progress: {error}",)
        result = self._send_progress(client, exchange, job, pulled=False)
        return (
            result.sent,
            self._kept_marks(exchange, result, pulled=False),
            result.failed,
        )

    def trim_cache(self) -> bool:
        """After Sync: keep downloads within the cache limit, or remove them."""

        remote = self._remote
        if remote is None:
            return False
        keep = self._config.cache_media
        limit = max(0, self._config.cache_limit_gb) * 1024**3

        def work(_job: _Job) -> tuple[int, int]:
            return remote.trim(keep=keep, limit_bytes=limit)

        return self._start("trim-cache", work)

    def set_cache(self, *, enabled: bool, limit_gb: int) -> None:
        self.update_config(
            replace(self._config, cache_media=enabled, cache_limit_gb=max(1, limit_gb))
        )

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

    def set_immich_pick(self, pick: ImmichPick, included: bool) -> None:
        """Tick or untick one album, person or Favorites, keeping the other picks."""

        picks = [
            p
            for p in self._config.immich.picks
            if (p.kind, p.id) != (pick.kind, pick.id)
        ]
        if included:
            picks.append(pick)
        self.set_immich_picks(tuple(picks))

    def list_immich(self) -> bool:
        """Sync's Photos checklist: albums and people on the server (``sync-immich``)."""

        client = self.immich()

        def work(_job: _Job) -> tuple[tuple[ImmichCollection, ...], ...]:
            return (client.albums(), client.people())

        return self._start("sync-immich", work)

    # --- Grimmory -----------------------------------------------------------------------

    def grimmory(self) -> GrimmoryClient:
        """A client using the saved sign-in; new tokens are written back to the vault."""

        secret = self.secret(SecretKey.GRIMMORY)
        tokens = (
            GrimmoryTokens(secret["access"], secret["refresh"])
            if secret.get("access") and secret.get("refresh")
            else None
        )
        return GrimmoryClient(
            self._config.grimmory.url, tokens, on_tokens=self._save_grimmory_tokens
        )

    def _save_grimmory_tokens(self, tokens: GrimmoryTokens) -> None:
        self.save_secret(
            SecretKey.GRIMMORY, {"access": tokens.access, "refresh": tokens.refresh}
        )

    def grimmory_connect(self, url: str, username: str, password: str) -> None:
        """Sign in with a password (never stored), or reuse the saved sign-in."""

        url, username = url.strip(), username.strip()
        same_server = url == self._config.grimmory.url
        self.update_config(
            replace(self._config, grimmory=replace(self._config.grimmory, url=url))
        )
        self.unregister_streamed()
        client = (
            self.grimmory()
            if same_server and not password
            else GrimmoryClient(url, on_tokens=self._save_grimmory_tokens)
        )

        def work(_job: _Job) -> object:
            if password:
                client.sign_in(username, password)
            elif client.tokens is None:
                raise ConnectedError("Enter your Grimmory password to sign in.", 401)
            return client.collections()

        def then(_value: object) -> None:
            if password:
                self.update_config(
                    replace(
                        self._config,
                        grimmory=replace(self._config.grimmory, username=username),
                    )
                )

        self._start("grimmory-connect", work, then=then)

    def grimmory_sso(self, url: str) -> None:
        """Sign in through Grimmory's single sign-on in the browser."""

        url = url.strip()
        self.update_config(
            replace(self._config, grimmory=replace(self._config.grimmory, url=url))
        )
        self.unregister_streamed()
        client = GrimmoryClient(url, on_tokens=self._save_grimmory_tokens)

        def work(job: _Job) -> object:
            sso = grimmory_sso.provider(client)
            if sso is None:
                raise ConnectedError(
                    "This Grimmory doesn't have single sign-on turned on. Sign in with your username and password."
                )
            job.signals.progress.emit(
                self.tr("Finish signing in with {provider} in your browser…").format(
                    provider=sso.name
                )
            )
            grimmory_sso.sign_in(
                client, sso, self._sso_inbox, job.cancelled, job.signals.link.emit
            )
            return (client.username(), *client.collections())

        def then(value: object) -> None:
            username = cast("tuple[str, object, object]", value)[0]
            self.update_config(
                replace(
                    self._config,
                    grimmory=replace(self._config.grimmory, username=username or "SSO"),
                )
            )

        self._start("grimmory-sso", work, then=then)

    def receive_link(self, link: str) -> bool:
        """An ``iopenpod://`` link from the browser; True when a sign-in took it."""

        accepted = self._sso_inbox.offer(link)
        if not accepted:
            self.message.emit(
                self.tr(
                    "A sign-in link arrived, but no Grimmory sign-in was waiting for it. Choose Sign In with SSO again."
                )
            )
        return accepted

    def set_grimmory_picks(self, picks: tuple[GrimmoryPick, ...]) -> None:
        self.update_config(
            replace(self._config, grimmory=replace(self._config.grimmory, picks=picks))
        )

    def set_grimmory_sync_progress(self, enabled: bool) -> None:
        self.update_config(
            replace(
                self._config,
                grimmory=replace(self._config.grimmory, sync_progress=enabled),
            )
        )

    def grimmory_books(
        self, collection: GrimmoryCollection | None = None, search: str = ""
    ) -> None:
        client = self.grimmory()
        self._start(
            "grimmory-books",
            lambda job: client.audiobooks(collection, search, job.cancelled),
        )

    @staticmethod
    def _remote_progress(
        client: GrimmoryClient, books: tuple[IPodBook, ...], job: _Job
    ) -> dict[str, GrimmoryProgress | None]:
        remote: dict[str, GrimmoryProgress | None] = {}
        for book in books:
            if job.cancelled.is_set():
                raise CancelledError
            try:
                remote[book.book_id] = client.progress(book.book_id)
            except ConnectedError as error:
                if error.status != 404:
                    raise
                remote[book.book_id] = None
        return remote

    @staticmethod
    def _send_progress(
        client: GrimmoryClient, plan: ProgressPlan, job: _Job, *, pulled: bool
    ) -> ProgressSent:
        sent = 0
        failed: list[str] = []
        books: set[str] = set()
        for index, push in enumerate(plan.to_grimmory, start=1):
            if job.cancelled.is_set():
                raise CancelledError
            job.signals.progress.emit(
                f"Grimmory: sending progress {index} of {len(plan.to_grimmory)}…"
            )
            try:
                client.save_progress(
                    push.link,
                    push.position_ms,
                    push.track_index,
                    push.track_position_ms,
                )
            except CancelledError:
                raise
            except ConnectedError as error:
                failed.append(f"{push.title}: {error}")
                books.add(push.link.book_id)
                continue
            sent += 1
        return ProgressSent(
            sent, tuple(failed), len(plan.to_ipod) if pulled else 0, frozenset(books)
        )

    @staticmethod
    def _kept_marks(
        plan: ProgressPlan, result: ProgressSent, *, pulled: bool
    ) -> tuple[GrimmoryMark, ...]:
        failed = result.failed_books
        skipped: set[str] = set() if pulled else {pull.book_id for pull in plan.to_ipod}
        return tuple(
            mark
            for mark in plan.marks
            if mark.book_id not in failed and mark.book_id not in skipped
        )

    def _save_marks(self, marks: tuple[GrimmoryMark, ...]) -> None:
        merged = {mark.book_id: mark for mark in self._config.grimmory.marks}
        merged.update((mark.book_id, mark) for mark in marks)
        self.update_config(
            replace(
                self._config,
                grimmory=replace(
                    self._config.grimmory,
                    marks=tuple(merged[k] for k in sorted(merged)),
                ),
            )
        )

    def grimmory_progress_plan(self, tracks: tuple[Track, ...]) -> None:
        """Compare iPod audiobook bookmarks with Grimmory's listening progress."""

        books = ipod_books(tracks)
        if not books:
            self.message.emit(
                self.tr(
                    "No audiobooks downloaded from Grimmory are on this iPod yet. Sync them first."
                )
            )
            return
        marks = {mark.book_id: mark for mark in self._config.grimmory.marks}
        client = self.grimmory()

        def work(job: _Job) -> ProgressPlan:
            job.signals.progress.emit(
                self.tr("Reading listening progress from Grimmory…")
            )
            return plan_progress(
                books, self._remote_progress(client, books, job), marks
            )

        self._start("grimmory-progress-plan", work)

    def grimmory_send_progress(self, plan: ProgressPlan, *, pulled: bool) -> None:
        """Send iPod positions; ``pulled`` says the iPod changes were applied."""

        client = self.grimmory()

        def work(job: _Job) -> ProgressSent:
            return self._send_progress(client, plan, job, pulled=pulled)

        def then(value: object) -> None:
            if isinstance(value, ProgressSent):
                self._save_marks(self._kept_marks(plan, value, pulled=pulled))

        self._start("grimmory-progress-send", work, then=then)

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

    def list_memos(self) -> bool:
        """Sync's Notes checklist: every memo that could be sent (``sync-memos``)."""

        config = self._config.memos

        def work(job: _Job) -> tuple[Memo, ...]:
            client = self.memos()
            memos = client.memos(
                include_archived=config.include_archived, cancelled=job.cancelled
            )
            return (
                only_mine(memos, client.current_user()) if config.only_mine else memos
            )

        return self._start("sync-memos", work)

    def set_memo_tag(self, tag: str, included: bool) -> None:
        kept = [t for t in self._config.memos.tags if t.casefold() != tag.casefold()]
        if included:
            kept.append(tag)
        self.update_config(
            replace(self._config, memos=replace(self._config.memos, tags=tuple(kept)))
        )

    def set_memos_included(self, names: Iterable[str], included: bool) -> None:
        chosen = set(names)
        skipped = [n for n in self._config.memos.skipped_memos if n not in chosen]
        if not included:
            skipped.extend(sorted(chosen))
        self.update_config(
            replace(
                self._config,
                memos=replace(self._config.memos, skipped_memos=tuple(skipped)),
            )
        )

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

    # --- Contacts --------------------------------------------------------------------

    def contacts_directory(self) -> Path:
        return self._store.directory / "connected-contacts"

    def add_contacts_file(self, source: Path) -> None:
        from iOpenPod.app.connected.contacts import MAX_CONTACTS_BYTES, check_contacts

        try:
            data = source.read_bytes()
        except OSError as error:
            self.message.emit(str(error))
            return
        if len(data) > MAX_CONTACTS_BYTES:
            self.message.emit(self.tr("That contacts file is larger than 20 MB."))
            return
        text = data.decode("utf-8-sig", errors="replace")
        try:
            count = check_contacts(text)
        except ConnectedError as error:
            self.message.emit(str(error))
            return
        directory = self.contacts_directory()
        directory.mkdir(parents=True, exist_ok=True)
        stem = safe_component(source.stem, "Contacts", limit=60)
        filename, counter = f"{stem}.vcf", 2
        existing = {item.filename.casefold() for item in self._config.contacts.files}
        while filename.casefold() in existing:
            filename, counter = f"{stem} ({counter}).vcf", counter + 1
        (directory / filename).write_text(text, encoding="utf-8")
        files = (
            *self._config.contacts.files,
            ContactsFile(source.stem or "Contacts", filename),
        )
        self.update_config(
            replace(self._config, contacts=replace(self._config.contacts, files=files))
        )
        self.message.emit(
            self.tr("Added {count} contacts from {name}.").format(
                count=count, name=source.name
            )
        )

    def remove_contacts_file(self, filename: str) -> None:
        (self.contacts_directory() / filename).unlink(missing_ok=True)
        files = tuple(
            item for item in self._config.contacts.files if item.filename != filename
        )
        self.update_config(
            replace(self._config, contacts=replace(self._config.contacts, files=files))
        )

    def contact_files(self) -> list[tuple[ContactsFile, str]]:
        """Each uploaded contacts file with its vCard text (unreadable ones left out)."""

        found: list[tuple[ContactsFile, str]] = []
        for item in self._config.contacts.files:
            try:
                text = (self.contacts_directory() / item.filename).read_text(
                    encoding="utf-8"
                )
            except OSError:
                continue
            found.append((item, text))
        return found

    def contact_uploads(self) -> list[tuple[str, str]]:
        """``(file name shown, vCard text)`` for every uploaded contacts file."""

        return [(item.name, text) for item, text in self.contact_files()]

    def set_contacts_included(self, keys: Iterable[str], included: bool) -> None:
        chosen = set(keys)
        skipped = [k for k in self._config.contacts.skipped if k not in chosen]
        if not included:
            skipped.extend(chosen)
        self.update_config(
            replace(
                self._config,
                contacts=replace(self._config.contacts, skipped=tuple(sorted(skipped))),
            )
        )

    def set_contact_included(self, key: str, included: bool) -> None:
        self.set_contacts_included((key,), included)

    # --- Calendars (files) ---------------------------------------------------------

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

    def calendar_choices(self) -> list[tuple[str, str]]:
        """``(key, name)`` for each calendar file and link, for Sync's checklist."""

        choices = [
            (f"file:{item.filename}", item.name)
            for item in self._config.calendars.files
        ]
        for index, (link, name) in enumerate(self.calendar_links(), start=1):
            digest = hashlib.sha256(link.encode()).hexdigest()[:16]
            choices.append(
                (
                    f"link:{digest}",
                    name or calendar_files.redact(link) or f"Calendar {index}",
                )
            )
        return choices

    def set_calendar_included(self, key: str, included: bool) -> None:
        skipped = [k for k in self._config.calendars.skipped if k != key]
        if not included:
            skipped.append(key)
        self.update_config(
            replace(
                self._config,
                calendars=replace(
                    self._config.calendars, skipped=tuple(sorted(skipped))
                ),
            )
        )

    def _calendar_files(
        self, job: _Job, config: CalendarsConfig, links: list[tuple[str, str]]
    ) -> tuple[tuple[ExtrasFile, ...], int, list[str]]:
        """The chosen calendars as iPod files, the events left out, and problems."""

        directory = self.calendar_directory()
        skipped = set(config.skipped)
        sources: list[tuple[str, str]] = []
        issues: list[str] = []
        for item in config.files:
            if f"file:{item.filename}" in skipped:
                continue
            try:
                sources.append(
                    (item.name, (directory / item.filename).read_text(encoding="utf-8"))
                )
            except OSError as error:
                issues.append(f"{item.name}: {error}")
        chosen = [
            (link, name)
            for link, name in links
            if f"link:{hashlib.sha256(link.encode()).hexdigest()[:16]}" not in skipped
        ]
        for index, (link, name) in enumerate(chosen, start=1):
            job.signals.progress.emit(
                self.tr("Downloading calendar {index} of {total}…").format(
                    index=index, total=len(chosen)
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
        return tuple(files), dropped, issues

    # --- Extras as part of Sync with Host ---------------------------------------------

    @property
    def has_extras(self) -> bool:
        """Whether notes, calendars or contacts are set up to go on the iPod."""

        config = self._config
        return (
            bool(config.memos.url and config.memos.tags)
            or bool(config.calendars.files)
            or bool(self.calendar_links())
            or bool(config.contacts.files)
        )

    def publish_for_sync(self) -> bool:
        """After Sync: write the chosen notes, calendars and contacts to the iPod.

        Each kind replaces only the files iOpenPod put there before; a kind with
        nothing set up is left alone, and one failing doesn't stop the others.
        """

        active = self._devices.active_ipod
        if active is None:
            return False
        config = self._config
        links = self.calendar_links()
        uploads = [text for _name, text in self.contact_uploads()]
        notes = bool(config.memos.url and config.memos.tags)
        calendars = bool(config.calendars.files or links)
        contacts = bool(config.contacts.files)
        if not (notes or calendars or contacts):
            return False

        def work(job: _Job) -> ExtrasSent:
            sent = ExtrasSent()
            if notes:
                job.signals.progress.emit(self.tr("Reading memos…"))
                try:
                    client = self.memos()
                    memos = client.memos(
                        include_archived=config.memos.include_archived,
                        cancelled=job.cancelled,
                    )
                    if config.memos.only_mine:
                        memos = only_mine(memos, client.current_user())
                    others = self._coordinator.count_foreign_extras(
                        active, ExtrasSection.NOTES
                    )
                    plan = plan_notes(memos, config.memos, other_notes=others)
                    job.signals.progress.emit(self.tr("Writing notes to the iPod…"))
                    result = self._coordinator.publish_extras(
                        active,
                        ExtrasSection.NOTES,
                        tuple(ExtrasFile(i.relative, i.data) for i in plan.files),
                        job.cancelled,
                    )
                    sent = replace(
                        sent, notes=plan.notes, issues=(*sent.issues, *result.issues)
                    )
                except CancelledError:
                    raise
                except Exception as error:
                    _LOGGER.warning("Notes were not sent", exc_info=True)
                    sent = replace(sent, issues=(*sent.issues, f"Notes: {error}"))
            if calendars:
                try:
                    files, _dropped, issues = self._calendar_files(
                        job, config.calendars, links
                    )
                    job.signals.progress.emit(self.tr("Writing calendars to the iPod…"))
                    result = self._coordinator.publish_extras(
                        active, ExtrasSection.CALENDARS, files, job.cancelled
                    )
                    sent = replace(
                        sent,
                        calendars=len(files),
                        issues=(*sent.issues, *issues, *result.issues),
                    )
                except CancelledError:
                    raise
                except Exception as error:
                    # One kind failing must not stop the others.
                    _LOGGER.warning("Calendars were not sent", exc_info=True)
                    sent = replace(sent, issues=(*sent.issues, f"Calendars: {error}"))
            if contacts:
                from iOpenPod.app.connected.contacts import plan_contacts

                try:
                    planned, over = plan_contacts(uploads, config.contacts.skipped)
                    job.signals.progress.emit(self.tr("Writing contacts to the iPod…"))
                    result = self._coordinator.publish_extras(
                        active,
                        ExtrasSection.CONTACTS,
                        tuple(ExtrasFile(c.relative, c.data) for c in planned),
                        job.cancelled,
                    )
                    extra = (
                        (self.tr("{count} contacts didn't fit.").format(count=over),)
                        if over
                        else ()
                    )
                    sent = replace(
                        sent,
                        contacts=len(planned),
                        issues=(*sent.issues, *extra, *result.issues),
                    )
                except CancelledError:
                    raise
                except Exception as error:
                    _LOGGER.warning("Contacts were not sent", exc_info=True)
                    sent = replace(sent, issues=(*sent.issues, f"Contacts: {error}"))
            return sent

        return self._start("extras-publish", work, exclusive=True)


__all__ = [
    "ConnectedController",
    "ConnectedSource",
    "ExtrasSent",
    "RefreshOutcome",
]
