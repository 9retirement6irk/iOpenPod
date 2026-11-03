"""Connected Source pages: Plex, Immich, Grimmory audiobooks, Memos notes, calendars."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QCoreApplication, QSize, Qt, QUrl
from PySide6.QtGui import QIcon, QPixmap, QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from iOpenPod.app.connected.config import (
    ImmichPick,
    ImmichPickKind,
    ImmichSize,
    PathMapping,
    TagMatch,
)
from iOpenPod.app.connected.grimmory_sso import CALLBACK
from iOpenPod.app.connected.secrets import SecretKey
from iOpenPod.app.connected_controller import (
    ConnectedSource,
    ProgressSent,
    RefreshOutcome,
)
from iOpenPod.app.scrobbling.models import Service
from iOpenPod.GUI.presentation.desktop_services import open_desktop_url
from iOpenPod.GUI.presentation.theme.tokens import LAYOUT
from iOpenPod.GUI.widgets.browser_chrome import PageHeader
from iOpenPod.GUI.widgets.themed_buttons import ActionButton, ActionButtonKind
from iPodDB.library import TrackFieldEdit

if TYPE_CHECKING:
    from collections.abc import Callable

    from iOpenPod.app.connected.grimmory import (
        GrimmoryCollection,
    )
    from iOpenPod.app.connected.grimmory_progress import ProgressPlan
    from iOpenPod.app.connected.immich import (
        ImmichClient,
        ImmichCollection,
        ImmichPhoto,
    )
    from iOpenPod.app.connected.memos import MemosSummary, NotesPlan
    from iOpenPod.app.connected.plex import (
        PlexClient,
        PlexServer,
        PlexServerChoice,
    )
    from iOpenPod.app.connected.plex_ratings import RatingPlan
    from iOpenPod.app.connected_controller import ConnectedController
    from iOpenPod.app.library_workspace import LibraryWorkspace
    from iOpenPod.app.scrobbling.controller import ScrobbleController
    from iOpenPod.app.services.ipod_extras import ExtrasResult

_ROLE = Qt.ItemDataRole.UserRole


def _button(
    text: str, parent: QWidget, kind: ActionButtonKind = ActionButtonKind.SECONDARY
) -> ActionButton:
    button = ActionButton(parent=parent, kind=kind)
    button.setText(text)
    return button


def _row(*widgets: QWidget, stretch_first: bool = False) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(LAYOUT.space_xs)
    for index, widget in enumerate(widgets):
        layout.addWidget(widget, 1 if stretch_first and index == 0 else 0)
    if not stretch_first:
        layout.addStretch(1)
    return layout


def _description(text: str, parent: QWidget) -> QLabel:
    label = QLabel(text, parent)
    label.setWordWrap(True)
    label.setObjectName("pageDescription")
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class _ConnectedPage(QWidget):
    """Shared chrome: header, one status line with Cancel, scrolling body."""

    def __init__(
        self, controller: ConnectedController, title: str, parent: QWidget | None
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.controller = controller
        self._header = PageHeader(self)
        self._header.set_title(title)
        self._status = QLabel(self)
        self._status.setWordWrap(True)
        self._status.setObjectName("pageDescription")
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._cancel = _button(
            QCoreApplication.translate("CommonActions", "Cancel"),
            self,
            ActionButtonKind.QUIET,
        )
        self._cancel.clicked.connect(controller.cancel)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(
            LAYOUT.space_lg, LAYOUT.space_xs, LAYOUT.space_lg, LAYOUT.space_xs
        )
        status_row.addWidget(self._status, 1)
        status_row.addWidget(self._cancel)
        body = QWidget(self)
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(
            LAYOUT.space_lg, LAYOUT.space_sm, LAYOUT.space_lg, LAYOUT.space_lg
        )
        self.body.setSpacing(LAYOUT.space_md)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(body)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._header)
        layout.addLayout(status_row)
        layout.addWidget(scroll, 1)
        controller.changed.connect(self._sync_busy)
        controller.progress.connect(self._show_progress)
        controller.message.connect(self._show_message)
        controller.result.connect(self._on_result)
        self._operations: set[str] = set()
        self._sync_busy()

    def header(self) -> PageHeader:
        return self._header

    def start(self, operation: str) -> None:
        self._operations.add(operation)

    def _owns(self, operation: str) -> bool:
        if operation in self._operations:
            self._operations.discard(operation)
            return True
        return False

    def _sync_busy(self) -> None:
        busy = self.controller.busy and bool(self._operations)
        self._cancel.setVisible(busy)
        self.refresh()

    def _show_progress(self, text: str) -> None:
        if self._operations:
            self._status.setText(text)

    def _show_message(self, text: str) -> None:
        if self._operations or self.isVisible():
            self._status.setText(text)
            self._operations.clear()

    def say(self, text: str) -> None:
        self._status.setText(text)

    def _on_result(self, operation: str, value: object) -> None:
        if self._owns(operation):
            self.handle(operation, value)

    def handle(self, operation: str, value: object) -> None:
        raise NotImplementedError

    def refresh(self) -> None:
        """Update enabled states from controller state."""

    def refresh_outcome(self, outcomes: object) -> None:
        lines: list[str] = []
        for outcome in cast("tuple[RefreshOutcome, ...]", outcomes):
            line = self.tr("{source}: {summary}.").format(
                source=outcome.source.capitalize(), summary=outcome.report.summary
            )
            lines.append(line)
            if outcome.progress_sent:
                lines.append(
                    self.tr("Sent listening progress for {count} audiobooks.").format(
                        count=outcome.progress_sent
                    )
                )
            lines.extend(outcome.notes[:5])
            lines.extend(outcome.report.failed[:5])
        lines.append(self.tr("Run Sync with Host to send them to the iPod."))
        self.say("\n".join(lines))

    def open_folder(self, source: str) -> None:
        path = self.controller.folder(source)
        path.mkdir(parents=True, exist_ok=True)
        open_desktop_url(QUrl.fromLocalFile(str(path)))


def _plex_art(
    client: PlexClient, server: PlexServer, thumb: str
) -> Callable[[], bytes]:
    return lambda: client.thumbnail(server, thumb)


def _thumbnail(client: ImmichClient, asset: str) -> Callable[[], bytes]:
    return lambda: client.thumbnail(asset)


def _clock(ms: int) -> str:
    seconds = max(0, ms) // 1000
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}"


def _checkable(
    text: str, data: object, checked: bool, detail: str = ""
) -> QListWidgetItem:
    item = QListWidgetItem(text if not detail else f"{text}  ·  {detail}")
    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
    item.setData(_ROLE, data)
    return item


# --- Plex ------------------------------------------------------------------------------


class PlexPage(_ConnectedPage):
    def __init__(
        self,
        controller: ConnectedController,
        scrobbling: ScrobbleController,
        workspace: LibraryWorkspace,
        parent: QWidget | None = None,
    ) -> None:
        self._scrobbling = scrobbling
        self._workspace = workspace
        super().__init__(controller, "Plex", parent)
        self.setObjectName("plexPage")
        self._header.set_title(self.tr("Plex"))

        account = QGroupBox(self.tr("Account and server"), self)
        self._account = QLabel(account)
        self._sign_in = _button(
            self.tr("Sign in with Plex"), account, ActionButtonKind.PRIMARY
        )
        self._sign_in.clicked.connect(self._start_sign_in)
        self._servers = QComboBox(account)
        self._use_server = _button(self.tr("Use this server"), account)
        self._use_server.clicked.connect(self._choose_server)
        self._manual = _button(
            self.tr("Server address…"), account, ActionButtonKind.QUIET
        )
        self._manual.clicked.connect(self._manual_server)
        self._sign_out = _button(self.tr("Sign out"), account, ActionButtonKind.QUIET)
        self._sign_out.clicked.connect(self._forget)
        account_layout = QVBoxLayout(account)
        account_layout.addLayout(_row(self._account, self._sign_in, self._sign_out))
        account_layout.addLayout(_row(self._servers, self._use_server, self._manual))

        plays = QGroupBox(self.tr("Plays and ratings"), self)
        self._ratings = _button(self.tr("Sync star ratings now…"), plays)
        self._ratings.clicked.connect(self._plan_ratings)
        plays_layout = QVBoxLayout(plays)
        plays_layout.addWidget(
            _description(
                self.tr(
                    "Scrobble now and Sync send iPod plays to Plex, reported from a player named after your iPod so they appear in Plex Play History. Scrobble now also sets Plex stars from the iPod's. Turn this off in Settings → Sync → Scrobbling."
                ),
                plays,
            )
        )
        plays_layout.addWidget(
            _description(
                self.tr(
                    "Star ratings: songs rated on the iPod set Plex's stars (the iPod wins when they differ); songs rated only in Plex get those stars on the iPod. Whole stars are compared, so Plex half-stars are kept."
                ),
                plays,
            )
        )
        plays_layout.addLayout(_row(self._ratings))

        mappings = QGroupBox(self.tr("Shared folders (optional)"), self)
        self._mappings = QTableWidget(0, 2, mappings)
        self._mappings.setHorizontalHeaderLabels(
            [
                self.tr("Path on the Plex server"),
                self.tr("Same folder on this computer"),
            ]
        )
        self._mappings.horizontalHeader().setStretchLastSection(True)
        self._mappings.setMaximumHeight(120)
        self._mappings.itemChanged.connect(self._save_mappings)
        add_mapping = _button(self.tr("Add"), mappings, ActionButtonKind.QUIET)
        add_mapping.clicked.connect(self._add_mapping)
        remove_mapping = _button(self.tr("Remove"), mappings, ActionButtonKind.QUIET)
        remove_mapping.clicked.connect(self._remove_mapping)
        mappings_layout = QVBoxLayout(mappings)
        mappings_layout.addWidget(
            _description(
                self.tr(
                    "If your music share is mounted here, iOpenPod copies files from it instead of downloading through Plex. Example: /mnt/media/music → /Volumes/Media/music."
                ),
                mappings,
            )
        )
        mappings_layout.addWidget(self._mappings)
        mappings_layout.addLayout(_row(add_mapping, remove_mapping))

        music = QGroupBox(self.tr("Music"), self)
        open_folder = _button(
            self.tr("Show downloaded music"), music, ActionButtonKind.QUIET
        )
        open_folder.clicked.connect(lambda: self.open_folder(ConnectedSource.PLEX))
        music_layout = QVBoxLayout(music)
        music_layout.addWidget(
            _description(
                self.tr(
                    "All your Plex music and playlists appear in Sync with Host's media list, beside your computer's music. Check what goes on the iPod there; songs download only when Sync copies them. Downloads are kept within the media cache limit in Settings → Sync."
                ),
                music,
            )
        )
        music_layout.addLayout(_row(open_folder))
        self.body.addWidget(account)
        self.body.addWidget(music)
        self.body.addWidget(plays)
        self.body.addWidget(mappings)
        self._load_mappings()
        self.refresh()

    # --- state ---------------------------------------------------------------------

    def refresh(self) -> None:
        if not hasattr(self, "_ratings"):
            return
        config = self.controller.config.plex
        signed_in = bool(config.username)
        connected = self.controller.plex_server is not None or bool(config.server_id)
        busy = self.controller.busy
        self._account.setText(
            self.tr("Signed in as {name} · {server}").format(
                name=config.username,
                server=config.server_name or self.tr("no server chosen"),
            )
            if signed_in
            else self.tr("Not signed in to Plex.")
        )
        self._sign_in.setVisible(not signed_in)
        self._sign_out.setVisible(signed_in)
        choices = self.controller.plex_choices
        self._servers.setVisible(bool(choices))
        self._use_server.setVisible(bool(choices))
        self._manual.setEnabled(signed_in and not busy)
        if self._servers.count() != len(choices):
            self._servers.clear()
            for choice in choices:
                self._servers.addItem(
                    choice.name
                    + ("" if choice.owned else self.tr(" (shared with you)")),
                    choice,
                )
        for widget in (self._sign_in, self._use_server, self._ratings):
            widget.setEnabled(not busy)
        self._ratings.setEnabled(not busy and connected)

    def _start_sign_in(self) -> None:
        self.start("plex-sign-in")
        self.say(self.tr("A Plex sign-in page is opening in your browser…"))
        self.controller.plex_sign_in()

    def handle(self, operation: str, value: object) -> None:
        if operation == "plex-sign-in":
            token, username, choices = cast(
                "tuple[str, str, tuple[PlexServerChoice, ...]]", value
            )
            self.controller.plex_signed_in(token, username, choices)
            owned = [choice for choice in choices if choice.owned]
            if len(owned) == 1:
                self.say(
                    self.tr("Signed in. Connecting to {name}…").format(
                        name=owned[0].name
                    )
                )
                self.start("plex-connect")
                self.controller.plex_choose_server(owned[0])
            else:
                self.say(self.tr("Signed in. Choose a server."))
        elif operation == "plex-servers":
            self.controller.set_plex_choices(
                cast("tuple[PlexServerChoice, ...]", value)
            )
        elif operation == "plex-connect":
            server = cast("PlexServer", value)
            self.controller.plex_connected(server)
            credentials = self.controller.plex_credentials()
            if credentials is not None and self._scrobbling.plex_sync:
                # Plays and stars follow the chosen server.
                self._scrobbling.connect_plex(credentials)
            self.say(
                self.tr(
                    "Connected to {name}. Its music appears in Sync with Host."
                ).format(name=server.name)
            )
        elif operation == "plex-ratings-plan":
            self._review_ratings(cast("RatingPlan", value))
        elif operation == "plex-ratings-send":
            self.say(self.tr("Set stars on {count} Plex songs.").format(count=value))

    def _choose_server(self) -> None:
        choice = self._servers.currentData()
        if choice is not None:
            self.start("plex-connect")
            self.say(self.tr("Connecting…"))
            self.controller.plex_choose_server(cast("PlexServerChoice", choice))

    def _manual_server(self) -> None:
        address, ok = QInputDialog.getText(
            self,
            self.tr("Plex server address"),
            self.tr("Address, for example http://192.168.1.10:32400"),
        )
        if ok and address.strip():
            self.start("plex-connect")
            self.controller.plex_connect_manual(address)

    def _forget(self) -> None:
        if self._scrobbling.plex_connected:
            self._scrobbling.disconnect_service(Service.PLEX)
        self.controller.forget(SecretKey.PLEX)

    # --- path mappings ---------------------------------------------------------------

    def _load_mappings(self) -> None:
        self._mappings.blockSignals(True)
        self._mappings.setRowCount(0)
        for mapping in self.controller.config.plex.mappings:
            row = self._mappings.rowCount()
            self._mappings.insertRow(row)
            self._mappings.setItem(row, 0, QTableWidgetItem(mapping.server))
            self._mappings.setItem(row, 1, QTableWidgetItem(mapping.host))
        self._mappings.blockSignals(False)

    def _save_mappings(self) -> None:
        values: list[PathMapping] = []
        for row in range(self._mappings.rowCount()):
            server, host = self._mappings.item(row, 0), self._mappings.item(row, 1)
            if (
                server is not None
                and host is not None
                and server.text().strip()
                and host.text().strip()
            ):
                values.append(PathMapping(server.text().strip(), host.text().strip()))
        self.controller.update_config(
            replace(
                self.controller.config,
                plex=replace(self.controller.config.plex, mappings=tuple(values)),
            )
        )

    def _add_mapping(self) -> None:
        row = self._mappings.rowCount()
        self._mappings.insertRow(row)
        self._mappings.setItem(row, 0, QTableWidgetItem("/mnt/media/music"))
        self._mappings.setItem(row, 1, QTableWidgetItem(str(Path.home() / "Music")))
        self._save_mappings()

    def _remove_mapping(self) -> None:
        row = self._mappings.currentRow()
        if row >= 0:
            self._mappings.removeRow(row)
            self._save_mappings()

    # --- plays and ratings -------------------------------------------------------------

    def _plan_ratings(self) -> None:
        self.start("plex-ratings-plan")
        self.controller.plex_ratings_plan(self._workspace.tracks)

    def _review_ratings(self, plan: RatingPlan) -> None:
        if not plan.to_plex and not plan.to_ipod:
            self.say(
                self.tr(
                    "Star ratings already match. {unmatched} songs weren't found in Plex."
                ).format(unmatched=plan.unmatched)
            )
            return
        box = QMessageBox(self)
        box.setWindowTitle(self.tr("Sync star ratings?"))
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            self.tr(
                "{plex} songs get new stars in Plex and {ipod} songs get new stars on the iPod."
            ).format(plex=len(plan.to_plex), ipod=len(plan.to_ipod))
        )
        box.setInformativeText(
            self.tr(
                "iPod stars are applied as Library changes; choose Review Changes in the sidebar to save them to the iPod. {unmatched} songs weren't found in Plex and {ambiguous} matched more than one song."
            ).format(unmatched=plan.unmatched, ambiguous=plan.ambiguous)
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        if box.exec() != QMessageBox.StandardButton.Ok:
            return
        if plan.to_ipod:
            from iOpenPod.app.library_workspace import TrackUpdate

            try:
                self._workspace.apply_track_edits(
                    tuple(
                        TrackUpdate(
                            change.track_id, (TrackFieldEdit("rating", change.rating),)
                        )
                        for change in plan.to_ipod
                    ),
                    self._workspace.edit_revision,
                )
            except ValueError as error:
                self.say(str(error))
                return
        if plan.to_plex:
            self.start("plex-ratings-send")
            self.controller.plex_send_ratings(plan)
        else:
            self.say(
                self.tr(
                    "Stars are ready; choose Review Changes to save them to the iPod."
                )
            )


# --- Immich ------------------------------------------------------------------------------


class ImmichPage(_ConnectedPage):
    def __init__(
        self, controller: ConnectedController, parent: QWidget | None = None
    ) -> None:
        self._loading = False
        self._open: tuple[ImmichPickKind, str, str] | None = None
        super().__init__(controller, "Immich", parent)
        self.setObjectName("immichPage")
        self._header.set_title(self.tr("Immich"))
        self._download = _button(
            self.tr("Download for Sync"), self, ActionButtonKind.PRIMARY
        )
        self._download.clicked.connect(self._refresh_downloads)
        self._header.add_action(self._download)

        account = QGroupBox(self.tr("Server"), self)
        self._url = QLineEdit(controller.config.immich.url, account)
        self._url.setPlaceholderText("http://192.168.1.10:2283")
        self._key = QLineEdit(account)
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText(self.tr("API key"))
        self._connect = _button(self.tr("Connect"), account, ActionButtonKind.PRIMARY)
        self._connect.clicked.connect(self._connect_server)
        form = QFormLayout(account)
        form.addRow(self.tr("Address"), self._url)
        form.addRow(self.tr("API key"), self._key)
        form.addRow(
            "",
            _description(
                self.tr(
                    "Create a key in Immich → Account Settings → API Keys with album.read, asset.read, asset.view, person.read and (for Full size) asset.download."
                ),
                account,
            ),
        )
        form.addRow("", _row(self._connect))

        picker = QGroupBox(self.tr("Choose photos"), self)
        self._collections = QListWidget(picker)
        self._collections.setMaximumWidth(300)
        self._collections.itemChanged.connect(self._collection_toggled)
        self._collections.currentItemChanged.connect(self._collection_opened)
        self._grid = QListWidget(picker)
        self._grid.setViewMode(QListWidget.ViewMode.IconMode)
        self._grid.setIconSize(QSize(120, 120))
        self._grid.setGridSize(QSize(140, 160))
        self._grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._grid.setMovement(QListWidget.Movement.Static)
        self._grid.setMinimumHeight(360)
        self._grid.itemChanged.connect(self._photo_toggled)
        self._size = QComboBox(picker)
        self._size.addItem(
            self.tr("iPod size (smaller, faster)"), ImmichSize.PREVIEW.value
        )
        self._size.addItem(self.tr("Full size JPEG"), ImmichSize.FULL.value)
        self._size.setCurrentIndex(
            0 if controller.config.immich.size is ImmichSize.PREVIEW else 1
        )
        self._size.currentIndexChanged.connect(self._size_changed)
        self._summary = QLabel(picker)
        open_folder = _button(self.tr("Show downloads"), picker, ActionButtonKind.QUIET)
        open_folder.clicked.connect(lambda: self.open_folder(ConnectedSource.IMMICH))
        split = QSplitter(picker)
        split.addWidget(self._collections)
        split.addWidget(self._grid)
        split.setStretchFactor(1, 1)
        layout = QVBoxLayout(picker)
        layout.addWidget(
            _description(
                self.tr(
                    "Tick Favorites, albums or people to sync all of their photos, or open one and tick individual photos. Videos are skipped. Photos are saved as JPEG for the iPod."
                ),
                picker,
            )
        )
        layout.addWidget(split, 1)
        layout.addLayout(
            _row(
                QLabel(self.tr("Photo size"), picker),
                self._size,
                self._summary,
                open_folder,
            )
        )
        self.body.addWidget(account)
        self.body.addWidget(picker, 1)
        controller.previewLoaded.connect(self._preview)
        self.refresh()

    def refresh(self) -> None:
        if not hasattr(self, "_download"):
            return
        busy = self.controller.busy
        picks = self.controller.config.immich.picks
        self._connect.setEnabled(not busy)
        self._download.setEnabled(not busy and bool(picks))
        self._summary.setText(self.tr("{count} selected").format(count=len(picks)))

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if (
            not self._collections.count()
            and self.controller.config.immich.url
            and not self.controller.busy
            and self.controller.secret(SecretKey.IMMICH).get("api_key")
        ):
            self._connect_server(saved=True)

    def _connect_server(self, *, saved: bool = False) -> None:
        self.start("immich-connect")
        self.say(self.tr("Connecting to Immich…"))
        key = "" if saved else self._key.text()
        if not saved and not key.strip():
            key = self.controller.secret(SecretKey.IMMICH).get("api_key", "")
        self.controller.immich_connect(self._url.text(), key)
        self._key.clear()

    def handle(self, operation: str, value: object) -> None:
        if operation == "immich-connect":
            user, albums, people = cast(
                "tuple[str, tuple[ImmichCollection, ...], tuple[ImmichCollection, ...]]",
                value,
            )
            self._fill_collections(albums, people)
            self.say(
                self.tr(
                    "Connected as {user}: {albums} albums, {people} people."
                ).format(user=user, albums=len(albums), people=len(people))
            )
        elif operation == "immich-photos":
            self._fill_photos(cast("tuple[ImmichPhoto, ...]", value))
        elif operation == "refresh":
            self.refresh_outcome(value)

    def _picks(self) -> dict[tuple[str, str], ImmichPick]:
        return {
            (pick.kind.value, pick.id): pick
            for pick in self.controller.config.immich.picks
        }

    def _fill_collections(
        self, albums: tuple[ImmichCollection, ...], people: tuple[ImmichCollection, ...]
    ) -> None:
        picks = self._picks()
        self._loading = True
        self._collections.clear()
        favorites = (ImmichPickKind.FAVORITES, "favorites", self.tr("★ Favorites"))
        self._collections.addItem(
            _checkable(
                favorites[2], favorites, (favorites[0].value, favorites[1]) in picks
            )
        )
        for collection in albums:
            data = (collection.kind, collection.id, collection.title)
            self._collections.addItem(
                _checkable(
                    collection.title,
                    data,
                    (collection.kind.value, collection.id) in picks,
                    self.tr("{count} items").format(count=collection.count),
                )
            )
        for collection in people:
            data = (collection.kind, collection.id, collection.title)
            self._collections.addItem(
                _checkable(
                    collection.title,
                    data,
                    (collection.kind.value, collection.id) in picks,
                    self.tr("Person"),
                )
            )
        self._loading = False

    def _collection_toggled(self, entry: QListWidgetItem) -> None:
        if self._loading:
            return
        kind, identity, title = cast(
            "tuple[ImmichPickKind, str, str]", entry.data(_ROLE)
        )
        picks = self._picks()
        if entry.checkState() == Qt.CheckState.Checked:
            picks[(kind.value, identity)] = ImmichPick(
                kind, identity, title.lstrip("★ ")
            )
        else:
            picks.pop((kind.value, identity), None)
        self.controller.set_immich_picks(tuple(picks.values()))

    def _collection_opened(
        self, current: QListWidgetItem | None, _previous: object
    ) -> None:
        if current is None or self.controller.busy:
            return
        kind, identity, title = cast(
            "tuple[ImmichPickKind, str, str]", current.data(_ROLE)
        )
        self._open = (kind, identity, title)
        client = self.controller.immich()
        self.start("immich-photos")
        self.say(self.tr("Loading {title}…").format(title=title))
        self.controller.run(
            "immich-photos", lambda job: client.photos(kind, identity, job.cancelled)
        )

    def _fill_photos(self, photos: tuple[ImmichPhoto, ...]) -> None:
        picks = self._picks()
        client = self.controller.immich()
        self._loading = True
        self._grid.clear()
        for photo in photos[:2000]:
            item = _checkable(
                photo.filename, photo, (ImmichPickKind.ASSET.value, photo.id) in picks
            )
            item.setSizeHint(QSize(140, 160))
            self._grid.addItem(item)
            self.controller.preview(f"immich:{photo.id}", _thumbnail(client, photo.id))
        self._loading = False
        extra = len(photos) - 2000
        self.say(
            self.tr("{count} photos").format(count=len(photos))
            + (
                self.tr(" (showing the first 2,000; tick the collection to sync all)")
                if extra > 0
                else ""
            )
        )

    def _preview(self, key: str, data: bytes) -> None:
        if not key.startswith("immich:") or not data:
            return
        asset = key.removeprefix("immich:")
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        for row in range(self._grid.count()):
            item = self._grid.item(row)
            if cast("ImmichPhoto", item.data(_ROLE)).id == asset:
                item.setIcon(QIcon(pixmap))
                break

    def _photo_toggled(self, entry: QListWidgetItem) -> None:
        if self._loading:
            return
        photo = cast("ImmichPhoto", entry.data(_ROLE))
        picks = self._picks()
        key = (ImmichPickKind.ASSET.value, photo.id)
        if entry.checkState() == Qt.CheckState.Checked:
            picks[key] = ImmichPick(ImmichPickKind.ASSET, photo.id, photo.filename)
        else:
            picks.pop(key, None)
        self.controller.set_immich_picks(tuple(picks.values()))

    def _size_changed(self) -> None:
        size = ImmichSize(str(self._size.currentData()))
        self.controller.update_config(
            replace(
                self.controller.config,
                immich=replace(self.controller.config.immich, size=size),
            )
        )

    def _refresh_downloads(self) -> None:
        self.start("refresh")
        self.controller.register_folder(ConnectedSource.IMMICH)
        self.controller.refresh((ConnectedSource.IMMICH,))


# --- Grimmory audiobooks --------------------------------------------------------------------


class GrimmoryPage(_ConnectedPage):
    def __init__(
        self,
        controller: ConnectedController,
        workspace: LibraryWorkspace,
        parent: QWidget | None = None,
    ) -> None:
        self._loading = False
        self._workspace = workspace
        super().__init__(controller, "Grimmory", parent)
        self.setObjectName("grimmoryPage")
        self._header.set_title(self.tr("Grimmory Audiobooks"))
        account = QGroupBox(self.tr("Server"), self)
        self._url = QLineEdit(controller.config.grimmory.url, account)
        self._url.setPlaceholderText("http://192.168.1.10:6060")
        self._user = QLineEdit(controller.config.grimmory.username, account)
        self._password = QLineEdit(account)
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.returnPressed.connect(self._connect_server)
        self._connect = _button(self.tr("Sign In"), account, ActionButtonKind.PRIMARY)
        self._connect.clicked.connect(self._connect_server)
        self._sso = _button(self.tr("Sign In with SSO"), account)
        self._sso.clicked.connect(self._sign_in_sso)
        self._sign_out = _button(self.tr("Sign Out"), account, ActionButtonKind.QUIET)
        self._sign_out.clicked.connect(self._forget)
        self._account = QLabel(account)
        self._account.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form = QFormLayout(account)
        form.addRow(self.tr("Address"), self._url)
        form.addRow(self.tr("Username"), self._user)
        form.addRow(self.tr("Password"), self._password)
        form.addRow(
            "",
            _description(
                self.tr(
                    "Sign in with your Grimmory account. iOpenPod keeps only Grimmory's sign-in token in your system keychain, never your password."
                ),
                account,
            ),
        )
        form.addRow("", _row(self._connect, self._sso, self._sign_out, self._account))
        form.addRow(
            "",
            _description(
                self.tr(
                    "Sign In with SSO opens your identity provider in the browser, the way Grimmory's mobile apps do, and the browser returns to iOpenPod through {address}. Add that address once in Grimmory (Settings → Authentication → Mobile Redirect URIs) and as an allowed redirect for Grimmory's client in your identity provider."
                ).format(address=CALLBACK),
                account,
            ),
        )

        books = QGroupBox(self.tr("Audiobooks"), self)
        open_folder = _button(
            self.tr("Show downloaded audiobooks"), books, ActionButtonKind.QUIET
        )
        open_folder.clicked.connect(lambda: self.open_folder(ConnectedSource.GRIMMORY))
        books_layout = QVBoxLayout(books)
        books_layout.addWidget(
            _description(
                self.tr(
                    "All your Grimmory audiobooks appear in Sync with Host's media list. Check what goes on the iPod there; they download only when Sync copies them, and are kept within the media cache limit in Settings → Sync."
                ),
                books,
            )
        )
        books_layout.addLayout(_row(open_folder))

        progress = QGroupBox(self.tr("Listening progress"), self)
        self._exchange = _button(self.tr("Sync Progress Now"), progress)
        self._exchange.clicked.connect(self._plan_progress)
        self._auto = QCheckBox(
            self.tr("Send iPod progress to Grimmory before each Sync"), progress
        )
        self._auto.setChecked(controller.config.grimmory.sync_progress)
        self._auto.toggled.connect(controller.set_grimmory_sync_progress)
        progress_layout = QVBoxLayout(progress)
        progress_layout.addWidget(
            _description(
                self.tr(
                    "Audiobooks downloaded here remember which Grimmory book they belong to. Sync Progress compares where you are on the iPod with Grimmory: the side you listened on most recently wins, so you can switch between the iPod and Grimmory's player."
                ),
                progress,
            )
        )
        progress_layout.addWidget(self._auto)
        progress_layout.addLayout(_row(self._exchange))
        self.body.addWidget(account)
        self.body.addWidget(books)
        self.body.addWidget(progress)
        self.refresh()

    def refresh(self) -> None:
        if not hasattr(self, "_exchange"):
            return
        busy = self.controller.busy
        config = self.controller.config.grimmory
        signed_in = bool(config.username)
        self._connect.setEnabled(not busy)
        self._sso.setEnabled(not busy)
        self._connect.setText(
            self.tr("Reconnect") if signed_in and config.url else self.tr("Sign In")
        )
        self._sign_out.setVisible(signed_in)
        self._account.setText(
            self.tr("Signed in as {name}").format(name=config.username)
            if signed_in
            else ""
        )
        self._exchange.setEnabled(not busy and signed_in)

    def _connect_server(self) -> None:
        if self.controller.busy:
            return
        self.start("grimmory-connect")
        self.say(self.tr("Connecting to Grimmory…"))
        self.controller.grimmory_connect(
            self._url.text(), self._user.text(), self._password.text()
        )
        self._password.clear()

    def _sign_in_sso(self) -> None:
        if self.controller.busy:
            return
        self.start("grimmory-sso")
        self.say(self.tr("Opening single sign-on…"))
        self.controller.grimmory_sso(self._url.text())
        self._password.clear()

    def _forget(self) -> None:
        self.controller.forget(SecretKey.GRIMMORY)
        self.say(self.tr("Signed out of Grimmory."))

    def handle(self, operation: str, value: object) -> None:
        if operation == "grimmory-sso":
            username, collections, issues = cast("tuple[str, object, object]", value)
            if username:
                self._user.setText(username)
            self.handle("grimmory-connect", (collections, issues))
        elif operation == "grimmory-connect":
            _collections, issues = cast(
                "tuple[tuple[GrimmoryCollection, ...], tuple[str, ...]]", value
            )
            self.say(
                "\n".join(
                    (
                        self.tr(
                            "Connected. Your audiobooks appear in Sync with Host's media list."
                        ),
                        *issues[:2],
                    )
                )
            )
        elif operation == "grimmory-progress-plan":
            self._review_progress(cast("ProgressPlan", value))
        elif operation == "grimmory-progress-send":
            result = cast("ProgressSent", value)
            lines = [
                self.tr(
                    "Sent {sent} positions to Grimmory and moved {pulled} iPod bookmarks."
                ).format(sent=result.sent, pulled=result.pulled)
            ]
            if result.pulled:
                lines.append(
                    self.tr(
                        "Choose Review Changes in the sidebar to save the bookmarks to the iPod."
                    )
                )
            lines.extend(result.failed[:5])
            self.say("\n".join(lines))

    def _plan_progress(self) -> None:
        if self.controller.busy:
            return
        self.start("grimmory-progress-plan")
        self.controller.grimmory_progress_plan(self._workspace.tracks)

    def _review_progress(self, plan: ProgressPlan) -> None:
        if not plan.to_grimmory and not plan.to_ipod:
            self.say(
                self.tr(
                    "Listening progress already matches for {count} audiobooks."
                ).format(count=plan.unchanged)
            )
            self.start("grimmory-progress-send")
            self.controller.grimmory_send_progress(plan, pulled=True)
            return
        lines = [
            self.tr("{title}: iPod {position} → Grimmory").format(
                title=push.title, position=_clock(push.position_ms)
            )
            for push in plan.to_grimmory
        ] + [
            self.tr("{title}: Grimmory {position} → iPod").format(
                title=pull.title, position=_clock(pull.position_ms)
            )
            for pull in plan.to_ipod
        ]
        box = QMessageBox(self)
        box.setWindowTitle(self.tr("Sync listening progress?"))
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            self.tr(
                "{grimmory} audiobooks get a new position in Grimmory and {ipod} get a new bookmark on the iPod."
            ).format(grimmory=len(plan.to_grimmory), ipod=len(plan.to_ipod))
        )
        details = "\n".join(lines[:12]) + ("\n…" if len(lines) > 12 else "")
        if plan.to_ipod:
            details += "\n\n" + self.tr(
                "iPod bookmarks are applied as Library changes; choose Review Changes in the sidebar to save them to the iPod."
            )
        box.setInformativeText(details)
        box.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        if box.exec() != QMessageBox.StandardButton.Ok:
            self.say(self.tr("Listening progress was left as it is."))
            return
        pulled = True
        if plan.to_ipod:
            from iOpenPod.app.library_workspace import TrackUpdate

            try:
                self._workspace.apply_track_edits(
                    tuple(
                        TrackUpdate(
                            change.track_id,
                            tuple(
                                TrackFieldEdit(field, value)
                                for field, value in change.edits
                            ),
                        )
                        for pull in plan.to_ipod
                        for change in pull.changes
                    ),
                    self._workspace.edit_revision,
                )
            except ValueError as error:
                self.say(str(error))
                pulled = False
        self.start("grimmory-progress-send")
        self.controller.grimmory_send_progress(plan, pulled=pulled)


# --- Memos → Notes -----------------------------------------------------------------------


class NotesPage(_ConnectedPage):
    def __init__(
        self, controller: ConnectedController, parent: QWidget | None = None
    ) -> None:
        self._loading = False
        super().__init__(controller, "Notes", parent)
        self.setObjectName("connectedNotesPage")
        self._header.set_title(self.tr("Notes from Memos"))
        self._send = _button(
            self.tr("Send notes to iPod"), self, ActionButtonKind.PRIMARY
        )
        self._send.clicked.connect(self._publish)
        self._header.add_action(self._send)
        config = controller.config.memos
        account = QGroupBox(self.tr("Memos server"), self)
        self._url = QLineEdit(config.url, account)
        self._url.setPlaceholderText("https://memos.example.com")
        self._token = QLineEdit(account)
        self._token.setEchoMode(QLineEdit.EchoMode.Password)
        self._token.setPlaceholderText(self.tr("Access token"))
        self._connect = _button(self.tr("Connect"), account, ActionButtonKind.PRIMARY)
        self._connect.clicked.connect(self._connect_server)
        self._mine = QCheckBox(self.tr("Only my memos"), account)
        self._mine.setChecked(config.only_mine)
        self._archived = QCheckBox(self.tr("Include archived memos"), account)
        self._archived.setChecked(config.include_archived)
        for box in (self._mine, self._archived):
            box.toggled.connect(self._save_options)
        form = QFormLayout(account)
        form.addRow(self.tr("Address"), self._url)
        form.addRow(self.tr("Access token"), self._token)
        form.addRow(
            "",
            _description(
                self.tr(
                    "Create one in Memos → Settings → My Account → Access Tokens. Works with Memos 26.10 and earlier versions."
                ),
                account,
            ),
        )
        form.addRow("", _row(self._mine, self._archived))
        form.addRow("", _row(self._connect))

        tags = QGroupBox(self.tr("Tags to sync"), self)
        self._tags = QListWidget(tags)
        self._tags.setMinimumHeight(220)
        self._tags.itemChanged.connect(self._tag_toggled)
        self._match = QComboBox(tags)
        self._match.addItem(self.tr("Memos with any of these tags"), TagMatch.ANY.value)
        self._match.addItem(self.tr("Memos with all of these tags"), TagMatch.ALL.value)
        self._match.setCurrentIndex(0 if config.match is TagMatch.ANY else 1)
        self._match.currentIndexChanged.connect(self._save_options)
        layout = QVBoxLayout(tags)
        layout.addWidget(
            _description(
                self.tr(
                    "Picking #work also includes nested tags like #work/meetings. Each memo becomes a note titled by its first line, directly in the iPod's Notes list; long memos are split into parts because the iPod shows 4 KB per note and up to 1,000 notes."
                ),
                tags,
            )
        )
        layout.addWidget(self._tags, 1)
        layout.addLayout(_row(self._match))
        self.body.addWidget(account)
        self.body.addWidget(tags, 1)
        self._render_saved_tags()
        self.refresh()

    def refresh(self) -> None:
        if not hasattr(self, "_send"):
            return
        busy = self.controller.busy
        self._connect.setEnabled(not busy)
        self._send.setEnabled(not busy and bool(self.controller.config.memos.tags))

    def _connect_server(self) -> None:
        self.start("memos-connect")
        self.say(self.tr("Reading your memos…"))
        self.controller.memos_connect(self._url.text(), self._token.text())
        self._token.clear()

    def _render_saved_tags(self) -> None:
        self._loading = True
        self._tags.clear()
        for tag in self.controller.config.memos.tags:
            self._tags.addItem(_checkable(f"#{tag}", tag, True))
        self._loading = False

    def handle(self, operation: str, value: object) -> None:
        if operation == "memos-connect":
            summary = cast("MemosSummary", value)
            chosen = {tag.casefold() for tag in self.controller.config.memos.tags}
            self._loading = True
            self._tags.clear()
            for tag in summary.tags:
                self._tags.addItem(
                    _checkable(
                        f"#{tag.tag}",
                        tag.tag,
                        tag.tag.casefold() in chosen,
                        str(tag.count),
                    )
                )
            self._loading = False
            self.say(
                self.tr(
                    "Connected as {user}: {memos} memos, {tags} tags ({untagged} memos have no tags)."
                ).format(
                    user=summary.user,
                    memos=summary.memos,
                    tags=len(summary.tags),
                    untagged=summary.untagged,
                )
            )
        elif operation == "notes-publish":
            plan, result = cast("tuple[NotesPlan, ExtrasResult]", value)
            lines = [
                self.tr(
                    "{notes} notes on the iPod from {matched} of {total} memos ({written} files written, {removed} removed)."
                ).format(
                    notes=plan.notes,
                    matched=plan.matched,
                    total=plan.total,
                    written=result.written,
                    removed=result.removed,
                )
            ]
            if plan.skipped:
                lines.append(
                    self.tr(
                        "{count} memos didn't fit: the iPod shows at most 1,000 notes."
                    ).format(count=plan.skipped)
                )
            lines.extend(result.issues[:5])
            lines.append(self.tr("Eject the iPod, then look in Extras → Notes."))
            self.say("\n".join(lines))

    def _tag_toggled(self, _entry: QListWidgetItem) -> None:
        if self._loading:
            return
        tags = tuple(
            str(self._tags.item(row).data(_ROLE))
            for row in range(self._tags.count())
            if self._tags.item(row).checkState() == Qt.CheckState.Checked
        )
        self.controller.update_config(
            replace(
                self.controller.config,
                memos=replace(self.controller.config.memos, tags=tags),
            )
        )

    def _save_options(self) -> None:
        self.controller.update_config(
            replace(
                self.controller.config,
                memos=replace(
                    self.controller.config.memos,
                    match=TagMatch(str(self._match.currentData())),
                    only_mine=self._mine.isChecked(),
                    include_archived=self._archived.isChecked(),
                ),
            )
        )

    def _publish(self) -> None:
        self.start("notes-publish")
        self.controller.publish_notes()


# --- Calendars ---------------------------------------------------------------------------


class CalendarsPage(_ConnectedPage):
    def __init__(
        self, controller: ConnectedController, parent: QWidget | None = None
    ) -> None:
        super().__init__(controller, "Calendars", parent)
        self.setObjectName("connectedCalendarsPage")
        self._header.set_title(self.tr("Calendars"))
        self._send = _button(
            self.tr("Send calendars to iPod"), self, ActionButtonKind.PRIMARY
        )
        self._send.clicked.connect(self._publish)
        self._header.add_action(self._send)
        files = QGroupBox(self.tr("Calendar files"), self)
        self._files = QListWidget(files)
        self._files.setMaximumHeight(160)
        add_file = _button(self.tr("Add .ics file…"), files)
        add_file.clicked.connect(self._add_file)
        rename_file = _button(self.tr("Rename"), files, ActionButtonKind.QUIET)
        rename_file.clicked.connect(self._rename_file)
        remove_file = _button(self.tr("Remove"), files, ActionButtonKind.QUIET)
        remove_file.clicked.connect(self._remove_file)
        files_layout = QVBoxLayout(files)
        files_layout.addWidget(self._files)
        files_layout.addLayout(_row(add_file, rename_file, remove_file))
        links = QGroupBox(self.tr("Calendar links"), self)
        self._links = QListWidget(links)
        self._links.setMaximumHeight(160)
        add_link = _button(self.tr("Add link…"), links)
        add_link.clicked.connect(self._add_link)
        rename_link = _button(self.tr("Rename"), links, ActionButtonKind.QUIET)
        rename_link.clicked.connect(self._rename_link)
        remove_link = _button(self.tr("Remove"), links, ActionButtonKind.QUIET)
        remove_link.clicked.connect(self._remove_link)
        links_layout = QVBoxLayout(links)
        links_layout.addWidget(
            _description(
                self.tr(
                    "Subscription links (https:// or webcal://, such as Google Calendar's secret iCal address) are downloaded fresh each time and kept in your system keychain because they can contain private tokens."
                ),
                links,
            )
        )
        links_layout.addWidget(self._links)
        links_layout.addLayout(_row(add_link, rename_link, remove_link))
        self._trim = QComboBox(self)
        for label, days in (
            (self.tr("Keep everything"), 0),
            (self.tr("Leave out events that ended over a week ago"), 7),
            (self.tr("Leave out events that ended over a month ago"), 30),
            (self.tr("Leave out events that ended over 3 months ago"), 90),
            (self.tr("Leave out events that ended over a year ago"), 365),
        ):
            self._trim.addItem(label, days)
        index = self._trim.findData(controller.config.calendars.trim_days)
        self._trim.setCurrentIndex(max(0, index))
        self._trim.currentIndexChanged.connect(self._trim_changed)
        self.body.addWidget(
            _description(
                self.tr(
                    "Calendars appear in Extras → Calendars on iPods that have it. Each send replaces only the calendars iOpenPod put there."
                ),
                self,
            )
        )
        self.body.addWidget(files)
        self.body.addWidget(links)
        self.body.addLayout(_row(self._trim))
        self.body.addStretch(1)

    def refresh(self) -> None:
        if not hasattr(self, "_send"):
            return
        self._send.setEnabled(not self.controller.busy)
        if self.isVisible():
            self._render()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # Calendar links live in the OS keychain; read them only when shown.
        self._render()

    def _render(self) -> None:
        if not hasattr(self, "_files"):
            return
        self._files.clear()
        for item in self.controller.config.calendars.files:
            entry = QListWidgetItem(item.name)
            entry.setData(_ROLE, item.filename)
            self._files.addItem(entry)
        self._links.clear()
        from iOpenPod.app.connected.calendars import redact

        for index, (link, name) in enumerate(self.controller.calendar_links()):
            entry = QListWidgetItem(
                f"{name or self.tr('(calendar name)')}  ·  {redact(link)}"
            )
            entry.setData(_ROLE, index)
            self._links.addItem(entry)

    def handle(self, operation: str, value: object) -> None:
        if operation == "calendars-publish":
            count, dropped, result = cast("tuple[int, int, ExtrasResult]", value)
            lines = [
                self.tr(
                    "{count} calendars on the iPod ({dropped} old events left out)."
                ).format(count=count, dropped=dropped)
            ]
            lines.extend(result.issues[:5])
            lines.append(self.tr("Eject the iPod, then look in Extras → Calendars."))
            self.say("\n".join(lines))

    def _add_file(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, self.tr("Add calendars"), str(Path.home()), "iCalendar (*.ics)"
        )
        for path in paths:
            self.controller.add_calendar_file(Path(path))
        self._render()

    def _rename_file(self) -> None:
        if self._files.currentRow() < 0:
            return
        item = self._files.currentItem()
        name, ok = QInputDialog.getText(
            self,
            self.tr("Calendar name"),
            self.tr("Name on the iPod"),
            text=item.text(),
        )
        if ok and name.strip():
            filename = str(item.data(_ROLE))
            files = tuple(
                replace(entry, name=name.strip())
                if entry.filename == filename
                else entry
                for entry in self.controller.config.calendars.files
            )
            self.controller.update_config(
                replace(
                    self.controller.config,
                    calendars=replace(self.controller.config.calendars, files=files),
                )
            )

    def _remove_file(self) -> None:
        if self._files.currentRow() >= 0:
            item = self._files.currentItem()
            self.controller.remove_calendar_file(str(item.data(_ROLE)))

    def _add_link(self) -> None:
        link, ok = QInputDialog.getText(
            self, self.tr("Calendar link"), self.tr("Link (https:// or webcal://)")
        )
        if not ok or not link.strip():
            return
        from iOpenPod.app.connected.calendars import subscription_url
        from iOpenPod.app.connected.http import ConnectedError

        try:
            subscription_url(link)
        except ConnectedError as error:
            self.say(str(error))
            return
        name, _ok = QInputDialog.getText(
            self, self.tr("Calendar name"), self.tr("Name on the iPod (optional)")
        )
        links = self.controller.calendar_links()
        links.append((link.strip(), name.strip()))
        self.controller.set_calendar_links(links)

    def _rename_link(self) -> None:
        if self._links.currentRow() < 0:
            return
        item = self._links.currentItem()
        index = int(item.data(_ROLE))
        links = self.controller.calendar_links()
        name, ok = QInputDialog.getText(
            self,
            self.tr("Calendar name"),
            self.tr("Name on the iPod"),
            text=links[index][1],
        )
        if ok:
            links[index] = (links[index][0], name.strip())
            self.controller.set_calendar_links(links)

    def _remove_link(self) -> None:
        if self._links.currentRow() >= 0:
            item = self._links.currentItem()
            links = self.controller.calendar_links()
            del links[int(item.data(_ROLE))]
            self.controller.set_calendar_links(links)

    def _trim_changed(self) -> None:
        self.controller.update_config(
            replace(
                self.controller.config,
                calendars=replace(
                    self.controller.config.calendars,
                    trim_days=int(self._trim.currentData()),
                ),
            )
        )

    def _publish(self) -> None:
        self.start("calendars-publish")
        self.controller.publish_calendars()


__all__ = ["CalendarsPage", "GrimmoryPage", "ImmichPage", "NotesPage", "PlexPage"]
