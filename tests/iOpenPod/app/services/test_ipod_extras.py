"""iPod Extras publish through Storage and never touch files iOpenPod did not write."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from iOpenPod.app.services.ipod_extras import (
    MANIFEST_PATH,
    ExtrasFile,
    ExtrasSection,
    count_foreign_files,
    prepare_extras,
    read_manifest,
    remove_empty_folders,
)
from storage import AccessMode, FilesystemSession, Storage
from storage.testing import VirtualStoragePlatform


@contextmanager
def volume(tmp_path: Path) -> Generator[tuple[FilesystemSession, Path]]:
    root = tmp_path / "device"
    (root / "iPod_Control").mkdir(parents=True)
    platform = VirtualStoragePlatform()
    platform.add_volume(root)
    storage = Storage(platform, transaction_recovery_directory=tmp_path / "recovery")
    with storage.open_session(
        storage.discover().volumes[0], access=AccessMode.READ_WRITE
    ) as session:
        yield session, root


def publish(
    session: FilesystemSession, section: ExtrasSection, files: tuple[ExtrasFile, ...]
) -> tuple[int, int, int, tuple[str, ...]]:
    prepared = prepare_extras(session, section, files, checkpoint=lambda: None)
    if prepared.transaction is not None:
        for directory in prepared.directories:
            session.create_directory(directory)
        committed = session.execute_transaction(
            prepared.transaction, checkpoint=lambda: None
        )
        session.finalize_committed_transaction(committed.recovery.journal_path)
        remove_empty_folders(session, prepared.emptied)
    return (
        prepared.written,
        prepared.unchanged,
        prepared.removed,
        tuple(prepared.issues),
    )


def test_publish_update_and_remove_owned_notes(tmp_path: Path) -> None:
    with volume(tmp_path) as (session, root):
        (root / "notes").mkdir()  # existing folder in another capitalization is reused
        (root / "notes" / "Mine.txt").write_bytes(b"the person's note")
        written = publish(
            session,
            ExtrasSection.NOTES,
            (
                ExtrasFile("Memos/work/Standup.txt", b"one"),
                ExtrasFile("Memos/home/Groceries.txt", b"two"),
            ),
        )
        assert written == (2, 0, 0, ())
        assert (
            root / "notes" / "Memos" / "work" / "Standup.txt"
        ).read_bytes() == b"one"
        assert count_foreign_files(session, ExtrasSection.NOTES) == 1
        owned = read_manifest(session)["notes"]
        assert set(owned) == {
            "notes/Memos/work/Standup.txt",
            "notes/Memos/home/Groceries.txt",
        }

        again = publish(
            session,
            ExtrasSection.NOTES,
            (ExtrasFile("Memos/work/Standup.txt", b"one updated"),),
        )
        assert again == (1, 0, 1, ())
        assert not (
            root / "notes" / "Memos" / "home"
        ).exists()  # emptied folder removed
        assert (root / "notes" / "Mine.txt").read_bytes() == b"the person's note"

        (root / "notes" / "Memos" / "work" / "Standup.txt").write_bytes(
            b"edited on device"
        )
        final = publish(session, ExtrasSection.NOTES, ())
        assert final[2] == 0 and "left alone" in final[3][0]
        assert (root / "notes" / "Memos" / "work" / "Standup.txt").exists()


def test_foreign_files_are_never_overwritten_and_sections_are_independent(
    tmp_path: Path,
) -> None:
    with volume(tmp_path) as (session, root):
        (root / "Calendars").mkdir()
        (root / "Calendars" / "Work.ics").write_bytes(b"theirs")
        result = publish(
            session,
            ExtrasSection.CALENDARS,
            (
                ExtrasFile("Work.ics", b"ours"),
                ExtrasFile("Home.ics", b"home"),
            ),
        )
        assert result[0] == 1 and "already uses this name" in result[3][0]
        assert (root / "Calendars" / "Work.ics").read_bytes() == b"theirs"
        publish(session, ExtrasSection.NOTES, (ExtrasFile("Memos/a.txt", b"a"),))
        unchanged = publish(
            session, ExtrasSection.CALENDARS, (ExtrasFile("Home.ics", b"home"),)
        )
        assert unchanged == (0, 1, 0, ())
        assert set(read_manifest(session)) == {"calendars", "notes"}
        assert session.exists(MANIFEST_PATH)
