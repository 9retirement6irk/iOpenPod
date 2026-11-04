# ADR-0900: Add Connected Sources and iPod Extras

- Status: Accepted
- Scope: this fork (fork records are numbered from 0900 so upstream merges never collide)
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
way Grimmory's mobile apps do: iOpenPod reads the provider from Grimmory's public
settings, gets a one-time state from Grimmory, and opens the provider's
authorization page with PKCE (S256) and a nonce. The provider returns through the
custom-scheme link `iopenpod://oauth2-callback`; iOpenPod accepts exactly one link
carrying that state and posts the code to Grimmory's mobile OIDC callback, which
exchanges it and returns ordinary Grimmory tokens. Grimmory accepts a custom-scheme
address only when it is listed under Settings → Authentication → Mobile Redirect
URIs, and the identity provider must allow it for Grimmory's client; both are
one-time server settings.

The operating system opens `iopenpod://` links with iOpenPod. Packaged macOS and
Microsoft Store builds declare the scheme in their bundle and package manifest;
other installations register it for the current user at start-up (the Windows
registry, or a hidden desktop entry with `xdg-mime` on Linux). On Windows and
Linux a link starts a second process, which hands it to the running iOpenPod over a
per-user local socket and exits; macOS delivers it to the running app as a
file-open event. Audiobooks are listed through the app API filtered to audiobook files, by
library, shelf, or search. Libraries come from the web API's `/api/v1/libraries`,
because Grimmory 3.5's app library list reads library paths outside a database
transaction and answers HTTP 500; a library or shelf list that still fails is
reported without undoing the sign-in. A single-file audiobook is downloaded as its original file (through the
player's stream endpoint on Grimmory 3.5, which has no per-file download);
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

## Plex and Grimmory are streamed into Sync

Plex and Grimmory no longer use Selections. Before each Sync, their catalogs
are listed (every Plex song and audio playlist from the music libraries chosen
on the Plex page, every Grimmory audiobook track)
and merged into the Host Media Library after the scan as Remote Items, so they
appear in Sync's media list beside the Host's own files and are checked there.
Nothing is downloaded until Sync Execution prepares a checked item: the download
happens inside that item's preparation, just before it is converted, and Plex
path mappings still copy from a share instead.

A Remote Item's path is its cache location, the same path a pick was saved to, so
an earlier pick counts as a cached copy and keeps its iPod pairing by prior path.
Its fingerprint is a Remote Identity: a marker no fpcalc output begins with,
followed by a digest of the item's server key, stored where Acoustic Fingerprints
are. Sync Plan never compares a Remote Identity with real audio evidence; it
compares file facts instead. Those facts are kept stable across downloads: a
current cached copy reports its own facts, an uncached item reports the server's
size (or the size last downloaded) and a time derived from its revision, and a
download receives that same time. A new server revision therefore changes the
facts and replaces the iPod copy, and an unchanged item is never copied again.

The media cache keeps downloads between Syncs up to a limit in Settings → Sync,
evicting the least recently synced first; with it off, each Sync removes its
downloads afterwards. Covers are always kept because Sync reads them for artwork.
The last listing is remembered, with audiobook details and downloaded sizes, so a
server that cannot be reached still shows its items. The Plex and Grimmory
folders are no longer registered as scanned Host folders. Immich keeps its
Selections.

Plex lists genres on albums and artists, not on Tracks, so a Plex Remote Item
takes its album's genre and year, else its artist's genre. A library left out on
the Plex page (for example an audiobook library, when Grimmory provides audiobooks)
contributes no items, and its songs are dropped from Plex playlists; a playlist
left empty is not listed. New libraries are included until left out.

Movie and TV libraries are used only when chosen on the Plex page, for movies,
music videos (movie-type libraries) or TV shows (TV-type libraries), because
videos are large. Their movies and episodes become video Remote Items with the
matching Media Type, a TV episode carrying its show, season and episode number,
and Sync downloads and prepares them like Host video files, converting them for
the iPod. Each movie and music video is cached in its own folder beside its
poster, and episodes share their season's folder and art. Music videos without
a separate artist take one from an "Artist - Title" name.

## Sync with Host chooses everything

Connected pages set a source up (addresses, sign-ins, uploads); Sync with Host's
media list chooses what goes on the iPod. Under CONNECTED, beside the Host media
pages, are checklists for Immich (Favorites, albums, people), Notes (tags, with
their memos underneath), Calendars (files and links) and Contacts (uploaded files,
with their contacts underneath). Ticks are saved in the Connected Sources
configuration as they change: Immich Selections, Memos tags and left-out memos,
and left-out calendar and contact keys.

Immich Photos are Host media. Updating photos downloads the ticked groups and
scans again, putting back every tick and untick the person made on media the
earlier scan had; Photos in the Immich folder are ticked, because only ticked
groups put them there. Notes, calendars and contacts are not Host media: when
any is set up, Sync counts them as work and, after a successful or partial Sync,
publishes each kind that is set up, one after another, before trimming the media
cache. A kind failing is reported and does not stop the others.

## iPod Extras are published through Storage

Notes, calendars and contacts are not Library media. They are iPod Extras: plain
files the firmware reads from the Volume's top-level `Notes`, `Calendars` and
`Contacts` folders. The
Application Layer plans one Storage Transaction per publication, guarded by the
Extras Manifest at `iPod_Control/iOpenPod/extras-v1.json`, which records owned
paths and their SHA-256 per section. New and changed owned files are written,
unwanted owned files are removed only when their bytes are unchanged, and existing
files iOpenPod did not write are reported and left untouched. Publication reserves
the Active iPod exclusively, verifies the Library database is unchanged, and uses
the same commit, recovery, and finalization path as game installation.

Memos notes use the 26.10 REST prefix `/api` with a fallback to `/api/v1` for older
servers; 26.10 answers the old prefix with `410 Gone`. Every note is written directly
in the iPod's `Notes` folder, once however many chosen tags it has, so it opens from
the first Notes screen. Notes follow the Note Reader limits: UTF-8 header, 4 KB per file with numbered parts for longer memos, and
1,000 notes in total counting foreign files.

Contacts come from uploaded vCard files kept beside the configuration. Each
contact becomes its own `.vcf` file so one malformed card cannot hide the rest:
photos, logos, sounds and keys are dropped, vCard 4.0 is labelled 3.0 (whose
common fields are the same), and lines end in CRLF. A contact is known by its UID,
else by a digest of its cleaned card, so the same contact in two uploads is sent
once. At most 5,000 contacts are written.

## Plex plays and ratings

Plex is a Scrobbling service. Captured iPod plays are delivered during Sync to the
matched Plex Track as a finished listen from a Plex player named after the iPod with
platform "iPod", falling back to a plain scrobble when timeline reports are refused.
Matching is conservative; unmatched or ambiguous listens are rejected and remain
pending. A mid-batch failure keeps receipts for plays already delivered.

Two-way star ratings compare the Library Workspace with Plex at whole-star precision:
the iPod's stars win when both differ, and Plex-only stars become ordinary Track
edits that the person reviews and saves.

One switch in Settings → Sync → Scrobbling, on by default, controls Plex: plays
go to the Plex server chosen on the Plex page during Sync and Scrobble now, and
Scrobble now then sets Plex stars from the iPod's without a review, because only
Plex changes. Plex-only stars still reach the iPod only through the Plex page's
reviewed sync. Turning the switch off keeps the Plex sign-in and leaves Plex out
of both.

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
acceptance of Notes, Calendars and Contacts publication, and of Plex Play History attribution,
remains unverified; virtual-volume tests cover Extras publication and preservation of
foreign files, and scripted HTTP tests cover each server protocol.
