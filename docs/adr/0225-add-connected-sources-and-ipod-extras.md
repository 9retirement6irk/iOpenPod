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
are never overwritten or removed. A Grimmory audiobook made of several files is
downloaded track by track and tracked as one bundle of files. Starting Sync with a Connected Source Folder
selected refreshes that source first; a download failure is reported and Sync
continues with the files already present.

Plex files are copied from a mapped Host path when a configured Path Mapping reaches
the server's file with the same size, and otherwise downloaded as originals. Plex
playlists become relative `.m3u8` files, and album artwork becomes the folder's
`cover.jpg`, which Host Media Scan already reads. Immich Photos are saved as JPEG.
Grimmory audiobooks use Grimmory's own REST API rather than OPDS, because OPDS can
neither report nor record listening progress. A person signs in once with their
Grimmory username and password; only the returned access and refresh tokens are kept,
and an expired access token is renewed once before the person is asked to sign in
again. A person whose Grimmory account uses single sign-on signs in the
way Grimmory's web app does: iOpenPod reads the provider from Grimmory's public
settings, gets a one-time state from Grimmory, and opens the provider's
authorization page with PKCE (S256) and a nonce. The provider redirects to a
loopback address, `http://127.0.0.1:53817/oauth2-callback` (RFC 8252), where
iOpenPod accepts exactly one answer carrying that state, then posts the code to
Grimmory's OIDC callback, which exchanges it and returns ordinary Grimmory tokens.
Grimmory accepts an http redirect only when its origin matches the request's
`Origin`, so iOpenPod sends the loopback origin. The port is fixed because many
self-hosted identity providers match redirect addresses exactly; a provider admin
adds that one address to Grimmory's client, and a Grimmory that limits
`app.cors.allowed-origins` must also allow `http://127.0.0.1:53817`. Audiobooks are listed through the app API filtered to audiobook files, by
library, shelf, or search. A single-file audiobook is downloaded as its original file;
a folder-based one is downloaded through its per-track stream endpoint. `.m4a` files
are renamed `.m4b` with `stik` 2, and MP3 files receive an ID3 `media_type` of
`audiobook`, the existing classification evidence.

Each file's comment carries a Grimmory Link,
`grimmory:<book>:<book file>:<track index>:<track start ms>:<book duration ms>`,
which survives Sync into the iPod Library. Listening progress is then exchanged per
book: the iPod's position is the bookmark of its most recently played Track of that
book plus the Track's start. A Progress Mark in the configuration records the iPod and
Grimmory positions seen when the two last agreed. The side that moved since then wins;
when both moved, the more recent listen wins, or the further position when the iPod
has no listening time. Positions within five seconds count as the same. iPod
positions are posted to Grimmory as its own player does (whole-book and in-track
position, track index, percentage). Grimmory positions become ordinary Track edits to
bookmark, played, and last-played fields, which the person reviews and saves; until
they are saved the Progress Mark keeps the iPod's old position, so the next exchange
offers the same move again instead of sending the old position back. Before Sync,
iPod positions are optionally sent to Grimmory; moving iPod bookmarks always waits for
an explicit exchange. A Progress Mark is not per iPod, so two iPods listening to the
same book take turns as the side that moved.

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

Plex tokens, Immich API keys, Memos access tokens, Grimmory sign-in tokens, and
calendar subscription links are kept in the native OS vault beside Scrobbling
credentials. The Connected Sources configuration file holds only non-secret choices.

## Updates

Fork builds check this fork's GitHub releases, not upstream's, and ship with no
update trust key. A trusted key would let a build install whatever release the
matching feed signs, and upstream's key signs upstream builds, which would replace
Connected Sources. Without a key, the built-in updater reports a newer fork release
and links to its download page. Automatic installation for fork builds needs a key
generated with `scripts/update_feed.py`, its private half stored as the
`IOPENPOD_UPDATE_PRIVATE_KEY` repository secret, and an `update-feed` branch.

## Consequences

Selections cost Host disk space equal to the downloaded media. Physical-device
acceptance of Notes and Calendars publication, and of Plex Play History attribution,
remains unverified; virtual-volume tests cover Extras publication and preservation of
foreign files, and scripted HTTP tests cover each server protocol.
