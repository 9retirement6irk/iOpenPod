# ADR-0903: Review Playlists in Their Own Sync Stage

- Status: Accepted
- Date: 2026-11-10
- Amends: ADR-0901
- Scope: this fork

## Context

ADR-0901 moved choosing Playlists to Select Media and kept their review in a
four-row table under the media changes in Review. With dozens of Plex Playlists
that table was cramped and squeezed the media changes above it.

Ticking a Playlist also froze the window for seconds on a large library. Every
tick worked out the whole Playlist review again, including correlating the Host
and iPod libraries a second time, and every Sync page re-filtered, re-sorted and
regrouped its lists although only one page is on screen.

## Decision

Sync with Host has five stages: Sources, Select Media, Playlists, Review, Sync.

- **Playlists stage.** When any Playlist is chosen, Select Media continues to a
  Playlists stage that gives the Playlist review table the whole page, with the
  Sync Playlists switch and Change match. Review Sync continues to Review, which
  no longer holds the table but still counts Playlist changes in its summary and
  offers a way back. With no Playlist chosen the stage is skipped.
- **All Playlists.** A three-state checkbox above the Playlists in Select Media
  chooses every Playlist or none in one update of Sync Selection.
- **Playlist changes are worked out when shown.** Selection changes in Select
  Media only mark them stale. They are worked out when the Playlists or Review
  stage opens, from the comparison Sync Selection already holds.
- **Only the shown page stays live.** The Host Library pages layer their own
  filter, sort and grouping proxies over shared models. A page that is not shown
  pauses those proxies for selection changes and catches up when shown. Row
  insertions, removals and resets are always followed.

## Consequences

Ticking a Playlist in a 12,000-Track library takes about a tenth of a second
instead of about two. Opening another Sync page after ticking does that page's
filtering and sorting once. Playlist matching, association and execution are
unchanged.
