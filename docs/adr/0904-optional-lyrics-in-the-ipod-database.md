# ADR-0904: Optional Lyrics in the iPod Database

- Status: Accepted
- Date: 2026-11-11
- Scope: this fork

## Context

The iPod Library database stores each Track's lyric text in full, next to the
copy embedded in the media file. Files with synchronized lyrics carry a few
thousand characters each, stored as UTF-16, so a Track grows from under 2 KB to
6 KB or more. A Sync of 2,230 Tracks and 75 Playlists produced a 14.4 MB
database for an iPod with 32 MB of memory. The database limit for that model is
also 32 MB, so nothing warned, and the iPod showed an empty Library. The same
Tracks on an iPod with 64 MB of memory played normally, and one Playlist alone
worked on the smaller iPod.

## Decision

A Sync setting, **Store Lyrics in the iPod Library**, is on by default and can
be overridden per iPod.

- Off: a Track that Sync adds gets no lyric text, so the database holds no
  lyric record and the Track is not marked as having lyrics.
- Off: a Track that Sync updates keeps whatever lyric text the database already
  has. Removing it would require rewriting the media file on the iPod.
- On: unchanged behavior.

## Consequences

- With the setting off, songs synced from then on show no lyrics on the iPod
  and none in iOpenPod's Lyrics column for that iPod.
- A Library that is already large stays large until its Tracks are removed and
  synced again.
- The database limit for 32 MB iPods still equals their whole memory. Lowering
  it needs a measured safe size and is left for a later change.
- Keeping lyrics visible on the iPod without database text would need the
  Library model to stop deriving the lyrics flag from the text. Not done here.
