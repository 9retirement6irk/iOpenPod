"""``iopenpod://`` links: register the address and deliver links to the running app.

A sign-in page in the browser returns to iOpenPod through a link such as
``iopenpod://oauth2-callback?code=...&state=...``. The operating system opens it
with iOpenPod:

* Windows and Linux start a second ``iOpenPod`` process with the link as its
  argument. That process hands the link to the running window over a per-user
  local socket and exits, so only one iOpenPod is ever on screen.
* macOS delivers it to the running app as a file-open event (and launches the app
  first when it isn't running).

Packaged macOS and Microsoft Store builds declare the address in their bundle or
package manifest. Other installations register it for the current user at start-up:
in the Windows registry, or as a hidden desktop entry on Linux.
"""

from __future__ import annotations

import getpass
import hashlib
import importlib
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtGui import QFileOpenEvent
from PySide6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger(__name__)

SCHEME = "iopenpod"
_MAX_LINK = 16 * 1024
_DESKTOP_ID = "io.github.therealsavi.iOpenPod-links.desktop"


def is_app_link(value: str) -> bool:
    return value.casefold().startswith(f"{SCHEME}:") and len(value) <= _MAX_LINK


def link_from(arguments: list[str]) -> str | None:
    return next((value for value in arguments if is_app_link(value)), None)


def server_name() -> str:
    """One socket name per user, so two people's iOpenPods never meet."""

    try:
        user = getpass.getuser()
    except Exception:
        user = str(Path.home())
    digest = hashlib.sha256(user.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"iOpenPod-links-{digest}"


def forward(link: str, name: str | None = None, timeout_ms: int = 1500) -> bool:
    """Give ``link`` to an iOpenPod that is already running; False if none is."""

    application = QCoreApplication.instance()
    temporary = application is None
    if temporary:
        application = QCoreApplication([sys.argv[0] if sys.argv else "iOpenPod"])
    socket = QLocalSocket()
    try:
        socket.connectToServer(name or server_name())
        if not socket.waitForConnected(timeout_ms):
            return False
        socket.write(link.encode("utf-8") + b"\n")
        socket.flush()
        # flush() may already have sent everything, leaving nothing to wait for.
        delivered = socket.bytesToWrite() == 0 or socket.waitForBytesWritten(timeout_ms)
        socket.disconnectFromServer()
        return delivered
    finally:
        socket.abort()
        if temporary:
            del application


class AppLinks(QObject):
    """Receives ``iopenpod://`` links for the running app."""

    received = Signal(str)

    def __init__(
        self, parent: QObject | None = None, *, name: str | None = None
    ) -> None:
        super().__init__(parent)
        self._name = name or server_name()
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept)
        self._sockets: set[QLocalSocket] = set()

    def listen(self) -> bool:
        if self._server.listen(self._name):
            return True
        # A crashed iOpenPod can leave its socket file behind on macOS and Linux.
        QLocalServer.removeServer(self._name)
        if self._server.listen(self._name):
            return True
        logger.warning(
            "Could not listen for iopenpod:// links: %s", self._server.errorString()
        )
        return False

    def close(self) -> None:
        """Stop listening and release every connection now, not at collection."""

        for socket in list(self._sockets):
            self._release(socket)
        self._server.close()

    def deliver(self, link: str) -> None:
        if is_app_link(link):
            self.received.emit(link)

    def _accept(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            self._sockets.add(socket)
            socket.readyRead.connect(self._ready)
            socket.disconnected.connect(self._gone)
            self._read(socket)

    def _ready(self) -> None:
        socket = self.sender()
        if isinstance(socket, QLocalSocket):
            self._read(socket)

    def _gone(self) -> None:
        socket = self.sender()
        if isinstance(socket, QLocalSocket):
            self._read(socket)  # a link may arrive together with the hang-up
            self._release(socket)

    def _release(self, socket: QLocalSocket) -> None:
        if socket not in self._sockets:
            return
        self._sockets.discard(socket)
        socket.readyRead.disconnect(self._ready)
        socket.disconnected.disconnect(self._gone)
        socket.abort()
        socket.deleteLater()

    def _read(self, socket: QLocalSocket) -> None:
        while socket.canReadLine():
            line = bytes(socket.readLine().data()).decode("utf-8", "replace").strip()
            self.deliver(line)
        if socket.bytesAvailable() > _MAX_LINK:
            socket.abort()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.FileOpen and isinstance(event, QFileOpenEvent):
            url = event.url().toString()
            if is_app_link(url):
                self.deliver(url)
                return True
        return super().eventFilter(watched, event)


def launch_command() -> list[str]:
    """How the operating system should start iOpenPod for a link."""

    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "iOpenPod"]


def register_scheme() -> None:
    """Make this installation the handler for iopenpod:// for the current user."""

    try:
        if sys.platform == "win32":
            if _windows_package():
                return  # the Store package's manifest declares the protocol
            _register_windows(launch_command())
        elif sys.platform.startswith("linux"):
            _register_linux(launch_command())
        # macOS: the app bundle's Info.plist declares the scheme.
    except Exception:
        logger.warning("Could not register iopenpod:// links", exc_info=True)


def _windows_package() -> bool:
    return "WindowsApps" in Path(sys.executable).parts


def _quote_windows(argument: str) -> str:
    return '"' + argument.replace('"', '\\"') + '"'


def _register_windows(command: list[str]) -> None:
    # Windows-only module; typed loosely so other platforms type-check too.
    winreg: Any = importlib.import_module("winreg")

    line = " ".join(_quote_windows(part) for part in command) + ' "%1"'
    root = r"Software\Classes\iopenpod"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "URL:iOpenPod sign-in")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(
        winreg.HKEY_CURRENT_USER, root + r"\shell\open\command"
    ) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, line)


def _quote_desktop(argument: str) -> str:
    escaped = (
        argument.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
    )
    return f'"{escaped}"'.replace("%", "%%")


def desktop_entry(command: list[str]) -> str:
    executable = " ".join(_quote_desktop(part) for part in command)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=iOpenPod sign-in\n"
        "NoDisplay=true\n"
        f"Exec={executable} %u\n"
        f"MimeType=x-scheme-handler/{SCHEME};\n"
    )


def _register_linux(command: list[str]) -> None:
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    applications = data / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    entry = applications / _DESKTOP_ID
    content = desktop_entry(command)
    try:
        current = entry.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if current != content:
        temporary = entry.with_name(f".{entry.name}.part")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(entry)
    tool = shutil.which("xdg-mime")
    if tool is not None:
        subprocess.run(
            [tool, "default", _DESKTOP_ID, f"x-scheme-handler/{SCHEME}"],
            check=False,
            capture_output=True,
            timeout=10,
        )


__all__ = [
    "SCHEME",
    "AppLinks",
    "desktop_entry",
    "forward",
    "is_app_link",
    "launch_command",
    "link_from",
    "register_scheme",
    "server_name",
]
