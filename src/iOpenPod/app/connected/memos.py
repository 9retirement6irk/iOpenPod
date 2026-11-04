"""Memos notes for the iPod's Extras → Notes, chosen by tag.

Supports Memos 26.10 and later (REST at ``/api``) and earlier 0.22-0.31 servers
(``/api/v1``); 26.10 answers the old prefix with ``410 Gone``. Requests use a
personal access token as a Bearer token.

The iPod Note Reader shows plain-text files from the ``Notes`` folder at the Volume
root, reads at most 4 KB of each, shows up to 1,000 notes, and presents subfolders
as menus. A leading ``<?xml encoding="UTF8"?>`` line selects UTF-8.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode

from .config import MemosConfig, TagMatch
from .http import (
    MAX_JSON_BYTES,
    CancelledError,
    ConnectedError,
    Transport,
    UrllibTransport,
    json_body,
    normalize_base_url,
)
from .materialize import safe_component

if TYPE_CHECKING:
    from threading import Event

NOTE_MAX_BYTES = 4000  # the Note Reader stops reading at 4,096 bytes
NOTE_HEADER = '<?xml encoding="UTF8"?>\n'
IPOD_MAX_NOTES = 1000
TAG_PATTERN = re.compile(r"(?<![\w#&/])#([^\s#,.;:!?()\[\]{}<>\"']+)")
_API_PREFIXES = ("/api", "/api/v1")
_MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def _object(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class Memo:
    name: str
    creator: str
    content: str
    tags: tuple[str, ...]
    title: str
    pinned: bool
    updated: str
    created: str
    attachments: int


@dataclass(frozen=True, slots=True)
class TagCount:
    tag: str
    count: int


@dataclass(frozen=True, slots=True)
class MemosSummary:
    user: str
    memos: int
    untagged: int
    tags: tuple[TagCount, ...]


@dataclass(frozen=True, slots=True)
class NoteFile:
    relative: str  # beneath the Volume's Notes folder, "/"-separated
    data: bytes


@dataclass(frozen=True, slots=True)
class NotesPlan:
    files: tuple[NoteFile, ...]
    matched: int
    total: int
    split: int
    skipped: int
    notes: int = field(default=0)


class MemosClient:
    def __init__(
        self, url: str, token: str, transport: Transport | None = None
    ) -> None:
        server = normalize_base_url(url)
        server = re.sub(r"/api(/v1)?$", "", server, flags=re.I)
        if not token.strip():
            raise ConnectedError(
                "Enter a Memos access token (Memos → Settings → My Account → Access Tokens)."
            )
        self.server = server
        self._token = token.strip()
        self._transport = transport or UrllibTransport()
        self._api = ""

    def _get(self, url: str, *, method: str = "GET") -> dict[str, object]:
        try:
            response = self._transport.send(
                method,
                url,
                {
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                },
                b"{}" if method == "POST" else None,
                timeout=120,
                limit=MAX_JSON_BYTES,
            )
        except ConnectedError as error:
            if error.status == 401:
                raise ConnectedError(
                    "Memos didn't accept the access token. It may be mistyped, expired, or deleted.",
                    401,
                ) from None
            raise
        looks_json = (
            "json" in response.content_type.casefold()
            or response.body.lstrip()[:1] in (b"{", b"[")
        )
        if not looks_json:
            # A web app page answering an API path: not this API generation.
            raise ConnectedError(
                "That address answered, but not like a Memos server.", 404
            )
        return _object(json_body(response))

    def api(self) -> str:
        if self._api:
            return self._api
        last: ConnectedError | None = None
        for prefix in _API_PREFIXES:
            try:
                self._get(f"{self.server}{prefix}/memos?pageSize=1")
            except ConnectedError as error:
                if error.status in (None, 401):
                    raise  # unreachable, or the token is wrong: say so directly
                last = error
                continue
            self._api = f"{self.server}{prefix}"
            return self._api
        raise ConnectedError(
            f"Couldn't find the Memos API at {self.server}. Use the address you open Memos at. ({last})"
        )

    def current_user(self) -> tuple[str, str] | None:
        """``(resource name, display label)`` for the token's user, if the server says."""

        base = self.api()
        for path, method in (
            ("/auth/me", "GET"),
            ("/auth/sessions/current", "GET"),
            ("/auth/status", "POST"),
        ):
            try:
                data = self._get(f"{base}{path}", method=method)
            except ConnectedError as error:
                if error.status == 401:
                    raise
                continue
            user = _object(data.get("user")) or data
            name = _str(user.get("name"))
            if name:
                label = (
                    _str(user.get("displayName"))
                    or _str(user.get("nickname"))
                    or _str(user.get("username"))
                )
                return name, label
        return None

    def memos(
        self, *, include_archived: bool, cancelled: Event | None = None
    ) -> tuple[Memo, ...]:
        base = self.api()
        allowed = {"NORMAL", "ACTIVE"} | ({"ARCHIVED"} if include_archived else set())
        found: dict[str, Memo] = {}
        for state in ("NORMAL", "ARCHIVED") if include_archived else ("NORMAL",):
            token = ""
            for _ in range(1000):
                if cancelled is not None and cancelled.is_set():
                    raise CancelledError
                query = {"pageSize": "1000", "state": state}
                if token:
                    query["pageToken"] = token
                data = self._get(f"{base}/memos?{urlencode(query)}")
                for raw in cast("list[object]", data.get("memos") or []):
                    row = _object(raw)
                    # Older servers ignore ``state`` and report ``rowStatus``.
                    status = (
                        _str(row.get("state")) or _str(row.get("rowStatus")) or "NORMAL"
                    )
                    memo = _memo(row)
                    if memo is not None and status in allowed:
                        found.setdefault(memo.name, memo)
                token = _str(data.get("nextPageToken"))
                if not token:
                    break
        return tuple(found.values())


