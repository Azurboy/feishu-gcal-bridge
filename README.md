# Feishu Calendar Bridge

Sync one Feishu CalDAV calendar into a dedicated secondary calendar in a personal Google account. This is a local, one-way Python tool. The source is read-only; the Google copy is updated by the Calendar API.

**Development status:** implementation in progress. Live Feishu and personal Gmail acceptance tests have not yet been run. See [the approved development spec](docs/DEVELOPMENT_SPEC_v0.1.md) and [research](docs/RESEARCH_AND_LAUNCH.md).

The CLI will provide `fgbridge setup`, `fgbridge sync --dry-run`, `fgbridge sync`, `fgbridge status`, and `fgbridge schedule install/remove`. Setup requires a Feishu CalDAV account and a personal Google Desktop OAuth client. The default schedule is every 120 seconds while the Mac is awake.

No data is sent to a project server. The chosen event fields are copied to Google and can be read by apps authorized on that Google account. Credentials are stored in a private local directory, outside this repository.
