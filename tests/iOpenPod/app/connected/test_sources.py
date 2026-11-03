"""Immich photos, Memos notes, and calendar files."""

import io
import json
from pathlib import Path
from threading import Event

import pytest
from PIL import Image
from tests.iOpenPod.app.connected.support import FakeTransport, reply

from iOpenPod.app.connected.calendars import (
    calendar_info,
    redact,
    rename_calendar,
    subscription_url,
    trim_calendar,
)
from iOpenPod.app.connected.config import (
    ImmichConfig,
    ImmichPick,
    ImmichPickKind,
    MemosConfig,
    TagMatch,
)
from iOpenPod.app.connected.http import ConnectedError, HttpResponse
from iOpenPod.app.connected.immich import ImmichClient, plan_immich
from iOpenPod.app.connected.materialize import refresh_folder
from iOpenPod.app.connected.memos import (
    NOTE_HEADER,
    MemosClient,
    markdown_to_text,
    only_mine,
    plan_notes,
    render_memo,
    summarize,
)

# --- Immich --------------------------------------------------------------------------


def webp() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 10, 10)).save(output, "WEBP")
    return output.getvalue()


def test_immich_people_favorites_and_jpeg_conversion(tmp_path: Path) -> None:
    base = "http://immich:2283/api"
    page = {
        "assets": {
            "items": [
                {
                    "id": "a1",
                    "type": "IMAGE",
                    "originalFileName": "IMG_1.HEIC",
                    "originalMimeType": "image/heic",
                    "checksum": "c1",
                },
                {"id": "v1", "type": "VIDEO", "originalFileName": "clip.mov"},
                {
                    "id": "a2",
                    "type": "IMAGE",
                    "originalFileName": "IMG_1.jpg",
                    "checksum": "c2",
                },
            ],
            "nextPage": None,
        }
    }
    searched: list[dict[str, object]] = []

    def search(_m: str, _u: str, _h: object, body: bytes | None) -> HttpResponse:
        searched.append(json.loads(body or b"{}"))
        return reply(page)

    transport = FakeTransport(
        {
            f"POST {base}/search/metadata": search,
            f"GET {base}/assets/a1/thumbnail": reply(
                body=webp(), content_type="image/webp"
            ),
            f"GET {base}/assets/a2/thumbnail": reply(
                body=b"\xff\xd8\xffjpeg", content_type="image/jpeg"
            ),
            f"GET {base}/people": {
                "people": [{"id": "p1", "name": "Mom"}, {"id": "p2", "name": ""}],
                "hasNextPage": False,
            },
        }
    )
    client = ImmichClient("http://immich:2283/", "key", transport)
    assert [person.title for person in client.people()] == ["Mom"]
    config = ImmichConfig(
        picks=(
            ImmichPick(ImmichPickKind.PERSON, "p1", "Mom"),
            ImmichPick(ImmichPickKind.FAVORITES, "favorites", "Favorites"),
        )
    )
    items = plan_immich(client, config, cancelled=Event())
    assert searched[0]["personIds"] == ["p1"] and searched[1]["isFavorite"] is True
    names = [item.relative for item in items]
    assert names[:2] == ["People - Mom/IMG_1.jpg", "People - Mom/IMG_1_a2.jpg"]
    refresh_folder(tmp_path, items, cancelled=Event())
    assert (tmp_path / "People - Mom" / "IMG_1.jpg").read_bytes()[:3] == b"\xff\xd8\xff"
    assert all(h.get("x-api-key") == "key" for _m, _u, h in transport.calls)


def test_immich_missing_permission_is_named() -> None:
    transport = FakeTransport(
        {"GET http://i/api/people": ConnectedError("HTTP 403", 403)}
    )
    with pytest.raises(ConnectedError, match=r"person\.read"):
        ImmichClient("http://i", "k", transport).people()


# --- Memos ---------------------------------------------------------------------------


def memo(
    name: str,
    content: str,
    tags: list[str],
    *,
    creator: str = "users/1",
    pinned: bool = False,
    updated: str = "2026-10-01T00:00:00Z",
) -> dict[str, object]:
    return {
        "name": name,
        "creator": creator,
        "content": content,
        "tags": tags,
        "pinned": pinned,
        "updateTime": updated,
        "state": "NORMAL",
    }


def test_memos_26_10_api_and_legacy_fallback() -> None:
    rows = [
        memo("memos/a", "Groceries\n- milk #home", ["home"]),
        memo("memos/b", "Other #home", ["home"], creator="users/2"),
    ]
    modern = FakeTransport(
        {
            "GET http://m/api/memos": {"memos": rows, "nextPageToken": ""},
            "GET http://m/api/auth/me": {
                "user": {"name": "users/1", "displayName": "Will"}
            },
        }
    )
    client = MemosClient("http://m/", "memos_pat_x", modern)
    user = client.current_user()
    assert user == ("users/1", "Will")
    mine = only_mine(client.memos(include_archived=False), user)
    assert [m.name for m in mine] == ["memos/a"]
    assert modern.calls[0][2]["Authorization"] == "Bearer memos_pat_x"
    legacy = FakeTransport(
        {
            "GET http://m/api/memos": ConnectedError("HTTP 410", 410),
            "GET http://m/api/v1/memos": {"memos": [], "nextPageToken": ""},
        }
    )
    assert MemosClient("http://m/api/v1", "t", legacy).api() == "http://m/api/v1"
    with pytest.raises(ConnectedError, match="access token"):
        MemosClient(
            "http://m",
            "bad",
            FakeTransport({"GET http://m/api/memos": ConnectedError("x", 401)}),
        ).api()