def _memo(row: dict[str, object]) -> Memo | None:
    name = _str(row.get("name"))
    if not name:
        return None
    content = _str(row.get("content"))
    raw_tags = row.get("tags")
    tags: list[str] = []
    if isinstance(raw_tags, list) and raw_tags:
        candidates = [str(item) for item in cast("list[object]", raw_tags)]
    else:
        candidates = TAG_PATTERN.findall(_strip_code(content))
    for tag in candidates:
        clean = tag.strip().strip("/")
        if clean and clean not in tags:
            tags.append(clean)
    attachments = row.get("attachments") or row.get("resources") or []
    return Memo(
        name=name,
        creator=_str(row.get("creator")),
        content=content,
        tags=tuple(tags),
        title=_str(_object(row.get("property")).get("title")).strip(),
        pinned=bool(row.get("pinned")),
        updated=_str(row.get("updateTime")),
        created=_str(row.get("createTime")) or _str(row.get("displayTime")),
        attachments=len(cast("list[object]", attachments))
        if isinstance(attachments, list)
        else 0,
    )


def _strip_code(text: str) -> str:
    return re.sub(r"```.*?```|`[^`\n]*`", " ", text, flags=re.S)


def tag_matches(memo_tag: str, wanted: str) -> bool:
    """``#work`` matches ``#work`` and nested ``#work/project`` tags."""

    tag = memo_tag.casefold()
    target = wanted.casefold().strip().lstrip("#").strip("/")
    return tag == target or tag.startswith(target + "/")


def summarize(memos: tuple[Memo, ...], user: str) -> MemosSummary:
    counts: dict[str, list[object]] = {}
    for memo in memos:
        seen: set[str] = set()
        for tag in memo.tags:
            parts = tag.split("/")
            for depth in range(1, len(parts) + 1):
                key = "/".join(parts[:depth])
                if key.casefold() in seen:
                    continue
                seen.add(key.casefold())
                entry = counts.setdefault(key.casefold(), [key, 0])
                entry[1] = cast("int", entry[1]) + 1
    tags = tuple(
        sorted(
            (
                TagCount(cast("str", tag), cast("int", count))
                for tag, count in counts.values()
            ),
            key=lambda item: item.tag.casefold(),
        )
    )
    return MemosSummary(user, len(memos), sum(not memo.tags for memo in memos), tags)


def only_mine(
    memos: tuple[Memo, ...], user: tuple[str, str] | None
) -> tuple[Memo, ...]:
    if user is None:
        return memos
    return tuple(memo for memo in memos if not memo.creator or memo.creator == user[0])


