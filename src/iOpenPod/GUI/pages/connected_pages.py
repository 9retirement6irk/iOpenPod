"""Connected Source pages: Plex, Immich, Grimmory audiobooks, Memos notes, calendars."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QCoreApplication, QSize, Qt, QUrl
from PySide6.QtGui import QIcon, QPixmap, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from iOpenPod.app.connected.config import (
    GrimmoryPick,
    ImmichPick,
    ImmichPickKind,
    ImmichSize,
    PathMapping,
    PlexPick,
    PlexPickKind,
    TagMatch,
)
from iOpenPod.app.connected.secrets import SecretKey
from iOpenPod.app.connected_controller import ConnectedSource, RefreshOutcome
from iOpenPod.app.scrobbling.models import Credentials, Service
from iOpenPod.GUI.presentation.desktop_services import open_desktop_url
from iOpenPod.GUI.presentation.theme.tokens import LAYOUT
from iOpenPod.GUI.widgets.browser_chrome import PageHeader
from iOpenPod.GUI.widgets.themed_buttons import ActionButton, ActionButtonKind
from iPodDB.library import TrackFieldEdit

if TYPE_CHECKING:
    from collections.abc import Callable

    from iOpenPod.app.connected.grimmory import Audiobook, GrimmoryClient, OpdsShelf
    from iOpenPod.app.connected.immich import (
        ImmichClient,
        ImmichCollection,
        ImmichPhoto,
    )
    from iOpenPod.app.connected.memos import MemosSummary, NotesPlan
    from iOpenPod.app.connected.plex import (
        PlexClient,
        PlexItem,
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


def _cover(client: GrimmoryClient, book: Audiobook) -> Callable[[], bytes]:
    return lambda: client.cover(book)


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
        self._section = ""
        self._loading_lists = False
        super().__init__(controller, "Plex", parent)
        self.setObjectName("plexPage")
        self._header.set_title(self.tr("Plex"))
        self._download = _button(
            self.tr("Download for Sync"), self, ActionButtonKind.PRIMARY
        )
        self._download.clicked.connect(self._refresh_downloads)
        self._header.add_action(self._download)

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
        self._send_plays = QCheckBox(
            self.tr("Send iPod plays to Plex during Sync"), plays
        )
        self._send_plays.setToolTip(
            self.tr(
                "Each play is reported as a finished listen from a player named after your iPod (platform iPod), so it appears in Plex Play History."
            )
        )
        self._send_plays.toggled.connect(self._toggle_plays)
        self._ratings = _button(self.tr("Sync star ratings now…"), plays)
        self._ratings.clicked.connect(self._plan_ratings)
        plays_layout = QVBoxLayout(plays)
        plays_layout.addWidget(self._send_plays)
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

        picker = QGroupBox(self.tr("Choose music"), self)
        self._tabs = QTabWidget(picker)
        self._playlists = QListWidget(self._tabs)
        self._artists = QListWidget(self._tabs)
        self._albums = QListWidget(self._tabs)
        artist_split = QSplitter(self._tabs)
        artist_split.addWidget(self._artists)
        artist_split.addWidget(self._albums)
        self._artists.currentItemChanged.connect(self._artist_selected)
        search = QWidget(self._tabs)
        self._query = QLineEdit(search)
        self._query.setPlaceholderText(self.tr("Search artists, albums and songs"))
        self._query.returnPressed.connect(self._search)
        self._results = QListWidget(search)
        search_layout = QVBoxLayout(search)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.addWidget(self._query)
        search_layout.addWidget(self._results)
        self._tabs.addTab(self._playlists, self.tr("Playlists"))
        self._tabs.addTab(artist_split, self.tr("Artists and albums"))
        self._tabs.addTab(search, self.tr("Search"))
        for widget in (self._playlists, self._artists, self._albums, self._results):
            widget.itemChanged.connect(self._pick_toggled)
            widget.setMinimumHeight(260)
        self._selected = QListWidget(picker)
        self._selected.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._selected.setMaximumWidth(320)
        remove = _button(
            self.tr("Remove from selection"), picker, ActionButtonKind.QUIET
        )
        remove.clicked.connect(self._remove_selected)
        reload = _button(self.tr("Reload Plex library"), picker, ActionButtonKind.QUIET)
        reload.clicked.connect(self._load_library)
        open_folder = _button(self.tr("Show downloads"), picker, ActionButtonKind.QUIET)
        open_folder.clicked.connect(lambda: self.open_folder(ConnectedSource.PLEX))
        selected_column = QVBoxLayout()
        selected_column.addWidget(QLabel(self.tr("Selected for the iPod"), picker))
        selected_column.addWidget(self._selected, 1)
        selected_column.addWidget(remove)
        picker_layout = QVBoxLayout(picker)
        top = QHBoxLayout()
        top.addWidget(self._tabs, 1)
        top.addLayout(selected_column)
        picker_layout.addLayout(top)
        picker_layout.addLayout(_row(reload, open_folder))

        for icon_list in (self._playlists, self._albums, self._results):
            icon_list.setIconSize(QSize(40, 40))
        controller.previewLoaded.connect(self._preview)
        self.body.addWidget(account)
        self.body.addWidget(picker, 1)
        self.body.addWidget(plays)
        self.body.addWidget(mappings)
        self._load_mappings()
        self._render_selected()
        self.refresh()

    # --- state ---------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if (
            self.controller.plex_restore()
            and not self._playlists.count()
            and not self.controller.busy
        ):
            self._load_library()

    def refresh(self) -> None:
        if not hasattr(self, "_download"):
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
        for widget in (self._sign_in, self._use_server, self._ratings, self._download):
            widget.setEnabled(not busy)
        self._download.setEnabled(not busy and connected and bool(config.picks))
        self._ratings.setEnabled(not busy and connected)
        self._send_plays.blockSignals(True)
        self._send_plays.setChecked(config.send_plays)
        self._send_plays.setEnabled(connected)
        self._send_plays.blockSignals(False)

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
            self.say(self.tr("Connected to {name}.").format(name=server.name))
            self._load_library()
        elif operation == "plex-library":
            playlists, artists, section = cast(
                "tuple[tuple[PlexItem, ...], tuple[PlexItem, ...], str]", value
            )
            self._section = section
            self._fill(self._playlists, playlists)
            self._fill(self._artists, artists)
            self.say(
                self.tr("{playlists} playlists and {artists} artists.").format(
                    playlists=len(playlists), artists=len(artists)
                )
            )
        elif operation == "plex-albums":
            self._fill(self._albums, cast("tuple[PlexItem, ...]", value))
            self.say("")
        elif operation == "plex-search":
            results = cast("tuple[PlexItem, ...]", value)
            self._fill(self._results, results)
            self.say(self.tr("{count} results.").format(count=len(results)))
        elif operation == "refresh":
            self.refresh_outcome(value)
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
        if self.controller.config.plex.send_plays:
            self._scrobbling.disconnect_service(Service.PLEX)
        self.controller.forget(SecretKey.PLEX)
        self.controller.update_config(
            replace(
                self.controller.config,
                plex=replace(self.controller.config.plex, send_plays=False),
            )
        )
        for widget in (self._playlists, self._artists, self._albums, self._results):
            widget.clear()

    def _load_library(self) -> None:
        self.start("plex-library")
        self.say(self.tr("Reading your Plex library…"))
        plex = self.controller.plex

        def work(server: PlexServer, _job: object) -> object:
            sections = plex.music_sections(server)
            section = sections[0][0] if sections else ""
            artists = plex.artists(server, section) if section else ()
            return (plex.playlists(server), artists, section)

        self.controller.run_plex("plex-library", work)

    def _artist_selected(
        self, current: QListWidgetItem | None, _previous: object
    ) -> None:
        if current is None or self.controller.busy:
            return
        item = cast("PlexItem", current.data(_ROLE))
        plex = self.controller.plex
        self.start("plex-albums")
        self.controller.run_plex(
            "plex-albums", lambda server, _job: plex.albums(server, item.key)
        )

    def _search(self) -> None:
        text = self._query.text().strip()
        if not text or not self._section:
            return
        plex, section = self.controller.plex, self._section
        self.start("plex-search")
        self.controller.run_plex(
            "plex-search", lambda server, _job: plex.search(server, section, text)
        )

    # --- selection -----------------------------------------------------------------

    def _picked(self) -> dict[tuple[str, str], PlexPick]:
        return {
            (pick.kind.value, pick.key): pick
            for pick in self.controller.config.plex.picks
        }

    def _fill(self, widget: QListWidget, items: tuple[PlexItem, ...]) -> None:
        picked = self._picked()
        self._loading_lists = True
        widget.clear()
        for item in items:
            detail = item.subtitle
            if item.count:
                detail = f"{detail} · {item.count}" if detail else str(item.count)
            widget.addItem(
                _checkable(
                    item.title, item, (item.kind.value, item.key) in picked, detail
                )
            )
            server = self.controller.plex_server
            if item.thumb and server is not None and widget is not self._artists:
                self.controller.preview(
                    f"plex:{item.key}",
                    _plex_art(self.controller.plex, server, item.thumb),
                )
        self._loading_lists = False

    def _preview(self, key: str, data: bytes) -> None:
        if not key.startswith("plex:") or not data:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        rating_key = key.removeprefix("plex:")
        for widget in (self._playlists, self._albums, self._results):
            for row in range(widget.count()):
                entry = widget.item(row)
                if cast("PlexItem", entry.data(_ROLE)).key == rating_key:
                    entry.setIcon(QIcon(pixmap))

    def _pick_toggled(self, entry: QListWidgetItem) -> None:
        if self._loading_lists:
            return
        item = cast("PlexItem", entry.data(_ROLE))
        picks = self._picked()
        key = (item.kind.value, item.key)
        if entry.checkState() == Qt.CheckState.Checked:
            picks[key] = PlexPick(item.kind, item.key, item.title, item.subtitle)
        else:
            picks.pop(key, None)
        self.controller.set_plex_picks(tuple(picks.values()))
        self._render_selected()

    def _render_selected(self) -> None:
        self._selected.clear()
        labels = {
            PlexPickKind.PLAYLIST: self.tr("Playlist"),
            PlexPickKind.ARTIST: self.tr("Artist"),
            PlexPickKind.ALBUM: self.tr("Album"),
            PlexPickKind.TRACK: self.tr("Song"),
        }
        for pick in self.controller.config.plex.picks:
            entry = QListWidgetItem(f"{labels[pick.kind]}: {pick.title}")
            entry.setData(_ROLE, pick)
            self._selected.addItem(entry)

    def _remove_selected(self) -> None:
        gone = {
            id(cast("PlexPick", item.data(_ROLE)))
            for item in self._selected.selectedItems()
        }
        keep = tuple(
            pick for pick in self.controller.config.plex.picks if id(pick) not in gone
        )
        self.controller.set_plex_picks(keep)
        self._render_selected()
        removed = {
            (pick.kind.value, pick.key) for pick in self.controller.config.plex.picks
        }
        self._loading_lists = True
        for widget in (self._playlists, self._artists, self._albums, self._results):
            for row in range(widget.count()):
                entry = widget.item(row)
                item = cast("PlexItem", entry.data(_ROLE))
                entry.setCheckState(
                    Qt.CheckState.Checked
                    if (item.kind.value, item.key) in removed
                    else Qt.CheckState.Unchecked
                )
        self._loading_lists = False

    def _refresh_downloads(self) -> None:
        self.start("refresh")
        self.controller.register_folder(ConnectedSource.PLEX)
        self.controller.refresh((ConnectedSource.PLEX,))

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

    def _toggle_plays(self, enabled: bool) -> None:
        secret = self.controller.secret(SecretKey.PLEX)
        config = self.controller.config.plex
        if enabled:
            if not secret.get("server_token"):
                self.say(self.tr("Choose a Plex server first."))
                self._send_plays.setChecked(False)
                return
            self._scrobbling.connect_plex(
                Credentials(
                    config.username or "Plex",
                    secret["server_token"],
                    secret.get("server_uri", ""),
                    secret.get("server_id", ""),
                )
            )
        else:
            self._scrobbling.disconnect_service(Service.PLEX)
        self.controller.update_config(
            replace(self.controller.config, plex=replace(config, send_plays=enabled))
        )

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
        self, controller: ConnectedController, parent: QWidget | None = None
    ) -> None:
        self._loading = False
        super().__init__(controller, "Grimmory", parent)
        self.setObjectName("grimmoryPage")
        self._header.set_title(self.tr("Grimmory Audiobooks"))
        self._download = _button(
            self.tr("Download for Sync"), self, ActionButtonKind.PRIMARY
        )
        self._download.clicked.connect(self._refresh_downloads)
        self._header.add_action(self._download)
        account = QGroupBox(self.tr("Server"), self)
        self._url = QLineEdit(controller.config.grimmory.url, account)
        self._url.setPlaceholderText("http://192.168.1.10:6060")
        self._user = QLineEdit(controller.config.grimmory.username, account)
        self._password = QLineEdit(account)
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._connect = _button(self.tr("Connect"), account, ActionButtonKind.PRIMARY)
        self._connect.clicked.connect(self._connect_server)
        form = QFormLayout(account)
        form.addRow(self.tr("Address"), self._url)
        form.addRow(self.tr("OPDS username"), self._user)
        form.addRow(self.tr("OPDS password"), self._password)
        form.addRow(
            "",
            _description(
                self.tr(
                    "Create an OPDS user in Grimmory's settings. iOpenPod never needs your main password."
                ),
                account,
            ),
        )
        form.addRow("", _row(self._connect))

        picker = QGroupBox(self.tr("Choose audiobooks"), self)
        self._shelves = QListWidget(picker)
        self._shelves.setMaximumWidth(280)
        self._shelves.currentItemChanged.connect(self._shelf_opened)
        self._query = QLineEdit(picker)
        self._query.setPlaceholderText(self.tr("Search audiobooks"))
        self._query.returnPressed.connect(self._search)
        self._books = QListWidget(picker)
        self._books.setIconSize(QSize(48, 48))
        self._books.setMinimumHeight(320)
        self._books.itemChanged.connect(self._book_toggled)
        self._summary = QLabel(picker)
        open_folder = _button(self.tr("Show downloads"), picker, ActionButtonKind.QUIET)
        open_folder.clicked.connect(lambda: self.open_folder(ConnectedSource.GRIMMORY))
        books_column = QWidget(picker)
        books_layout = QVBoxLayout(books_column)
        books_layout.setContentsMargins(0, 0, 0, 0)
        books_layout.addWidget(self._query)
        books_layout.addWidget(self._books, 1)
        split = QSplitter(picker)
        split.addWidget(self._shelves)
        split.addWidget(books_column)
        split.setStretchFactor(1, 1)
        layout = QVBoxLayout(picker)
        layout.addWidget(split, 1)
        layout.addLayout(_row(self._summary, open_folder))
        self.body.addWidget(account)
        self.body.addWidget(picker, 1)
        controller.previewLoaded.connect(self._preview)
        self.refresh()

    def refresh(self) -> None:
        if not hasattr(self, "_download"):
            return
        busy = self.controller.busy
        picks = self.controller.config.grimmory.picks
        self._connect.setEnabled(not busy)
        self._download.setEnabled(not busy and bool(picks))
        self._summary.setText(
            self.tr("{count} audiobooks selected").format(count=len(picks))
        )

    def _connect_server(self) -> None:
        self.start("grimmory-connect")
        self.say(self.tr("Connecting to Grimmory…"))
        self.controller.grimmory_connect(
            self._url.text(), self._user.text(), self._password.text()
        )
        self._password.clear()

    def handle(self, operation: str, value: object) -> None:
        if operation == "grimmory-connect":
            libraries, shelves = cast(
                "tuple[tuple[OpdsShelf, ...], tuple[OpdsShelf, ...]]", value
            )
            self._shelves.clear()
            everything = QListWidgetItem(self.tr("All audiobooks"))
            everything.setData(_ROLE, "/api/v1/opds/catalog")
            self._shelves.addItem(everything)
            for shelf in (*libraries, *shelves):
                item = QListWidgetItem(shelf.title)
                item.setData(_ROLE, shelf.href)
                self._shelves.addItem(item)
            self.say(self.tr("Connected. Choose a library or shelf."))
        elif operation == "grimmory-books":
            self._fill(cast("tuple[Audiobook, ...]", value))
        elif operation == "refresh":
            self.refresh_outcome(value)

    def _shelf_opened(self, current: QListWidgetItem | None, _previous: object) -> None:
        if current is None or self.controller.busy:
            return
        href = str(current.data(_ROLE))
        client = self.controller.grimmory()
        self.start("grimmory-books")
        self.say(self.tr("Loading audiobooks…"))
        self.controller.run(
            "grimmory-books", lambda job: client.audiobooks(href, job.cancelled)
        )

    def _search(self) -> None:
        text = self._query.text().strip()
        if not text or self.controller.busy:
            return
        client = self.controller.grimmory()
        self.start("grimmory-books")
        self.controller.run("grimmory-books", lambda _job: client.search(text))

    def _fill(self, books: tuple[Audiobook, ...]) -> None:
        picked = {pick.book_id for pick in self.controller.config.grimmory.picks}
        client = self.controller.grimmory()
        self._loading = True
        self._books.clear()
        for book in books:
            detail = " · ".join(part for part in (book.author, book.series) if part)
            self._books.addItem(
                _checkable(book.title, book, book.book_id in picked, detail)
            )
            if book.cover:
                self.controller.preview(
                    f"grimmory:{book.book_id}", _cover(client, book)
                )
        self._loading = False
        self.say(self.tr("{count} audiobooks.").format(count=len(books)))

    def _preview(self, key: str, data: bytes) -> None:
        if not key.startswith("grimmory:") or not data:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        book_id = key.removeprefix("grimmory:")
        for row in range(self._books.count()):
            item = self._books.item(row)
            if cast("Audiobook", item.data(_ROLE)).book_id == book_id:
                item.setIcon(QIcon(pixmap))

    def _book_toggled(self, entry: QListWidgetItem) -> None:
        if self._loading:
            return
        book = cast("Audiobook", entry.data(_ROLE))
        picks = {pick.book_id: pick for pick in self.controller.config.grimmory.picks}
        if entry.checkState() == Qt.CheckState.Checked:
            picks[book.book_id] = GrimmoryPick(
                book.book_id, book.title, book.author, book.download
            )
        else:
            picks.pop(book.book_id, None)
        self.controller.set_grimmory_picks(tuple(picks.values()))

    def _refresh_downloads(self) -> None:
        self.start("refresh")
        self.controller.register_folder(ConnectedSource.GRIMMORY)
        self.controller.refresh((ConnectedSource.GRIMMORY,))


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
        self._per_tag = QCheckBox(self.tr("A folder for each tag"), tags)
        self._per_tag.setChecked(config.folder_per_tag)
        self._per_tag.toggled.connect(self._save_options)
        self._folder = QLineEdit(config.folder, tags)
        self._folder.editingFinished.connect(self._save_options)
        layout = QVBoxLayout(tags)
        layout.addWidget(
            _description(
                self.tr(
                    "Picking #work also includes nested tags like #work/meetings. Each memo becomes a note titled by its first line; long memos are split into parts because the iPod shows 4 KB per note and up to 1,000 notes."
                ),
                tags,
            )
        )
        layout.addWidget(self._tags, 1)
        layout.addLayout(_row(self._match, self._per_tag))
        layout.addLayout(
            _row(QLabel(self.tr("Folder on the iPod: Notes /"), tags), self._folder)
        )
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
                    folder_per_tag=self._per_tag.isChecked(),
                    only_mine=self._mine.isChecked(),
                    include_archived=self._archived.isChecked(),
                    folder=self._folder.text().strip() or "Memos",
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
