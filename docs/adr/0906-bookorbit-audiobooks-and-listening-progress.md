# ADR-0906: BookOrbit Audiobooks and Listening Progress

- Status: Accepted
- Date: 2026-11-22
- Scope: this fork

## Context

[BookOrbit](https://github.com/bookorbit/bookorbit) is a self-hosted library for
ebooks and audiobooks. Its web player and apps share one audiobook playback state per
book: the audio file being played (an "asset" in the book's manifest) and a position
inside it. Every write names the revision it builds on and the manifest revision it
was made against, so two players can't silently overwrite each other.

The fork already streams Grimmory audiobooks into Sync and exchanges listening
progress with Grimmory (ADR-0900). BookOrbit needs the same two things.

## Decision

BookOrbit is a third streamed Connected Source, beside Plex and Grimmory.

- **Sign-in.** A BookOrbit API key (`bok_…`), sent as `X-Api-Key` and kept in the OS
  vault. API keys are BookOrbit's supported way for other programs to connect and
  can be revoked on their own. Progress needs a key with write access; a read-only
  key is refused with a message saying so.
- **Listing.** `POST /api/v1/books/query` filtered to audio formats, 200 books a
  page. Each book's manifest is cached with the book's `updatedAt` and read again
  only when that changes.
- **Files.** One Remote Item per manifest asset, downloaded from
  `/api/v1/audiobooks/{id}/assets/{asset}/content` into `BookOrbit/` in the Connected
  folder. The cache revision includes the asset's `etag`, so a replaced file
  downloads again.
- **Links.** Each file is tagged with a link of the Grimmory Link shape under its
  own name, `bookorbit:<book>:<file>:<index>:<start ms>:<book ms>`. The link parser
  takes the source name, so a book is never matched to the wrong server.
- **Progress.** BookOrbit's position is turned into a position in the whole book
  (the start of its asset plus the position in it), and Grimmory's progress rules
  decide which side moves. When saving, the position is placed in the file it was
  heard in when BookOrbit still has that file, otherwise by time in the current
  manifest. A revision conflict (409) or manifest change (412) is read again and
  retried once.

### One M4B per book

With **Combine each audiobook into one M4B with chapters** on (the default), a book
of several files, or of an MP3, is one Remote Item. Its fetch downloads every file,
checks each with FFprobe, and has FFmpeg concatenate them into one AAC M4B (64 kbps a
channel, no more than the source, mono kept mono), following the chaptered Album
conversion (ADR-0095). The chapters are the manifest's chapter list, or one per file
when BookOrbit has none, at BookOrbit's file boundaries. The downloaded parts are
deleted once the M4B is made. A book that is already a single M4B or M4A is copied
unchanged.

The M4B's link names no file and covers the whole book
(`bookorbit:<book>:0:0:0:<book ms>`), so its bookmark is a position in the book.
Saving progress places it in the BookOrbit file it falls in, by the manifest's file
lengths; reading progress adds the start of BookOrbit's file to its position. The
cache revision is the manifest revision, so changed files rebuild the M4B.

## Consequences

- BookOrbit servers without the audiobook manifest and playback-state API (before
  its 3.x releases) aren't supported.
- Progress marks reuse Grimmory's mark type; its `grimmory_ms` field holds
  BookOrbit's position for BookOrbit books.
- Combining re-encodes MP3s to AAC (a small quality loss) and takes minutes per book
  during Sync; it needs FFmpeg with its AAC encoder.
- Progress inside a combined book is as exact as BookOrbit's file lengths: a file
  BookOrbit couldn't measure moves positions after it.
- Switching combining off or on changes the Remote Items, so Sync offers the other
  form of each book.
- Tested against a fake server written from BookOrbit's server code, not a running
  BookOrbit.
