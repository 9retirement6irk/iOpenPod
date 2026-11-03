"""Connected Sources: self-hosted servers that supply media and extras for Sync.

Each Connected Source turns a user's Selection on one server into Host files inside
an application-owned Connected Source Folder. Those folders are ordinary Host Media
Library roots, so the normal Host Media Scan, Sync Plan, Sync Review, and Sync
Execution workflows decide what reaches the iPod. Notes and calendars, which are not
Library media, are published separately as iPod Extras through Storage.

Nothing in this package imports Qt; controllers adapt it for the GUI.
"""
