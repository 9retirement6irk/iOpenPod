"""Calendars for the iPod's Extras → Calendars, from uploaded files or links.

The iPod reads iCalendar files from the ``Calendars`` folder at the Volume root.
Old one-off events are left out to keep large calendars responsive on the iPod;
repeating events stay unless their repetition has ended.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .http import ConnectedError, Transport, UrllibTransport

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_CALENDAR_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CalendarInfo:
    name: str
    events: int


@dataclass(frozen=True, slots=True)
class TrimmedCalendar:
    text: str
    kept: int
    dropped: int


def unfold(text: str) -> list[str]:
    """iCalendar lines with RFC 5545 folding undone; blank lines removed."""

    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return [line for line in lines if line.strip()]


def fold(line: str, limit: int = 74) -> str:
    if len(line.encode("utf-8")) <= limit:
        return line
    parts: list[str] = []
    current = ""
    for character in line:
        if len((current + character).encode("utf-8")) > limit:
            parts.append(current)
            current = " " + character
        else:
            current += character
    parts.append(current)
    return "\r\n".join(parts)


def _property(line: str) -> str:
    return line.split(":", 1)[0].split(";", 1)[0].upper()


def calendar_info(text: str) -> CalendarInfo:
    if "BEGIN:VCALENDAR" not in text.upper():
        raise ConnectedError("That isn't an iCalendar (.ics) file.")
    name = ""
    events = 0
    for line in unfold(text):
        if _property(line) == "X-WR-CALNAME" and not name and ":" in line:
            name = line.split(":", 1)[1].strip()
        elif line.strip().upper() == "BEGIN:VEVENT":
            events += 1
    return CalendarInfo(name, events)


def _date(value: str) -> str:
    match = re.search(r"(\d{8})", value or "")
    return match.group(1) if match else ""


def trim_calendar(
    text: str, keep_days: int, *, clock: Callable[[], float] = time.time
) -> TrimmedCalendar:
    """Drop one-off events (and finished repetitions) that ended over ``keep_days`` ago."""

    lines = unfold(text)
    if keep_days <= 0:
        events = sum(line.strip().upper() == "BEGIN:VEVENT" for line in lines)
        return TrimmedCalendar(
            "\r\n".join(fold(line) for line in lines) + "\r\n", events, 0
        )
    cutoff = time.strftime("%Y%m%d", time.localtime(clock() - keep_days * 86400))
    output: list[str] = []
    block: list[str] | None = None
    kept = dropped = 0
    for line in lines:
        upper = line.strip().upper()
        if upper == "BEGIN:VEVENT" and block is None:
            block = [line]
            continue
        if block is None:
            output.append(line)
            continue
        block.append(line)
        if upper != "END:VEVENT":
            continue
        values: dict[str, str] = {}
        for item in block:
            values.setdefault(
                _property(item), item.split(":", 1)[1] if ":" in item else ""
            )
        rule = values.get("RRULE", "")
        if rule:
            until = re.search(r"UNTIL=([0-9TZ]+)", rule.upper())
            end = _date(until.group(1)) if until else ""
        else:
            end = _date(values.get("DTEND", "")) or _date(values.get("DTSTART", ""))
        if end and end < cutoff and "RECURRENCE-ID" not in values:
            dropped += 1
        else:
            output.extend(block)
            kept += 1
        block = None
    return TrimmedCalendar(
        "\r\n".join(fold(line) for line in output) + "\r\n", kept, dropped
    )


def rename_calendar(text: str, name: str) -> str:
    """Set ``X-WR-CALNAME``, the name the iPod and calendar apps show."""

    clean = " ".join(name.split())
    output: list[str] = []
    done = False
    for line in unfold(text):
        if _property(line) == "X-WR-CALNAME":
            if not done:
                output.append(f"X-WR-CALNAME:{clean}")
                done = True
            continue
        output.append(line)
        if not done and line.strip().upper() == "BEGIN:VCALENDAR":
            output.append(f"X-WR-CALNAME:{clean}")
            done = True
    return "\r\n".join(fold(line) for line in output) + "\r\n"


def subscription_url(value: str) -> str:
    url = value.strip()
    if url.casefold().startswith("webcal://"):
        url = "https://" + url[9:]
    if not re.match(r"^https?://", url, re.I):
        raise ConnectedError("A calendar link starts with https:// or webcal://.")
    return url


def fetch_calendar(url: str, transport: Transport | None = None) -> str:
    link = subscription_url(url)
    response = (transport or UrllibTransport()).send(
        "GET",
        link,
        {"Accept": "text/calendar, */*"},
        timeout=60,
        limit=MAX_CALENDAR_BYTES,
    )
    text = response.body.decode("utf-8-sig", errors="replace")
    try:
        calendar_info(text)
    except ConnectedError:
        raise ConnectedError("That link didn't return a calendar (.ics).") from None
    return text


def redact(url: str) -> str:
    """A link safe to show: private calendar addresses embed secret tokens."""

    match = re.match(r"^(?:https?|webcal)://([^/]+)", url.strip(), re.I)
    return f"{match.group(1)}/…" if match else "calendar link"


__all__ = [
    "MAX_CALENDAR_BYTES",
    "CalendarInfo",
    "TrimmedCalendar",
    "calendar_info",
    "fetch_calendar",
    "redact",
    "rename_calendar",
    "subscription_url",
    "trim_calendar",
]