def test_notes_fit_the_note_reader_and_follow_tags() -> None:
    client = MemosClient(
        "http://m",
        "t",
        FakeTransport(
            {
                "GET http://m/api/memos": {
                    "memos": [
                        memo(
                            "memos/1",
                            "# Trip packing\n"
                            + "\n".join(f"- [ ] item {i} ünï 🎒" for i in range(300))
                            + "\n#travel",
                            ["travel"],
                        ),
                        memo(
                            "memos/2",
                            "Standup <notes>\nSee [doc](https://ex.com)\n#work/standup",
                            ["work/standup"],
                            pinned=True,
                        ),
                        memo(
                            "memos/3",
                            "Groceries\n**milk**\n#home #shop",
                            ["home", "shop"],
                        ),
                        memo("memos/4", "untagged", []),
                    ],
                    "nextPageToken": "",
                }
            }
        ),
    )
    memos = client.memos(include_archived=False)
    summary = summarize(memos, "Will")
    assert {(t.tag, t.count) for t in summary.tags} >= {
        ("work", 1),
        ("work/standup", 1),
    }
    plan = plan_notes(memos, MemosConfig(tags=("work", "travel", "home")))
    paths = [f.relative for f in plan.files]
    assert "Memos/work/Standup \u2039notes\u203a.txt" in paths
    parts = [
        f for f in plan.files if f.relative.startswith("Memos/travel/Trip packing (")
    ]
    assert len(parts) >= 2
    assert all(
        len(f.data) <= 4096 and f.data.startswith(NOTE_HEADER.encode())
        for f in plan.files
    )
    joined = "".join(f.data.decode() for f in parts)
    assert all(f"item {i} " in joined for i in range(300))
    groceries = next(f for f in plan.files if "Groceries" in f.relative).data.decode()
    assert (
        "milk" in groceries and "**" not in groceries and groceries.count("#home") == 1
    )
    both = plan_notes(
        memos,
        MemosConfig(tags=("home", "shop"), match=TagMatch.ALL, folder="From Memos"),
    )
    assert [f.relative for f in both.files] == ["From Memos/Groceries.txt"]
    capped = plan_notes(
        memos, MemosConfig(tags=("work", "travel", "home")), other_notes=999
    )
    assert capped.notes == 1 and capped.skipped == 2  # pinned memo kept first


def test_markdown_and_title_rendering() -> None:
    assert (
        markdown_to_text("## Head\n- [x] done\n> quote\n`code` ~~old~~")
        == "Head\n[x] done\n| quote\ncode old"
    )
    from iOpenPod.app.connected.memos import Memo

    title, files = render_memo(
        Memo("m", "", "#a\nReal title here", ("a",), "", False, "", "", 2)
    )
    assert title == "Real title here" and b"2 attachments in Memos" in files[0]


# --- Calendars ------------------------------------------------------------------------

ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
X-WR-CALNAME:Work\r
BEGIN:VEVENT\r
SUMMARY:Old meeting\r
DTSTART:20200101T100000Z\r
DTEND:20200101T110000Z\r
END:VEVENT\r
BEGIN:VEVENT\r
SUMMARY:Weekly\r
DTSTART:20200106T100000Z\r
RRULE:FREQ=WEEKLY\r
END:VEVENT\r
BEGIN:VEVENT\r
SUMMARY:A very long description that will need folding because it is much longer than seventy-five bytes\r
DTSTART:20991231T100000Z\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_calendar_trim_and_rename() -> None:
    assert calendar_info(ICS).name == "Work" and calendar_info(ICS).events == 3
    trimmed = trim_calendar(ICS, 30, clock=lambda: 1_790_000_000)
    assert (trimmed.kept, trimmed.dropped) == (2, 1)
    assert "Old meeting" not in trimmed.text and "Weekly" in trimmed.text
    assert all(len(line.encode()) <= 75 for line in trimmed.text.split("\r\n"))
    renamed = rename_calendar(trimmed.text, "Office")
    assert (
        calendar_info(renamed).name == "Office" and renamed.count("X-WR-CALNAME") == 1
    )
    assert (
        subscription_url("webcal://cal.example.com/x.ics")
        == "https://cal.example.com/x.ics"
    )
    assert (
        redact("https://calendar.google.com/calendar/ical/secret/basic.ics")
        == "calendar.google.com/…"
    )
    with pytest.raises(ConnectedError):
        calendar_info("not a calendar")
