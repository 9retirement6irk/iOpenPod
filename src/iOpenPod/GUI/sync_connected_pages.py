"""Sync with Host's Connected pages: choose photos, notes, calendars and contacts.

Setting a source up (addresses, sign-ins, uploads) stays on its Connected page.
These pages only choose what goes on the iPod; the choices are saved as they are
ticked, and Sync with Host sends notes, calendars and contacts once its media
changes are done. Photos are ordinary Host media: ticking an album, a person or
Favorites downloads it when photos are next updated, and its photos are ticked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QCoreApplication, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from iOpenPod.app.connected.config import ImmichPick, ImmichPickKind, TagMatch
from iOpenPod.app.connected.secrets import SecretKey
from iOpenPod.GUI.presentation.theme.tokens import LAYOUT
from iOpenPod.GUI.widgets.themed_buttons import ActionButton, ActionButtonKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from PySide6.QtGui import QShowEvent

    from iOpenPod.app.connected.immich import ImmichCollection
    from iOpenPod.app.connected.memos import Memo
    from iOpenPod.app.connected_controller import ConnectedController

_ROLE = Qt.ItemDataRole.UserRole
_CHECKED = Qt.CheckState.Checked
_UNCHECKED = Qt.CheckState.Unchecked


def _state(checked: bool) -> Qt.CheckState:
    return _CHECKED if checked else _UNCHECKED


def _checkable(
    parent: QTreeWidget | QTreeWidgetItem, text: str, data: object, checked: bool
) -> QTreeWidgetItem:
    item = QTreeWidgetItem(parent, [text])
    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
    item.setCheckState(0, _state(checked))
    item.setData(0, _ROLE, data)
    return item


def _children(item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
    found = (item.child(row) for row in range(item.childCount()))
    return [child for child in found if child is not None]


def _tops(tree: QTreeWidget) -> list[QTreeWidgetItem]:
    found = (tree.topLevelItem(row) for row in range(tree.topLevelItemCount()))
    return [item for item in found if item is not None]


def _heading(tree: QTreeWidget, text: str) -> QTreeWidgetItem:
    item = QTreeWidgetItem(tree, [text])
    item.setFlags(Qt.ItemFlag.ItemIsEnabled)
    font = item.font(0)
    font.setBold(True)
    item.setFont(0, font)
    item.setExpanded(True)
    return item


class _SyncConnectedPage(QWidget):
    """A title, a short explanation, a checklist and a status line."""

    operation = ""

    def __init__(self, controller: ConnectedController, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.controller = controller
        self._loading = False
        self._waiting = False
        self._title = QLabel(self)
        self._title.setObjectName("syncConnectedTitle")
        font = self._title.font()
        font.setPointSizeF(font.pointSizeF() * 1.3)
        font.setBold(True)
        self._title.setFont(font)
        self._description = QLabel(self)
        self._description.setObjectName("pageDescription")
        self._description.setWordWrap(True)
        self._description.setTextFormat(Qt.TextFormat.PlainText)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setObjectName("syncConnectedChecklist")
        self.tree.itemChanged.connect(self._item_changed)
        self._status = QLabel(self)
        self._status.setObjectName("pageDescription")
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self.footer = QHBoxLayout()
        self.footer.setSpacing(LAYOUT.space_xs)
        self.footer.addWidget(self._status, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            LAYOUT.space_lg, LAYOUT.space_md, LAYOUT.space_lg, LAYOUT.space_md
        )
        layout.setSpacing(LAYOUT.space_sm)
        layout.addWidget(self._title)
        layout.addWidget(self._description)
        layout.addWidget(self.tree, 1)
        layout.addLayout(self.footer)
        controller.result.connect(self._result)
        controller.message.connect(self._message)

    def say(self, text: str) -> None:
        self._status.setText(text)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.reload()

    def reload(self) -> None:
        """Fill the checklist; pages that read a server start a job here."""

    def _start(self, started: bool) -> None:
        self._waiting = started
        if started:
            self.say(QCoreApplication.translate("_SyncConnectedPage", "Loading…"))
        else:
            self.say(
                QCoreApplication.translate(
                    "_SyncConnectedPage",
                    "Another Connected Source task is running. Open this page again in a moment.",
                )
            )

    def _result(self, operation: str, value: object) -> None:
        if operation == self.operation and self._waiting:
            self._waiting = False
            self.loaded(value)

    def _message(self, text: str) -> None:
        if self._waiting and not self.controller.busy:
            self._waiting = False
            self.say(text)

    def loaded(self, value: object) -> None:
        """A job started by reload() finished."""

    def _item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if not self._loading:
            self.toggled(item, item.checkState(0) == _CHECKED)

    def toggled(self, item: QTreeWidgetItem, checked: bool) -> None:
        """The person ticked or unticked ``item``."""

    def reset(self) -> None:
        """Forget the listed items so the next visit reads them again."""

        self._loading = True
        self.tree.clear()
        self._loading = False

    def fill(self, build: Callable[[], None]) -> None:
        self._loading = True
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            build()
        finally:
            self.tree.setUpdatesEnabled(True)
            self._loading = False


# --- Photos from Immich -----------------------------------------------------------------


class ImmichSyncPage(_SyncConnectedPage):
    updateRequested = Signal()
    operation = "sync-immich"

    def __init__(
        self, controller: ConnectedController, parent: QWidget | None = None
    ) -> None:
        super().__init__(controller, parent)
        self.setObjectName("syncImmichPage")
        self._changed = False
        self._update = ActionButton(parent=self, kind=ActionButtonKind.PRIMARY)
        self._update.setObjectName("updateImmichPhotos")
        self._update.clicked.connect(self.updateRequested.emit)
        self.footer.addWidget(self._update)
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self._title.setText(self.tr("Photos from Immich"))
        self._description.setText(
            self.tr(
                "Tick Favorites, albums or people. Update photos to download them; their photos are then ticked in Photos, and your other ticks are kept. Photos picked one by one on the Immich page are included too."
            )
        )
        self._update.setText(self.tr("Update photos"))

    def reload(self) -> None:
        config = self.controller.config.immich
        if not config.url or not self.controller.secret(SecretKey.IMMICH).get(
            "api_key"
        ):
            self.tree.clear()
            self._update.setEnabled(False)
            self.say(self.tr("Connect Immich on its page under Connected first."))
            return
        self._update.setEnabled(True)
        if not self.tree.topLevelItemCount():
            self._start(self.controller.list_immich())

    def loaded(self, value: object) -> None:
        albums, people = cast(
            "tuple[tuple[ImmichCollection, ...], tuple[ImmichCollection, ...]]", value
        )
        picks = {(pick.kind, pick.id) for pick in self.controller.config.immich.picks}

        def build() -> None:
            favorites = ImmichPick(ImmichPickKind.FAVORITES, "favorites", "Favorites")
            _checkable(
                self.tree,
                self.tr("★ Favorites"),
                favorites,
                (favorites.kind, favorites.id) in picks,
            )
            for heading, collections in (
                (self.tr("Albums"), albums),
                (self.tr("People"), people),
            ):
                if not collections:
                    continue
                group = _heading(self.tree, heading)
                for collection in collections:
                    pick = ImmichPick(collection.kind, collection.id, collection.title)
                    text = collection.title
                    if collection.count:
                        text += "  ·  " + self.tr("{count} items").format(
                            count=collection.count
                        )
                    _checkable(group, text, pick, (pick.kind, pick.id) in picks)
                group.setExpanded(True)

        self.fill(build)
        singles = sum(
            pick.kind is ImmichPickKind.ASSET
            for pick in self.controller.config.immich.picks
        )
        self.say(
            self.tr("{count} photos picked one by one are included too.").format(
                count=singles
            )
            if singles
            else ""
        )

    def toggled(self, item: QTreeWidgetItem, checked: bool) -> None:
        pick = item.data(0, _ROLE)
        if isinstance(pick, ImmichPick):
            self.controller.set_immich_pick(pick, checked)
            self._changed = True
            self.say(self.tr("Update photos to bring these changes into this Sync."))

    @property
    def changed(self) -> bool:
        return self._changed

    def reset(self) -> None:
        super().reset()
        self._changed = False


# --- Notes from Memos -------------------------------------------------------------------


class NotesSyncPage(_SyncConnectedPage):
    operation = "sync-memos"

    def __init__(
        self, controller: ConnectedController, parent: QWidget | None = None
    ) -> None:
        super().__init__(controller, parent)
        self.setObjectName("syncNotesPage")
        self._memos: tuple[Memo, ...] = ()
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self._title.setText(self.tr("Notes from Memos"))
        self._describe()

    def _describe(self) -> None:
        match = self.controller.config.memos.match
        self._description.setText(
            (
                self.tr("Memos with all of the ticked tags become notes.")
                if match is TagMatch.ALL
                else self.tr("Memos with any of the ticked tags become notes.")
            )
            + " "
            + self.tr(
                "Open a tag to leave out single memos. Notes are sent at the end of the Sync."
            )
        )

    def reload(self) -> None:
        self._describe()
        if not self.controller.config.memos.url or not self.controller.secret(
            SecretKey.MEMOS
        ).get("token"):
            self.tree.clear()
            self.say(self.tr("Connect Memos on the Notes page under Connected first."))
            return
        if not self.tree.topLevelItemCount():
            self._start(self.controller.list_memos())

    def loaded(self, value: object) -> None:
        from iOpenPod.app.connected.memos import summarize, tag_matches

        self._memos = cast("tuple[Memo, ...]", value)
        config = self.controller.config.memos
        chosen = {tag.casefold() for tag in config.tags}
        skipped = set(config.skipped_memos)
        summary = summarize(self._memos, "")

        def build() -> None:
            for tag in summary.tags:
                ticked = tag.tag.casefold() in chosen
                parent = _checkable(
                    self.tree, f"#{tag.tag}  ·  {tag.count}", tag.tag, ticked
                )
                for memo in self._memos:
                    if any(tag_matches(own, tag.tag) for own in memo.tags):
                        child = _checkable(
                            parent,
                            memo.title or self.tr("(untitled memo)"),
                            memo.name,
                            memo.name not in skipped,
                        )
                        child.setDisabled(not ticked)

        self.fill(build)
        self._summarize()

    def toggled(self, item: QTreeWidgetItem, checked: bool) -> None:
        value = str(item.data(0, _ROLE))
        if item.parent() is None:
            self.controller.set_memo_tag(value, checked)
            self._loading = True
            for child in _children(item):
                child.setDisabled(not checked)
            self._loading = False
        else:
            self.controller.set_memos_included((value,), checked)
            self._loading = True  # the same memo can sit under several tags
            for parent in _tops(self.tree):
                for child in _children(parent):
                    if child.data(0, _ROLE) == value:
                        child.setCheckState(0, _state(checked))
            self._loading = False
        self._summarize()

    def _summarize(self) -> None:
        from iOpenPod.app.connected.memos import tag_matches

        config = self.controller.config.memos
        wanted = [tag for tag in config.tags if tag.strip()]
        skipped = set(config.skipped_memos)
        count = 0
        for memo in self._memos:
            if memo.name in skipped or not wanted:
                continue
            hits = sum(
                any(tag_matches(own, want) for own in memo.tags) for want in wanted
            )
            count += hits == len(wanted) if config.match is TagMatch.ALL else hits > 0
        self.say(self.tr("{count} memos will go on the iPod.").format(count=count))


# --- Calendars --------------------------------------------------------------------------


class CalendarsSyncPage(_SyncConnectedPage):
    def __init__(
        self, controller: ConnectedController, parent: QWidget | None = None
    ) -> None:
        super().__init__(controller, parent)
        self.setObjectName("syncCalendarsPage")
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self._title.setText(self.tr("Calendars"))
        self._description.setText(
            self.tr(
                "Ticked calendars are sent at the end of the Sync; links are downloaded fresh each time. Add files and links on the Calendars page under Connected."
            )
        )

    def reload(self) -> None:
        skipped = set(self.controller.config.calendars.skipped)
        choices = self.controller.calendar_choices()

        def build() -> None:
            for key, name in choices:
                _checkable(self.tree, name, key, key not in skipped)

        self.fill(build)
        self.say("" if choices else self.tr("No calendars added yet."))

    def toggled(self, item: QTreeWidgetItem, checked: bool) -> None:
        self.controller.set_calendar_included(str(item.data(0, _ROLE)), checked)


# --- Contacts ---------------------------------------------------------------------------


class ContactsSyncPage(_SyncConnectedPage):
    def __init__(
        self, controller: ConnectedController, parent: QWidget | None = None
    ) -> None:
        super().__init__(controller, parent)
        self.setObjectName("syncContactsPage")
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self._title.setText(self.tr("Contacts"))
        self._description.setText(
            self.tr(
                "Ticked contacts are sent at the end of the Sync and appear in Extras → Contacts. Add vCard files on the Contacts page under Connected."
            )
        )

    def reload(self) -> None:
        from iOpenPod.app.connected.contacts import parse_contacts

        skipped = set(self.controller.config.contacts.skipped)
        files = [
            (item.name, parse_contacts(text))
            for item, text in self.controller.contact_files()
        ]

        def build() -> None:
            for name, contacts in files:
                parent = QTreeWidgetItem(self.tree, [name])
                parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                for contact in sorted(contacts, key=lambda c: c.name.casefold()):
                    _checkable(
                        parent, contact.name, contact.key, contact.key not in skipped
                    )
                self._settle(parent)

        self.fill(build)
        self._summarize()

    def _settle(self, parent: QTreeWidgetItem) -> None:
        states = {child.checkState(0) for child in _children(parent)}
        parent.setCheckState(
            0,
            Qt.CheckState.PartiallyChecked
            if len(states) > 1
            else (states.pop() if states else _UNCHECKED),
        )

    def toggled(self, item: QTreeWidgetItem, checked: bool) -> None:
        parent = item.parent()
        self._loading = True
        try:
            if parent is None:
                keys = [str(child.data(0, _ROLE)) for child in _children(item)]
                self.controller.set_contacts_included(keys, checked)
                for child in _children(item):
                    child.setCheckState(0, _state(checked))
            else:
                self.controller.set_contacts_included(
                    (str(item.data(0, _ROLE)),), checked
                )
                self._settle(parent)
        finally:
            self._loading = False
        self._summarize()

    def _summarize(self) -> None:
        total = ticked = 0
        for parent in _tops(self.tree):
            for child in _children(parent):
                total += 1
                ticked += child.checkState(0) == _CHECKED
        self.say(
            self.tr("{ticked} of {total} contacts will go on the iPod.").format(
                ticked=ticked, total=total
            )
            if total
            else self.tr("No contacts added yet.")
        )


__all__ = [
    "CalendarsSyncPage",
    "ContactsSyncPage",
    "ImmichSyncPage",
    "NotesSyncPage",
]
