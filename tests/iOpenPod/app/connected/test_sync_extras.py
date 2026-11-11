"""Sync with Host sends the ticked notes, calendars and contacts after the media."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

from tests.iOpenPod.app.test_library_write_controller import wait_for
from tests.iOpenPod.GUI.application_shell_test_support import APPLICATION

from iOpenPod.app.connected.config import ConnectedConfigStore
from iOpenPod.app.connected.memos import Memo
from iOpenPod.app.connected.secrets import MemorySecretStore
from iOpenPod.app.connected_controller import ConnectedController, ExtrasSent
from iOpenPod.app.services.ipod_extras import ExtrasResult, ExtrasSection
from storage import AtomicHostFile

CALENDAR = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-WR-CALNAME:{name}\r\n"
    "BEGIN:VEVENT\r\nUID:1\r\nDTSTART:20991201T100000Z\r\n"
    "DTEND:20991201T110000Z\r\nSUMMARY:Later\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)
CARDS = (
    "BEGIN:VCARD\nVERSION:3.0\nUID:a\nFN:Ada\nEND:VCARD\n"
    "BEGIN:VCARD\nVERSION:3.0\nUID:g\nFN:Grace\nEND:VCARD\n"
)


def memo(name: str, title: str, *tags: str) -> Memo:
    return Memo(name, "", f"{title}\nbody", tags, title, False, "", "", 0)


class FakeMemos:
    def memos(self, **_options: object) -> tuple[Memo, ...]:
        return (
            memo("memos/1", "Plan", "work"),
            memo("memos/2", "Standup", "work/meetings"),
            memo("memos/3", "Shopping", "home"),
        )

    def current_user(self) -> None:
        return None


def controller(tmp_path: Path) -> tuple[ConnectedController, MagicMock]:
    coordinator = MagicMock()
    coordinator.count_foreign_extras.return_value = 0
    coordinator.publish_extras.side_effect = lambda _a, _s, files, _c: ExtrasResult(
        len(files), 0, 0
    )
    store = ConnectedConfigStore(AtomicHostFile(tmp_path / "connected.json"))
    made = ConnectedController(
        MagicMock(), store, MagicMock(), coordinator, secrets=MemorySecretStore()
    )
    return made, coordinator


def written(coordinator: MagicMock) -> dict[ExtrasSection, list[str]]:
    return {
        call.args[1]: sorted(f.relative for f in call.args[2])
        for call in coordinator.publish_extras.call_args_list
    }


def test_nothing_set_up_sends_nothing(tmp_path: Path) -> None:
    made, coordinator = controller(tmp_path)
    try:
        assert not made.has_extras
        assert not made.publish_for_sync()
        coordinator.publish_extras.assert_not_called()
    finally:
        made.shutdown()


def test_ticked_notes_calendars_and_contacts_are_sent(tmp_path: Path) -> None:
    made, coordinator = controller(tmp_path)
    results: list[tuple[str, object]] = []
    made.result.connect(lambda op, value: results.append((op, value)))
    try:
        for name in ("Work", "Family"):
            path = tmp_path / f"{name}.ics"
            path.write_text(CALENDAR.format(name=name))
            made.add_calendar_file(path)
        cards = tmp_path / "People.vcf"
        cards.write_text(CARDS)
        made.add_contacts_file(cards)
        made.update_config(
            replace(
                made.config,
                memos=replace(made.config.memos, url="https://memos.example"),
            )
        )
        made.memos = FakeMemos  # type: ignore[method-assign,assignment]

        # Sync's checklists: a tag, one memo left out, a calendar and a contact.
        made.set_memo_tag("work", True)
        made.set_memo_tag("home", True)
        made.set_memo_tag("home", False)
        made.set_memos_included(("memos/2",), False)
        work, family = made.calendar_choices()
        assert work == ("file:Work.ics", "Work")
        made.set_calendar_included(family[0], False)
        made.set_contacts_included(("g",), False)
        assert made.config.memos.tags == ("work",)
        assert made.has_extras

        assert made.publish_for_sync()
        wait_for(lambda: not made.busy)
        APPLICATION.processEvents()
        assert written(coordinator) == {
            ExtrasSection.NOTES: ["Plan.txt"],
            ExtrasSection.CALENDARS: ["Work.ics"],
            ExtrasSection.CONTACTS: ["Ada.vcf"],
        }
        assert results[-1] == ("extras-publish", ExtrasSent(1, 1, 1))

        # One kind failing doesn't stop the others, and is reported by name.
        coordinator.publish_extras.reset_mock()
        normal = coordinator.publish_extras.return_value

        def flaky(_active: object, section: ExtrasSection, *_rest: object) -> object:
            if section is ExtrasSection.CALENDARS:
                raise RuntimeError("the iPod Library changed")
            return normal

        coordinator.publish_extras.side_effect = flaky
        assert made.publish_for_sync()
        wait_for(lambda: not made.busy)
        APPLICATION.processEvents()
        assert set(written(coordinator)) == {
            ExtrasSection.NOTES,
            ExtrasSection.CALENDARS,
            ExtrasSection.CONTACTS,
        }
        operation, sent = results[-1]
        assert operation == "extras-publish" and isinstance(sent, ExtrasSent)
        assert (sent.notes, sent.calendars, sent.contacts) == (1, 0, 1)
        assert sent.issues == ("Calendars: the iPod Library changed",)
        coordinator.publish_extras.side_effect = None

        # Ticking them again brings them back.
        coordinator.publish_extras.reset_mock()
        made.set_memos_included(("memos/2",), True)
        made.set_calendar_included(family[0], True)
        made.set_contact_included("g", True)
        assert made.publish_for_sync()
        wait_for(lambda: not made.busy)
        APPLICATION.processEvents()
        assert written(coordinator) == {
            ExtrasSection.NOTES: ["Plan.txt", "Standup.txt"],
            ExtrasSection.CALENDARS: ["Family.ics", "Work.ics"],
            ExtrasSection.CONTACTS: ["Ada.vcf", "Grace.vcf"],
        }
    finally:
        made.shutdown()