def markdown_to_text(markdown: str) -> str:
    """Memos Markdown as plain text the Note Reader can show."""

    output: list[str] = []
    fence = False
    for line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fence = not fence
            continue
        if fence:
            output.append("    " + line)
            continue
        text = re.sub(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", r"\1", line)
        text = re.sub(
            r"^(\s*)[-*+]\s+\[( |x|X)\]\s+",
            lambda m: m.group(1) + ("[x] " if m.group(2) != " " else "[ ] "),
            text,
        )
        text = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", text)
        text = re.sub(r"^\s{0,3}>\s?", "| ", text)
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", text):
            text = "———"
        if re.match(r"^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*:?-*:?\s*$", text):
            continue
        text = re.sub(
            r"!\[([^\]]*)\]\([^)]*\)",
            lambda m: f"[image{': ' + m.group(1) if m.group(1) else ''}]",
            text,
        )
        text = re.sub(
            r"\[([^\]]+)\]\((https?://[^)\s]+)[^)]*\)",
            lambda m: (
                m.group(1)
                if m.group(1) == m.group(2)
                else f"{m.group(1)} ({m.group(2)})"
            ),
            text,
        )
        text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
        text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
        text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", text)
        text = re.sub(r"~~(.+?)~~", r"\1", text)
        text = re.sub(r"==(.+?)==", r"\1", text)
        text = re.sub(r"`([^`]*)`", r"\1", text)
        output.append(text.rstrip())
    joined = re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip("\n")
    # The Note Reader treats <…> as markup; keep stray angle brackets visible.
    return joined.replace("<", "\u2039").replace(">", "\u203a")


def _date(value: str) -> str:
    match = re.match(r"(\d{4})-(\d\d)-(\d\d)", value)
    if not match:
        return ""
    return f"{int(match.group(3))} {_MONTHS[int(match.group(2)) - 1]} {match.group(1)}"


def note_title(memo: Memo, text: str) -> str:
    title = memo.title
    if not title:
        for line in text.split("\n"):
            candidate = TAG_PATTERN.sub("", re.sub(r"^[•|\[\] x]+", "", line)).strip()
            if candidate:
                title = candidate
                break
    title = " ".join((title or f"Memo {_date(memo.updated or memo.created)}").split())
    return title if len(title) <= 50 else title[:48].rstrip() + "…"


def _cut(text: str, limit: int) -> tuple[str, str]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, ""
    cut = encoded[:limit].decode("utf-8", "ignore")
    newline = cut.rfind("\n")
    if newline > len(cut) // 2:
        cut = cut[: newline + 1]
    else:
        space = cut.rfind(" ")
        if space > len(cut) // 2:
            cut = cut[: space + 1]
    return cut, text[len(cut) :]


def split_note(body: str, budget: int = NOTE_MAX_BYTES) -> list[str]:
    room = budget - len(NOTE_HEADER.encode("utf-8")) - 40  # room for "continued" lines
    parts: list[str] = []
    rest = body
    while rest:
        piece, rest = _cut(rest, room)
        parts.append(piece.strip("\n"))
        rest = rest.lstrip("\n")
    return parts or [""]


def render_memo(memo: Memo) -> tuple[str, list[bytes]]:
    """``(title, file bodies)``; long memos become several numbered parts."""

    text = markdown_to_text(memo.content)
    title = note_title(memo, text)
    if memo.tags:  # a line of only #tags is listed once, in the footer
        text = "\n".join(
            line
            for line in text.split("\n")
            if TAG_PATTERN.sub("", line).strip() or not line.strip()
        ).strip("\n")
    footer: list[str] = []
    if memo.tags:
        footer.append(" ".join(f"#{tag}" for tag in memo.tags))
    if when := _date(memo.updated or memo.created):
        footer.append(f"Updated {when}" if memo.updated else when)
    if memo.attachments:
        footer.append(
            f"{memo.attachments} attachment{'s' if memo.attachments != 1 else ''} in Memos"
        )
    body = text + ("\n\n" + "\n".join(footer) if footer else "")
    parts = split_note(body)
    files: list[bytes] = []
    for index, part in enumerate(parts):
        piece = part
        if len(parts) > 1:
            if index:
                piece = f"(continued from part {index})\n\n{piece}"
            if index < len(parts) - 1:
                piece += f"\n\n(continues in part {index + 2})"
        files.append((NOTE_HEADER + piece + "\n").encode("utf-8"))
    return title, files


def _part_names(stem: str, count: int) -> list[str]:
    if count == 1:
        return [f"{stem}.txt"]
    return [f"{stem} ({index + 1} of {count}).txt" for index in range(count)]


def plan_notes(
    memos: tuple[Memo, ...], config: MemosConfig, *, other_notes: int = 0
) -> NotesPlan:
    """The note files for the chosen tags, directly in the iPod's ``Notes`` folder.

    Notes sit at the top of Notes rather than in subfolders, so they open from the
    first Notes screen without an extra menu.
    """

    wanted = [tag.strip().lstrip("#").strip("/") for tag in config.tags if tag.strip()]
    if not wanted:
        raise ConnectedError("Pick at least one Memos tag to sync.")
    matched: list[Memo] = []
    skipped_memos = set(config.skipped_memos)
    for memo in memos:
        if memo.name in skipped_memos:
            continue  # unticked under its tag in Sync with Host
        hits = [
            want for want in wanted if any(tag_matches(tag, want) for tag in memo.tags)
        ]
        if (len(hits) == len(wanted)) if config.match is TagMatch.ALL else bool(hits):
            matched.append(memo)
    # When the 1,000-note limit is reached, pinned and recently edited memos win.
    matched.sort(key=lambda memo: memo.updated or memo.created, reverse=True)
    matched.sort(key=lambda memo: not memo.pinned)
    room = IPOD_MAX_NOTES - other_notes
    files: list[NoteFile] = []
    used: set[str] = set()
    split = skipped = written = 0
    for memo in matched:
        title, bodies = render_memo(memo)
        if room < len(bodies):
            skipped += 1
            continue
        base = safe_component(title.replace("…", ""), "Memo", limit=60)
        stem, counter = base, 2
        while any(name.casefold() in used for name in _part_names(stem, len(bodies))):
            stem, counter = f"{base} ({counter})", counter + 1
        for name, data in zip(_part_names(stem, len(bodies)), bodies, strict=True):
            used.add(name.casefold())
            files.append(NoteFile(name, data))
        room -= len(bodies)
        written += 1
        split += len(bodies) > 1
    return NotesPlan(
        tuple(files), len(matched), len(memos), split, skipped, notes=written
    )


__all__ = [
    "IPOD_MAX_NOTES",
    "NOTE_HEADER",
    "Memo",
    "MemosClient",
    "MemosSummary",
    "NoteFile",
    "NotesPlan",
    "TagCount",
    "markdown_to_text",
    "only_mine",
    "plan_notes",
    "render_memo",
    "summarize",
    "tag_matches",
]
