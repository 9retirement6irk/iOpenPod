"""vCard uploads become one cleaned contact file each for Extras → Contacts."""

import pytest

from iOpenPod.app.connected import contacts
from iOpenPod.app.connected.contacts import (
    check_contacts,
    parse_contacts,
    plan_contacts,
)
from iOpenPod.app.connected.http import ConnectedError

UPLOAD = (
    "BEGIN:VCARD\n"
    "VERSION:4.0\n"
    "UID:urn:uuid:ada\n"
    "FN:Ada Lovelace\n"
    "N:Lovelace;Ada;;;\n"
    "TEL;TYPE=cell:+44 20 7946 0000\n"
    "NOTE:First line that is folded\n"
    "  over two lines\n"
    "PHOTO;ENCODING=b;TYPE=JPEG:AAAA\n"
    " BBBB\n"
    "END:VCARD\n"
    "BEGIN:VCARD\r\n"
    "VERSION:3.0\r\n"
    "N:Hopper;Grace;;;\r\n"
    "item1.EMAIL:grace@example.com\r\n"
    "END:VCARD\r\n"
    "BEGIN:VCARD\n"
    "FN:Ada Lovelace\n"
    "END:VCARD\n"
)


def test_cards_are_split_cleaned_and_named() -> None:
    ada, grace, other = parse_contacts(UPLOAD)
    assert (ada.key, ada.name) == ("urn:uuid:ada", "Ada Lovelace")
    assert ada.text.startswith("BEGIN:VCARD\r\nVERSION:3.0\r\n")
    assert ada.text.endswith("END:VCARD\r\n")
    assert "PHOTO" not in ada.text and "BBBB" not in ada.text
    assert "NOTE:First line that is folded over two lines\r\n" in ada.text
    assert "\n" not in ada.text.replace("\r\n", "")
    # No FN: the structured name, given name first; no UID: a digest of the card.
    assert grace.name == "Grace Hopper" and len(grace.key) == 24
    assert "item1.EMAIL:grace@example.com" in grace.text
    # A card without VERSION gets one.
    assert other.text.startswith("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Ada Lovelace")
    assert parse_contacts(UPLOAD) == (ada, grace, other)  # keys stay the same


def test_uploads_without_cards_are_refused() -> None:
    assert check_contacts(UPLOAD) == 3
    with pytest.raises(ConnectedError):
        check_contacts("just some text")


def test_unticked_and_repeated_contacts_are_left_out() -> None:
    files, over = plan_contacts([UPLOAD, UPLOAD], skipped=["urn:uuid:ada"])
    assert over == 0
    # Grace once (the second upload repeats her), and the other "Ada Lovelace".
    assert [f.relative for f in files] == ["Grace Hopper.vcf", "Ada Lovelace.vcf"]
    files, _over = plan_contacts([UPLOAD])
    assert [f.relative for f in files] == [
        "Ada Lovelace.vcf",
        "Grace Hopper.vcf",
        "Ada Lovelace (2).vcf",
    ]
    assert all(f.data.startswith(b"BEGIN:VCARD\r\n") for f in files)


def test_the_contact_limit_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contacts, "MAX_CONTACTS", 2)
    files, over = plan_contacts([UPLOAD])
    assert (len(files), over) == (2, 1)
