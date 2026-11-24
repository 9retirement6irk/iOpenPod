"""Combine a BookOrbit audiobook's files into one chaptered M4B for the iPod.

A book stored as many MP3 files becomes one iPod Track: one place in the
Audiobooks list, one bookmark, and the book's chapters to skip between. Books
that are already a single M4B or M4A are left as they are.

The chapters are BookOrbit's own chapter list for the book (embedded, edited in
BookOrbit, or one per file when none is set). Their times are on BookOrbit's book
timeline, which is also how listening progress is exchanged: the M4B carries a
link with no file and the whole book's length, so a position in the M4B is a
position in the book, and BookOrbit is told which of its files that falls in.

Encoding follows the chaptered Album conversion (ADR-0095): FFmpeg decodes every
part, concatenates them, and encodes AAC with the chapters in the container. Speech
needs little bitrate, so the result keeps the source's channels at 64 kbps each,
or less when the source used less.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from iOpenPod.app.media.inspection import MediaInspector
from iOpenPod.app.media.models import StreamKind
from storage import HostPath
from storage.media_processing import (
    MediaToolError,
    MediaToolFinder,
    discover_media_tools,
    run_media_tool,
)

from .bookorbit import SOURCE, BookOrbitClient, BookOrbitManifest, extension
from .grimmory import GrimmoryLink, tag_audiobook
from .http import CancelledError, ConnectedError
from .materialize import safe_component

if TYPE_CHECKING:
    from pathlib import Path
    from threading import Event

# Formats the iPod plays as an audiobook without converting the book.
_KEPT_FORMATS = frozenset({"m4b", "m4a"})
_PER_CHANNEL_BPS = 64_000
_MIN_BPS = 32_000
_SAMPLE_RATE = "44100"


@dataclass(frozen=True, slots=True)
class BookChapter:
    title: str
    start_ms: int


def needs_combining(manifest: BookOrbitManifest) -> bool:
    """Several files, or one file the iPod would otherwise list as an MP3."""

    return len(manifest.assets) > 1 or manifest.assets[0].format not in _KEPT_FORMATS


def combined_link(manifest: BookOrbitManifest) -> GrimmoryLink:
    """The link a combined M4B carries: the whole book, in no one file."""

    total = manifest.total_ms or sum(asset.duration_ms for asset in manifest.assets)
    return GrimmoryLink(manifest.book_id, "0", 0, 0, total, SOURCE)


def combined_name(manifest: BookOrbitManifest, book_title: str) -> str:
    return f"{safe_component(book_title or manifest.title, 'Audiobook', limit=100)}.m4b"


def book_chapters(
    manifest: BookOrbitManifest, durations_ms: tuple[int, ...]
) -> tuple[BookChapter, ...]:
    """BookOrbit's chapters, or one per file, on the book timeline.

    ``durations_ms`` are the measured lengths of the files, used where BookOrbit
    couldn't measure one.
    """

    chapters = sorted(
        (chapter for chapter in manifest.chapters if chapter.start_ms >= 0),
        key=lambda chapter: chapter.start_ms,
    )
    if chapters:
        result: list[BookChapter] = []
        for chapter in chapters:
            if result and chapter.start_ms <= result[-1].start_ms:
                continue  # the container needs strictly later chapters
            result.append(
                BookChapter(chapter.title.strip() or "Chapter", chapter.start_ms)
            )
        if result[0].start_ms > 0:
            result.insert(0, BookChapter(manifest.title or "Start", 0))
        return tuple(result)
    if len(manifest.assets) == 1:
        return ()
    result = []
    elapsed = 0
    for index, (asset, measured) in enumerate(
        zip(manifest.assets, durations_ms, strict=True), start=1
    ):
        result.append(BookChapter(f"Part {index}", elapsed))
        elapsed += asset.duration_ms or measured
    return tuple(result)


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace(";", "\\;")
        .replace("#", "\\#")
        .replace("\n", "\\n")
    )


def _ffmetadata(
    manifest: BookOrbitManifest,
    title: str,
    author: str,
    chapters: tuple[BookChapter, ...],
    total_ms: int,
) -> str:
    values = (
        ("title", title),
        ("artist", author),
        ("album", title),
        ("album_artist", author),
        ("composer", manifest.narrators),
        ("genre", "Audiobook"),
    )
    lines = [
        ";FFMETADATA1",
        *(f"{key}={_escape(value)}" for key, value in values if value),
    ]
    for index, chapter in enumerate(chapters):
        end = chapters[index + 1].start_ms if index + 1 < len(chapters) else total_ms
        if end <= chapter.start_ms:
            continue
        lines.extend(
            (
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={chapter.start_ms}",
                f"END={end}",
                f"title={_escape(chapter.title)}",
            )
        )
    return "\n".join(lines) + "\n"


def _bitrate(channels: int, source_bps: int) -> int:
    """64 kbps a channel, but never more than the source had."""

    target = _PER_CHANNEL_BPS * channels
    if source_bps > 0:
        target = min(target, source_bps // 16_000 * 16_000)
    return max(_MIN_BPS, target)


def fetch_combined(
    client: BookOrbitClient,
    manifest: BookOrbitManifest,
    staging: Path,
    cancelled: Event,
    *,
    title: str = "",
    author: str = "",
    find_tool: MediaToolFinder | None = None,
) -> Path:
    """Download every file of the book and return one tagged, chaptered M4B."""

    def checkpoint() -> None:
        if cancelled.is_set():
            raise CancelledError

    try:
        tools = discover_media_tools(checkpoint=checkpoint, find_tool=find_tool)
    except MediaToolError as error:
        raise ConnectedError(
            f"Combining an audiobook into one M4B needs FFmpeg and FFprobe: {error}"
        ) from None
    if "aac" not in tools.encoders:
        raise ConnectedError(
            "FFmpeg has no AAC encoder, so the audiobook can't be combined into an M4B."
        )
    inspector = MediaInspector(tools.ffprobe)
    parts: list[Path] = []
    measured: list[int] = []
    channels = 1
    source_bits = 0
    try:
        for index, asset in enumerate(manifest.assets):
            checkpoint()
            part = staging / f"part-{index:04d}{extension(asset)}"
            client.download_asset(manifest.book_id, asset, part, cancelled)
            parts.append(part)
            observed = inspector.inspect(HostPath(part), checkpoint=checkpoint)
            audio = [
                stream for stream in observed.streams if stream.kind is StreamKind.AUDIO
            ]
            duration = (audio[0].duration_seconds if audio else None) or (
                observed.duration_seconds
            )
            if not audio or not duration:
                raise ConnectedError(
                    f"Part {index + 1} of {title or manifest.title} has no playable audio."
                )
            measured.append(round(float(duration) * 1000))
            channels = max(channels, min(2, audio[0].channels or 1))
            source_bits += part.stat().st_size * 8
        total_ms = sum(measured)
        expected = manifest.total_ms or total_ms
        chapters = book_chapters(manifest, tuple(measured))
        metadata = staging / "chapters.ffmetadata"
        metadata.write_text(
            _ffmetadata(
                manifest,
                title or manifest.title,
                author or manifest.authors,
                chapters,
                expected,
            ),
            encoding="utf-8",
        )
        layout = "stereo" if channels == 2 else "mono"
        bitrate = _bitrate(channels, round(source_bits / max(1, total_ms / 1000)))
        output = staging / "combined.m4b"
        arguments: list[str] = ["-hide_banner", "-nostdin", "-y"]
        filters: list[str] = []
        labels: list[str] = []
        for index, part in enumerate(parts):
            arguments.extend(("-i", str(part)))
            filters.append(
                f"[{index}:a:0]aresample={_SAMPLE_RATE},"
                f"aformat=sample_fmts=fltp:channel_layouts={layout}[a{index}]"
            )
            labels.append(f"[a{index}]")
        arguments.extend(("-i", str(metadata)))
        filters.append(f"{''.join(labels)}concat=n={len(parts)}:v=0:a=1[aout]")
        arguments.extend(
            (
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[aout]",
                "-map_metadata",
                str(len(parts)),
                "-map_chapters",
                str(len(parts)),
                "-c:a",
                "aac",
                "-b:a",
                str(bitrate),
                "-ar",
                _SAMPLE_RATE,
                "-ac",
                str(channels),
                "-movflags",
                "+faststart",
                "-f",
                "ipod",
                "-progress",
                "pipe:1",
                str(output),
            )
        )
        try:
            run_media_tool(
                tools.ffmpeg,
                tuple(arguments),
                checkpoint=checkpoint,
                timeout_seconds=max(300, total_ms / 1000 * 2),
                progress=lambda _sample: checkpoint(),
            )
        except MediaToolError as error:
            raise ConnectedError(
                f"FFmpeg couldn't combine {title or manifest.title} into an M4B: {error}"
            ) from None
        result = inspector.inspect(HostPath(output), checkpoint=checkpoint)
        made = round(float(result.duration_seconds or 0) * 1000)
        if abs(made - total_ms) > max(1_000, total_ms // 100):
            raise ConnectedError(
                f"The combined {title or manifest.title} is {made // 1000} s long, but its parts add up to {total_ms // 1000} s."
            )
    finally:
        for part in parts:
            part.unlink(missing_ok=True)
    return tag_audiobook(output, combined_link(manifest))


__all__ = [
    "BookChapter",
    "book_chapters",
    "combined_link",
    "combined_name",
    "fetch_combined",
    "needs_combining",
]
