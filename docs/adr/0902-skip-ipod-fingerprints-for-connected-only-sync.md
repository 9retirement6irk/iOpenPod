# ADR-0902: Skip iPod Fingerprints for a Connected-Only Sync

- Status: Accepted
- Date: 2026-11-10
- Scope: this fork

## Context

Before Review, Sync scans the Active iPod and calculates an Acoustic Fingerprint
for every iPod Track that has no saved one. Each fingerprint reads two minutes of
audio from the iPod, one Track at a time, so an iPod of a few thousand songs
added outside Sync (for example FLAC files converted to ALAC) took hours to scan.

Connected Sources such as Plex and Grimmory list Remote Items. A Remote Item has
a Remote Identity, not an Acoustic Fingerprint, so it never pairs with an iPod
Track by sound. It pairs by a prior Sync record or by Track details. When a Sync
has no local Host audio or video, no new iPod fingerprint can match anything.

## Decision

When the Host Media Library has no local audio or video files (only Remote
Items, Photos and Playlists), the iPod scan does not fingerprint Tracks that have
no saved fingerprint. Saved fingerprints, the Sync helper and the analysis cache
are still used, and Photos are still checked.

Sync planning keeps those deliberately unfingerprinted Tracks for details
matching. One that matches no Remote Item is offered as iPod-only, which is the
same result a fingerprint would have given. A Track that failed to fingerprint
still needs attention as before.

The iPod scan now logs one summary line: time taken, Tracks reused,
fingerprinted, skipped and failed.

## Consequences

A Plex-only Sync no longer reads every hand-added iPod Track. Adding a local
music or video folder to a Sync turns fingerprinting back on for that Sync.
Duplicate detection among unfingerprinted iPod Tracks is not available in a
Connected-only Sync.
