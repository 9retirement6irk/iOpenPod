# ADR-0901: Choose Playlists in Select Media

- Status: Accepted
- Date: 2026-11-05
- Amends: ADR-0159
- Scope: this fork

## Context

ADR-0159 offers every supported Host Playlist in Sync Review and keeps Playlist
choices separate from Track selection. With large libraries, and with Plex
Playlists listed as Remote Items, Review opened with dozens of Playlists marked
"Needs attention" because none of their Tracks were selected, and choosing a
Playlist meant opening it and ticking its Tracks one by one.

## Decision

Playlists are chosen in Select Media. The Sync sidebar shows a checkbox on each
supported Host Playlist (regular, top-level, not firmware-managed); Smart
Playlists, folders and nested Playlists have none. Sync Selection owns the chosen
Playlists:

- Ticking a Playlist chooses it and ticks all of its Tracks.
- Unticking stops syncing it and unticks only its Tracks that aren't on the iPod
  and aren't in another chosen Playlist, so it never plans a removal.
- A Playlist with a saved association (ADR-0159) starts ticked, so Playlists
  synced before keep syncing; the others start unticked. Choices survive a
  rescan of the same scan session.

Unchosen Playlists are passed to Sync as excluded choices and hidden from Review.
Review still shows each chosen Playlist's target, changes and problems, and can
skip one for this Sync or change its match. Explicit Track removals still remove
their occurrences from every Playlist.

## Consequences

Track selection is no longer entirely independent of Playlists: ticking a
Playlist is a shortcut for ticking its Tracks. Opening a Playlist still shows its
Tracks with their own checkboxes. Matching, association and execution are
unchanged.
