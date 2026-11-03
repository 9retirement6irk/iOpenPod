"""The Plex and Grimmory media cache within Settings → Sync."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QUrl
from PySide6.QtWidgets import QCheckBox, QLabel, QSpinBox, QVBoxLayout, QWidget

from iOpenPod.GUI.presentation.desktop_services import open_desktop_url
from iOpenPod.GUI.presentation.theme.tokens import LAYOUT
from iOpenPod.GUI.widgets.setting_group import SettingGroup, SettingRow
from iOpenPod.GUI.widgets.themed_buttons import ActionButton

if TYPE_CHECKING:
    from iOpenPod.app.connected_controller import ConnectedController


class MediaCacheSettings(QWidget):
    def __init__(
        self, controller: ConnectedController, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._title = QLabel(self)
        self._title.setObjectName("sectionTitle")
        self._keep = QCheckBox(self)
        self._keep.setObjectName("cacheMedia")
        self._limit = QSpinBox(self)
        self._limit.setObjectName("cacheLimit")
        self._limit.setRange(1, 4096)
        self._show = ActionButton(parent=self)
        self._show.clicked.connect(self._open_folder)
        self._keep_row = SettingRow("", "", self._keep, self)
        self._limit_row = SettingRow("", "", self._limit, self)
        self._folder_row = SettingRow("", "", self._show, self)
        group = SettingGroup(self)
        group.setMaximumWidth(960)
        for row in (self._keep_row, self._limit_row, self._folder_row):
            group.add_row(row)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(LAYOUT.space_xs)
        layout.addWidget(self._title)
        layout.addWidget(group)
        self._keep.toggled.connect(self._save)
        self._limit.editingFinished.connect(self._save)
        controller.changed.connect(self._refresh)
        self.retranslate_ui()

    def _refresh(self) -> None:
        config = self._controller.config
        for control in (self._keep, self._limit):
            control.blockSignals(True)
        self._keep.setChecked(config.cache_media)
        self._limit.setValue(max(1, config.cache_limit_gb))
        for control in (self._keep, self._limit):
            control.blockSignals(False)
        self._limit.setEnabled(config.cache_media)

    def _save(self) -> None:
        self._controller.set_cache(
            enabled=self._keep.isChecked(), limit_gb=self._limit.value()
        )

    def _open_folder(self) -> None:
        root = self._controller.config.root()
        root.mkdir(parents=True, exist_ok=True)
        open_desktop_url(QUrl.fromLocalFile(str(root)))

    def retranslate_ui(self) -> None:
        self._title.setText(self.tr("Plex and Grimmory media"))
        self._keep.setText(self.tr("On"))
        self._keep_row.set_copy(
            self.tr("Keep downloads between Syncs"),
            self.tr(
                "Plex songs and Grimmory audiobooks download when Sync copies them to the iPod. Kept downloads aren't fetched again for later Syncs; when this is off, each Sync downloads what it needs and removes it afterwards."
            ),
        )
        self._limit_row.set_copy(
            self.tr("Cache limit"),
            self.tr(
                "After each Sync, the least recently synced downloads are removed until the cache fits. Covers are always kept."
            ),
        )
        self._limit.setSuffix(self.tr(" GB"))
        self._limit.setAccessibleName(self.tr("Cache limit in gigabytes"))
        self._folder_row.set_copy(
            self.tr("Cache folder"),
            str(self._controller.config.root()),
        )
        self._show.setText(self.tr("Show folder"))
        self._refresh()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate_ui()
        super().changeEvent(event)


__all__ = ["MediaCacheSettings"]
