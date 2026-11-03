# ADR-0225: Add Connected Sources and iPod Extras

- Status: Accepted
- Date: 2026-11-02

People keep music in Plex, photos in Immich, audiobooks in Grimmory, notes in Memos,
and calendars in iCalendar feeds. This fork lets iOpenPod take Selections from those
servers without an export step, while every iPod Library change still flows through
the existing Host Media Scan, Sync Plan, Sync Review, and Sync Execution.

## Connected Sources feed the Host Media Library

A Connected Source turns a Selection (Plex playlists, artists, albums, or Tracks;
Immich Favorites, albums, people, or individual Photos; Grimmory audiobooks) into
Host files inside its own Connected Source Folder under one user-visible root,
`~/Music/iOpenPod Connected` by default. Each folder is registered as an ordinary
Host Media Library folder with fixed media types: Plex contributes audio and
Playlists, Immich Photos, and Grimmory audio. Sync never reaches a server directly.

A Connected Source Manifest (`.iopenpod-connected.json` in each folder) names the
files iOpenPod wrote with a server revision and size. Refresh downloads missing or
changed items through temporary files and atomic replacement, and removes owned files
that are no longer selected only when their size is unchanged. Files a person adds
are never overwritten or removed. A Grimmory folder-based audiobook arrives as one ZIP
and is tracked as a bundle of files. Starting Sync with a Connected Source Folder
selected refreshes that source first; a download failure is reported and Sync
continues with the files already present.

Plex files are copied from a mapped Host path when a configured Path Mapping reaches
the server's file with the same size, and otherwise downloaded as originals. Plex
playlists become relative `.m3u8` files, and album artwork becomes the folder's
`cover.jpg`, which Host Media Scan already reads. Immich Photos are saved as JPEG.
Grimmory audiobooks are read over OPDS with a separate OPDS user; `.m4a` files are
renamed `.m4b` and MP3 files receive an ID3 `media_type` of `audiobook`, the existing
classification evidence.

## iPod Extras are published through Storage

Notes and calendars are not Library media. They are iPod Extras: plain files the
firmware reads from the Volume's top-level `Notes` and `Calendars` folders. The
Application Layer plans one Storage Transaction per publication, guarded by the
Extras Manifest at `iPod_Control/iOpenPod/extras-v1.json`, which records owned
paths and their SHA-256 per section. New and changed owned files are written,
unwanted owned files are removed only when their bytes are unchanged, and existing
files iOpenPod did not write are reported and left untouched. Publication reserves
the Active iPod exclusively, verifies the Library database is unchanged, and uses
the same commit, recovery, and finalization path as game installation.

Memos notes use the 26.10 REST prefix `/api` with a fallback to `/api/v1` for older
servers; 26.10 answers the old prefix with `410 Gone`. Notes follow the Note Reader
limits: UTF-8 header, 4 KB per file with numbered parts for longer memos, and
1,000 notes in total counting foreign files.

## Plex plays and ratings

Plex is a Scrobbling service. Captured iPod plays are delivered during Sync to the
matched Plex Track as a finished listen from a Plex player named after the iPod with
platform "iPod", falling back to a plain scrobble when timeline reports are refused.
Matching is conservative; unmatched or ambiguous listens are rejected and remain
pending. A mid-batch failure keeps receipts for plays already delivered.

Two-way star ratings compare the Library Workspace with Plex at whole-star precision:
the iPod's stars win when both differ, and Plex-only stars become ordinary Track
edits that the person reviews and saves.

## Secrets

Plex tokens, Immich API keys, Memos access tokens, Grimmory OPDS passwords, and
calendar subscription links are kept in the native OS vault beside Scrobbling
credentials. The Connected Sources configuration file holds only non-secret choices.

## Consequences

Selections cost Host disk space equal to the downloaded media. Physical-device
acceptance of Notes and Calendars publication, and of Plex Play History attribution,
remains unverified; virtual-volume tests cover Extras publication and preservation of
foreign files, and scripted HTTP tests cover each server protocol.
