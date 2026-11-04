"""Contacts for the iPod's Extras → Contacts, from uploaded vCard files.

The iPod reads vCard files from the ``Contacts`` folder at the Volume root and
understands vCard 2.1 and 3.0. Uploaded files may hold many contacts each, in any
version; every contact becomes its own ``.vcf`` file so one malformed card cannot
hide the rest. Photos, logos and sounds are dropped (the iPod cannot show them and
they make cards large), vCard 4.0 cards are labelled 3.0, whose common fields are
the same, and line endings become CRLF as the format requires.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .http import ConnectedError
from .materialize import safe_component

if TYPE_CHECKING:
    from collections.abc import Iterable

MAX_CONTACTS_BYTES = 20 * 1024 * 1024
MAX_CONTACTS = 5_000
_DROPPED = frozenset({"PHOTO", "LOGO", "SOUND", "KEY"})
_CARD = re.compile(r"BEGIN:VCARD\r?\n(.*?)\r?\nEND:VCARD", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True, slots=True)
class Contact:
    key: str  # stable while the card is unchanged: its UID, else a digest
    name: str
    text: str  # cleaned, CRLF-terminated vCard


@dataclass(frozen=True, slots=True)
class ContactFile:
    name: str  # what the person sees
    relative: str  # "/"-separated, beneath the Contacts folder
    data: bytes


def _unfold(lines: list[str]) -> list[str]:
    """Join folded lines (continuations start with a space or tab)."""

    logical: list[str] = []
    for line in lines:
        if line[:1] in {" ", "\t"} and logical:
            logical[-1] += line[1:]
        elif line.strip():
            logical.append(line)
    return logical


def _property(line: str) -> tuple[str, str]:
    head, _, value = line.partition(":")
    name = head.split(";", 1)[0].split(".", 1)[-1].strip().upper()
    return name, value


def _decode_name(value: str) -> str:
    return value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()


def parse_contacts(text: str) -> tuple[Contact, ...]:
    """Every card in one uploaded vCard file, cleaned for the iPod."""

    found: list[Contact] = []
    for match in _CARD.finditer(text.replace("\r\n", "\n").replace("\r", "\n")):
        properties = _unfold(match.group(1).split("\n"))
        kept: list[str] = []
        name = uid = ""
        structured = ""
        for line in properties:
            prop, value = _property(line)
            if prop in _DROPPED or prop in {"BEGIN", "END"}:
                continue
            if prop == "VERSION":
                line = "VERSION:3.0" if value.strip() == "4.0" else line
            elif prop == "FN":
                name = _decode_name(value)
            elif prop == "N" and not structured:
                parts = [_decode_name(part) for part in value.split(";")]
                structured = " ".join(p for p in (parts[1:2] + parts[:1]) if p)
            elif prop == "UID":
                uid = value.strip()
            kept.append(line)
        if not any(_property(line)[0] == "VERSION" for line in kept):
            kept.insert(0, "VERSION:3.0")
        card = "\r\n".join(["BEGIN:VCARD", *kept, "END:VCARD"]) + "\r\n"
        key = uid or hashlib.sha256(card.encode("utf-8", "replace")).hexdigest()[:24]
        found.append(Contact(key, name or structured or "Unnamed contact", card))
    return tuple(found)


def check_contacts(text: str) -> int:
    """How many contacts an upload holds; raise when it holds none."""

    count = len(parse_contacts(text))
    if not count:
        raise ConnectedError("That file has no contacts (vCard) in it.")
    return count


def plan_contacts(
    uploads: Iterable[str], skipped: Iterable[str] = ()
) -> tuple[tuple[ContactFile, ...], int]:
    """One file per chosen contact; returns the files and how many were left out."""

    left_out = set(skipped)
    seen: set[str] = set()
    names: set[str] = set()
    files: list[ContactFile] = []
    over = 0
    for text in uploads:
        for contact in parse_contacts(text):
            if contact.key in left_out or contact.key in seen:
                continue
            seen.add(contact.key)
            if len(files) >= MAX_CONTACTS:
                over += 1
                continue
            stem = base = safe_component(contact.name, "Contact", limit=60)
            counter = 2
            while stem.casefold() in names:
                stem, counter = f"{base} ({counter})", counter + 1
            names.add(stem.casefold())
            files.append(
                ContactFile(contact.name, f"{stem}.vcf", contact.text.encode("utf-8"))
            )
    return tuple(files), over


__all__ = [
    "MAX_CONTACTS",
    "MAX_CONTACTS_BYTES",
    "Contact",
    "ContactFile",
    "check_contacts",
    "parse_contacts",
    "plan_contacts",
]
