# ADR-0905: Subscribe to Calendar Links from the Calendar Page

- Status: Accepted
- Date: 2026-11-17
- Scope: this fork

## Context

Upstream 2.1.4 added a Calendar page that reads and edits the calendars in the
iPod's `Calendars` folder and imports `.ics` and `.vcs` files. This fork already
kept calendar links (`https://` or `webcal://`) under Connected → Calendars and
wrote their calendars to the same folder at the end of each Sync (ADR-0900). The
two met only on the iPod: a link could not be added where calendars are viewed.

## Decision

The Calendar page gains **Subscribe to Link…** next to **Import Calendar…**.

- It asks for the link and an optional name, then adds the link through the
  Connected controller. Links stay in the OS keychain, because private calendar
  addresses embed secret tokens.
- With a writable iPod the calendars are downloaded and written straight away,
  using the step Sync runs last. Otherwise the link waits for the next Sync.
- The page reloads after calendars are written, by this action or by Sync.
- Connected → Calendars stays the place to rename or remove links, add files and
  choose how much history to keep.

One writer owns subscribed calendars. They are published as whole files under the
Connected ownership manifest and replaced on each refresh.

## Consequences

- Edits made on the Calendar page to a subscribed calendar last until its next
  refresh. The page does not yet mark which calendars are subscribed.
- A link already added under its `webcal://` or `https://` spelling is refused.
- Upstream's page works unchanged when no Connected controller is supplied; the
  button is hidden.
