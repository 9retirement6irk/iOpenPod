"""Capture this fork's Connected Sources screens for the README.

Run with ``uv run python -m scripts.capture_fork_screenshots``. Like
``capture_store_screenshots``, it renders the real application widgets offscreen
with fictional sample data: no servers, devices, settings, keychain or private
media are touched. Plex, Grimmory, Immich and Memos content is invented, and the
artwork is generated from geometric primitives.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import math
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT = 1440, 1000

PLEX_ALBUMS = (
    ("Amber Horizon", "The Sample Ensemble", "Ambient", 2019),
    ("Blue Hour", "The Sample Ensemble", "Electronic", 2021),
    ("Coastal Lines", "Studio Example", "Instrumental", 2017),
    ("Daylight Studies", "Studio Example", "Electronic", 2022),
    ("Echo Garden", "Demo Collective", "Ambient", 2018),
    ("Faraway Forms", "Demo Collective", "Instrumental", 2020),
    ("Golden Islands", "The Sample Ensemble", "Jazz", 2016),
    ("Harbor Lights", "Studio Example", "Electronic", 2023),
    ("Into the Quiet", "Demo Collective", "Ambient", 2015),
    ("Juniper Morning", "The Sample Ensemble", "Instrumental", 2024),
    ("Kinetic Shapes", "Studio Example", "Electronic", 2021),
    ("Late Summer", "Demo Collective", "Jazz", 2019),
)
SHOWS = (("Coastal Kitchen", 2, 6), ("The Night Garden", 1, 8))
MOVIES = ("Paper Planets", "Quiet Architecture", "River Patterns", "Soft Signals")
BOOKS = (
    ("The Lighthouse Keeper", "A. Example", 6),
    ("Maps of Quiet Places", "B. Sample", 4),
    ("A Year of Small Things", "C. Demo", 9),
)


TITLES = (
    "First Light",
    "A Place to Begin",
    "Open Water",
    "Slow Motion",
    "In the Distance",
    "Between the Lines",
    "Small Discoveries",
    "Parallel Paths",
    "Still Moving",
    "The Long Way Home",
    "After the Rain",
    "Until Tomorrow",
)

_PALETTE = (
    ("#fbc278", "#78415c"),
    ("#64d5de", "#203461"),
    ("#bce1d3", "#266675"),
    ("#f3db77", "#dd8362"),
    ("#adbd81", "#294d54"),
    ("#eabdce", "#634991"),
    ("#edd9a1", "#ac743b"),
    ("#9bcece", "#304e70"),
    ("#f0c5a1", "#675078"),
    ("#c4d9a4", "#466b57"),
    ("#efbd92", "#b75d58"),
    ("#f2cd87", "#5c8b86"),
)


def illustration(index: int, *, landscape: bool = False) -> Image.Image:
    """Produce original, repeatable sample artwork without external source images."""
    pale, dark = _PALETTE[index % len(_PALETTE)]
    width, height = (960, 640) if landscape else (640, 640)
    canvas = Image.new("RGB", (width, height), pale)
    draw = ImageDraw.Draw(canvas)
    if landscape:
        draw.ellipse(
            (width * 0.66, 54, width * 0.82, 54 + width * 0.16), fill="#fff0ca"
        )
        for layer in range(5):
            points = [(0, height)]
            points.extend(
                (x, int(235 + layer * 70 + math.sin(x / 145 + index + layer) * 58))
                for x in range(0, width + 1, 8)
            )
            points.append((width, height))
            color = tuple(
                max(0, value - layer * 13) for value in ImageColor.getrgb(dark)
            )
            draw.polygon(points, fill=color)
    elif index % 4 == 0:
        for radius in range(400, 50, -45):
            draw.ellipse(
                (320 - radius, 320 - radius, 320 + radius, 320 + radius),
                outline=dark,
                width=16,
            )
        draw.rectangle((0, 430, 640, 640), fill=dark)
    elif index % 4 == 1:
        for step in range(9):
            x = 25 + step * 78
            draw.rounded_rectangle(
                (x, -120 + step * 47, x + 34, 485 + step * 47), radius=17, fill=dark
            )
    elif index % 4 == 2:
        for step in range(5):
            draw.polygon(
                [
                    (0, 200 + step * 100),
                    (320, 30 + step * 100),
                    (640, 230 + step * 100),
                    (640, 640),
                    (0, 640),
                ],
                fill=dark if step % 2 == 0 else pale,
            )
    else:
        for row in range(4):
            for col in range(4):
                x, y = 45 + col * 145, 45 + row * 145
                draw.pieslice(
                    (x, y, x + 116, y + 116), 0 + row * 90, 270 + row * 90, fill=dark
                )
    return canvas


def _jpeg(image: Image.Image, path: Path) -> tuple[int, int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=88)
    path.write_bytes(buffer.getvalue())
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, hashlib.sha256(buffer.getvalue()).hexdigest()


def remote_catalog(root: Path) -> object:
    """Fictional Plex music and video and Grimmory audiobooks, with covers."""

    from iOpenPod.app.remote_media import (
        RemoteCatalog,
        RemoteCover,
        RemoteItem,
        RemotePlaylist,
        revision_time_ns,
    )
    from iPodDB.library import MediaType
    from storage import HostPath

    def cover(index: int, path: Path, *, landscape: bool = False) -> RemoteCover:
        art = illustration(index, landscape=landscape)
        if landscape:
            art = art.crop((160, 0, 800, 640))
        size, modified, digest = _jpeg(art, path)
        return RemoteCover(HostPath(path), size, modified, digest)

    def remote(key: str, path: Path, title: str, **details: object) -> RemoteItem:
        revision = f"rev-{key}"
        return RemoteItem(
            key,
            HostPath(path),
            revision,
            6_000_000,
            revision_time_ns(revision),
            False,
            title,
            **details,  # type: ignore[arg-type]
        )

    plex = root / "Plex"
    items: list[RemoteItem] = []
    for index, (album, artist, genre, year) in enumerate(PLEX_ALBUMS):
        folder = plex / artist / album
        art = cover(index, folder / "cover.jpg")
        for number, title in enumerate(TITLES[:10], 1):
            items.append(
                remote(
                    f"plex:srv:{index}{number:02d}",
                    folder / f"{number:02d} {title} [{index}{number:02d}].flac",
                    title,
                    artist=artist,
                    album=album,
                    album_artist=artist,
                    genre=genre,
                    year=year,
                    track_number=number,
                    total_tracks=10,
                    length_ms=(180 + number * 11) * 1000,
                    cover=art,
                )
            )
    for show_index, (show, seasons, episodes) in enumerate(SHOWS):
        for season in range(1, seasons + 1):
            folder = plex / "TV Shows" / show / f"Season {season:02d}"
            art = cover(
                13 + show_index * 3 + season, folder / "cover.jpg", landscape=True
            )
            for episode in range(1, episodes + 1):
                title = TITLES[(episode + season) % len(TITLES)]
                items.append(
                    remote(
                        f"plex:srv:tv{show_index}{season}{episode:02d}",
                        folder / f"S{season:02d}E{episode:02d} - {title}.mkv",
                        title,
                        artist=show,
                        album=f"Season {season}",
                        album_artist=show,
                        track_number=episode,
                        length_ms=1_440_000,
                        media_type=MediaType.TV_SHOW,
                        show=show,
                        season_number=season,
                        episode_number=episode,
                        cover=art,
                    )
                )
    for index, title in enumerate(MOVIES):
        folder = plex / "Movies" / f"{title} [m{index}]"
        items.append(
            remote(
                f"plex:srv:m{index}",
                folder / f"{title}.mkv",
                title,
                year=2018 + index,
                genre="Drama",
                length_ms=6_300_000,
                media_type=MediaType.VIDEO,
                cover=cover(16 + index, folder / "cover.jpg"),
            )
        )
    books = root / "Audiobooks"
    for index, (title, author, parts) in enumerate(BOOKS):
        folder = books / author / title
        art = cover(20 + index, folder / "cover.jpg")
        items.extend(
            remote(
                f"grimmory:{index}:{part}",
                folder / f"{part:02d} {title}.m4b",
                f"{title}, Part {part}",
                artist=author,
                album=title,
                album_artist=author,
                genre="Audiobook",
                track_number=part,
                total_tracks=parts,
                length_ms=2_700_000,
                media_type=MediaType.AUDIOBOOK,
                cover=art,
            )
            for part in range(1, parts + 1)
        )
    mix = RemotePlaylist(
        HostPath(plex / "Playlists" / "Road Trip.m3u8"),
        "Road Trip",
        tuple(item.path for item in items[:120:7]),
    )
    return RemoteCatalog(tuple(items), (), (mix,))


def _webp(source: Path, target: Path) -> None:
    with Image.open(source) as image:
        image.convert("RGB").save(target, "WEBP", quality=82, method=6)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/screenshots/fork")
    args = parser.parse_args()
    scratch = Path(tempfile.mkdtemp(prefix="iopenpod-fork-shots-"))
    # Keep every setting, cache and Connected file away from the real home folder.
    for name in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    ):
        os.environ[name] = str(scratch / name.lower())
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ.setdefault("QT_SCALE_FACTOR", "2")

    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QScrollArea, QStackedWidget, QTabWidget
    from scripts.capture_store_screenshots import (
        child,
        navigate,
        sample_ipod,
        sample_library,
        settle,
    )
    from tests.iOpenPod.GUI.application_shell_test_support import (
        APPLICATION,
        build_context,
    )

    from iOpenPod.app.connected.config import (
        ConnectedConfigStore,
        ImmichPick,
        ImmichPickKind,
        PlexVideoSection,
        PlexVideoUse,
    )
    from iOpenPod.app.connected.immich import ImmichCollection
    from iOpenPod.app.connected.memos import Memo
    from iOpenPod.app.connected.plex import PlexSection
    from iOpenPod.app.connected.secrets import MemorySecretStore, SecretKey
    from iOpenPod.app.core.settings.definitions import (
        APPEARANCE_MODE,
        COLORFUL_MODE,
        AppearanceMode,
    )
    from iOpenPod.app.host_media_library import HostMediaCacheStats, HostMediaLibrary
    from iOpenPod.app.library_sync_helper import IPodMediaCacheStats, IPodMediaLibrary
    from iOpenPod.app.remote_media import RemoteCatalog, merge_remote
    from iOpenPod.app.sync_plan import (
        SyncPlan,
        SyncPlanAction,
        SyncPlanBasis,
        SyncPlanItem,
        SyncPlanMediaKind,
    )
    from iOpenPod.GUI.main_window import MainWindow
    from iOpenPod.GUI.navigation import PageId
    from iOpenPod.GUI.pages.connected_pages import GrimmoryPage, PlexPage
    from iOpenPod.GUI.sync_connected_pages import (
        ContactsSyncPage,
        ImmichSyncPage,
        NotesSyncPage,
    )
    from iOpenPod.GUI.sync_workspace import SyncWorkspace
    from iOpenPod.GUI.widgets.media_cache_settings import MediaCacheSettings
    from iOpenPod.GUI.widgets.sidebar import Sidebar
    from iPodDB.library import LibrarySnapshot
    from storage import Storage

    APPLICATION.setFont(QFont("Inter", 10))
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)
    context = build_context()
    context.settings.set_global(APPEARANCE_MODE, AppearanceMode.DARK.value)
    context.settings.set_global(COLORFUL_MODE, True)
    library = sample_library()
    active = sample_ipod(library)
    # The Connected Sources configuration is written before the window reads it.
    store = ConnectedConfigStore(
        Storage().host_config_file("iOpenPod", "connected-sources.json")
    )
    secrets = MemorySecretStore()

    # Signed in to fictional servers. No request is made: the jobs that would
    # read them are replaced, and their results are handed to the pages.
    secrets.save(
        SecretKey.PLEX,
        {"server_token": "x", "server_uri": "http://plex.example", "server_id": "srv"},
    )
    secrets.save(SecretKey.IMMICH, {"api_key": "x"})
    secrets.save(SecretKey.MEMOS, {"token": "x"})
    secrets.save(SecretKey.GRIMMORY, {"access": "x", "refresh": "x"})
    config = store.load()
    store.save(
        replace(
            config,
            plex=replace(
                config.plex,
                server_id="srv",
                server_name="Living Room Plex",
                username="sample-user",
                skipped_sections=("2",),
                video_sections=(
                    PlexVideoSection("3", PlexVideoUse.MOVIES),
                    PlexVideoSection("5", PlexVideoUse.TV_SHOWS),
                ),
            ),
            immich=replace(
                config.immich,
                url="https://photos.example",
                picks=(
                    ImmichPick(ImmichPickKind.FAVORITES, "favorites", "Favorites"),
                    ImmichPick(ImmichPickKind.ALBUM, "a2", "Summer by the Sea"),
                    ImmichPick(ImmichPickKind.PERSON, "p1", "Sam"),
                ),
            ),
            memos=replace(
                config.memos,
                url="https://memos.example",
                tags=("recipes", "travel"),
                skipped_memos=("memos/5",),
            ),
            grimmory=replace(
                config.grimmory,
                url="https://books.example",
                username="sample-user",
                sync_progress=True,
            ),
        )
    )
    window = MainWindow(context, auto_discover=False)
    connected = window._connected  # pyright: ignore[reportPrivateUsage]
    connected._secrets = secrets  # pyright: ignore[reportPrivateUsage]
    for name in ("plex_sections", "list_immich", "list_memos", "grimmory_libraries"):
        if hasattr(connected, name):
            setattr(connected, name, lambda *_a, **_k: True)
    contacts = scratch / "Family.vcf"
    contacts.write_text(
        "".join(
            f"BEGIN:VCARD\nVERSION:3.0\nUID:{name.lower().replace(' ', '-')}\nFN:{name}\n"
            f"TEL;TYPE=cell:+1 555 01{index:02d}\nEND:VCARD\n"
            for index, name in enumerate(
                (
                    "Alex Rivera",
                    "Grandma Rose",
                    "Jordan Lee",
                    "Morgan Ellis",
                    "Priya Shah",
                    "Sam Taylor",
                    "Dr. Chen (Dentist)",
                )
            )
        )
    )
    connected.add_contacts_file(contacts)
    work = scratch / "Work.vcf"
    work.write_text(
        "BEGIN:VCARD\nVERSION:3.0\nUID:w1\nFN:Casey Morgan\nEND:VCARD\n"
        "BEGIN:VCARD\nVERSION:3.0\nUID:w2\nFN:Front Desk\nEND:VCARD\n"
    )
    connected.add_contacts_file(work)
    connected.set_contacts_included(("w2",), False)

    window.resize(WIDTH, HEIGHT)
    context.library_workspace.load(
        library,
        photo_album_creation_type=active.profile.capabilities.artwork.photo_album_creation_type,
    )
    context.track_model.reset_tracks(library.tracks)
    child(window, Sidebar).set_active_ipod(active)
    context.status.show(
        "fork-screenshot", "Sample library · Fictional media and servers"
    )
    window.show()
    captured: list[str] = []

    def capture(name: str) -> None:
        settle(APPLICATION)
        settle(APPLICATION)
        png = scratch / f"{name}.png"
        if not window.grab().save(str(png), "PNG"):
            raise RuntimeError(f"Could not save {png}")
        _webp(png, output / f"{name}.webp")
        captured.append(name)

    try:
        # --- Sync with Host: Plex and Grimmory beside the computer's music ------------
        catalog = remote_catalog(scratch / "Connected")
        assert isinstance(catalog, RemoteCatalog)
        host = merge_remote(
            HostMediaLibrary(LibrarySnapshot(), (), (), HostMediaCacheStats()), catalog
        )
        items = tuple(
            SyncPlanItem(
                SyncPlanAction.ADD,
                SyncPlanMediaKind.TRACK,
                SyncPlanBasis.HOST_ONLY,
                track.title,
                f"{track.artist} · {track.album}",
                track.metadata.location,
            )
            for track in host.snapshot.tracks
        )
        workspace = child(window, SyncWorkspace)
        workspace.load_comparison(
            host,
            SyncPlan(items),
            active,
            IPodMediaLibrary((), (), (), IPodMediaCacheStats(), None, False),
        )
        workspace.set_available_pages(workspace.library_page_ids)
        chosen = [
            t.track_id
            for t in host.snapshot.tracks
            if t.album in {"Blue Hour", "Echo Garden", "Harbor Lights", "Late Summer"}
            or t.show == "Coastal Kitchen"
            or t.album == "The Lighthouse Keeper"
        ]
        workspace.selection.set_tracks_checked(chosen, True)
        workspace.set_extras_pending(True)
        workspace.show_selection()
        child(window, QStackedWidget, "workspaceStack").setCurrentWidget(workspace)
        capture("sync-plex-music")

        workspace._show_library_page(PageId.TV_SHOWS.value)  # pyright: ignore[reportPrivateUsage]
        capture("sync-plex-tv")
        workspace._show_library_page(PageId.AUDIOBOOKS.value)  # pyright: ignore[reportPrivateUsage]
        capture("sync-grimmory-audiobooks")

        # --- Sync with Host: the CONNECTED checklists --------------------------------
        immich = child(workspace, ImmichSyncPage)
        workspace._show_library_page(PageId.IMMICH.value)  # pyright: ignore[reportPrivateUsage]
        settle(APPLICATION)
        immich._waiting = False  # pyright: ignore[reportPrivateUsage]
        immich.loaded(
            (
                tuple(
                    ImmichCollection(ImmichPickKind.ALBUM, f"a{i}", title, count)
                    for i, (title, count) in enumerate(
                        (
                            ("Birthday 2026", 84),
                            ("Hiking the Ridge", 143),
                            ("Summer by the Sea", 212),
                            ("Weekend in the City", 66),
                            ("Winter Cabin", 98),
                        ),
                        start=0,
                    )
                ),
                tuple(
                    ImmichCollection(ImmichPickKind.PERSON, f"p{i}", name)
                    for i, name in enumerate(("Alex", "Sam", "Jordan", "Priya"))
                ),
            )
        )
        capture("sync-connected-immich")

        notes = child(workspace, NotesSyncPage)
        workspace._show_library_page(PageId.NOTES.value)  # pyright: ignore[reportPrivateUsage]
        settle(APPLICATION)

        def memo(number: int, title: str, *tags: str) -> Memo:
            return Memo(f"memos/{number}", "", title, tags, title, False, "", "", 0)

        notes._waiting = False  # pyright: ignore[reportPrivateUsage]
        notes.loaded(
            (
                memo(1, "Sunday pancakes", "recipes"),
                memo(2, "Grandma's tomato soup", "recipes", "family"),
                memo(3, "Weeknight curry", "recipes"),
                memo(4, "Lisbon: places to eat", "travel", "recipes"),
                memo(5, "Old packing list", "travel"),
                memo(6, "Kyoto day trips", "travel"),
                memo(7, "Book club picks", "books"),
                memo(8, "Birthday ideas", "family"),
                memo(9, "Standup notes", "work/meetings"),
            )
        )
        for index in range(notes.tree.topLevelItemCount()):
            item = notes.tree.topLevelItem(index)
            if item is not None:
                item.setExpanded(item.text(0).startswith(("#recipes", "#travel")))
        capture("sync-connected-notes")

        contacts_page = child(workspace, ContactsSyncPage)
        workspace._show_library_page(PageId.CONTACTS.value)  # pyright: ignore[reportPrivateUsage]
        settle(APPLICATION)
        contacts_page.tree.expandAll()
        capture("sync-connected-contacts")

        # --- Connected pages: setup ---------------------------------------------------
        child(window, QStackedWidget, "workspaceStack").setCurrentIndex(0)
        navigate(window, PageId.PLEX)
        plex = child(window, PlexPage)
        plex._show_libraries(  # pyright: ignore[reportPrivateUsage]
            (
                PlexSection("1", "Music", "artist"),
                PlexSection("2", "Audiobooks", "artist"),
                PlexSection("3", "Movies", "movie"),
                PlexSection("4", "Home Videos", "movie"),
                PlexSection("5", "TV Shows", "show"),
            )
        )
        plex.say("")
        capture("connected-plex")

        navigate(window, PageId.GRIMMORY)
        child(window, GrimmoryPage).say("")
        capture("connected-grimmory")

        navigate(window, PageId.SETTINGS)
        tabs = child(window, QTabWidget, "settingsTabs")
        tabs.setCurrentIndex(
            next(i for i in range(tabs.count()) if tabs.tabText(i) == "Sync")
        )
        cache = child(window, MediaCacheSettings)
        # Show the default location as people see it, not this run's temporary home.
        cache._folder_row.set_copy(  # pyright: ignore[reportPrivateUsage]
            "Cache folder", "~/Music/iOpenPod Connected"
        )
        area = cache.parentWidget()
        while area is not None and not isinstance(area, QScrollArea):
            area = area.parentWidget()
        if isinstance(area, QScrollArea):
            area.ensureWidgetVisible(cache, 0, 40)
            bar = area.verticalScrollBar()
            bar.setValue(bar.value() + cache.height())
        capture("settings-sync")
    finally:
        window.close()
        context.shutdown()
        APPLICATION.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    print(f"Captured {len(captured)} screenshots in {output}: {', '.join(captured)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
