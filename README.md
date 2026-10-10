# Inbox

Python 3.9+ LAN service for capturing text and URLs into a Markdown inbox. No third-party dependencies.

## Setup

1. Copy `config.example.json` to `config.json` and set absolute paths and an assigned private LAN IPv4 address. Wildcard, loopback and public addresses are rejected.
2. Create the private state directory and a `secrets.json` file there containing a `token` string of at least 32 ASCII characters. Generate a random token locally with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. Keep the token private.
3. Run `python3 server.py --config config.json`.

The existing Mac installation uses launchd; this repository does not install or restart the service. Use HTTP only on a trusted private LAN. Do not expose the port to the Internet.

## API and storage

`GET /health` reports service health. `POST /capture` requires `Authorization: Bearer TOKEN` and `Content-Type: application/json`:

```json
{"id":"in-20261010143025123","content":"https://example.com/","tags":["example"]}
```

IDs contain 1–128 letters, digits, underscores or hyphens. Content contains 1–16384 UTF-8 bytes. Tags are an optional list of up to 10 words, each up to 32 characters, using letters, digits, underscores or hyphens. Bodies are limited to 32768 bytes.

Standalone URLs are preserved; shared text containing a URL is normalized to the first valid HTTP(S) URL. Plain text is preserved. Successful responses include `ok: true`, the ID and `duplicate`. An identical retry with the same ID is safe; different content or tags with the same ID returns 409.

Markdown is grouped alphabetically by first tag (`no-tags` when absent), newest first per group. SQLite retains content, tags, IDs, ordering and UTC creation timestamps (older imports may lack timestamps). Writes use a writer lock, atomic replacement and a recovery journal. External Markdown edits halt writes for manual reconciliation.

**Deleting the Markdown inbox resets capture history during recovery and creates a fresh empty inbox.** To preserve data, back up Markdown and SQLite together under the writer lock.

## iCapture backup

Import `shortcuts/iCapture.shortcut` on Mac or iPhone. It is named **iCapture Backup**. Replace `REPLACE_WITH_YOUR_INBOX_TOKEN` and `http://YOUR-MAC.local:8765/capture` with your token and endpoint, then rename if desired.

The shortcut accepts shared text and URLs, with clipboard fallback. It suggests an editable domain tag, ignoring common web/mobile/language prefixes, or `no-tags`. This is a prefix rule, not a full public-suffix parser.

An `in-` date/time ID with milliseconds is generated once per invocation. Success shows an English notification. Service errors offer Retry or Cancel, with at most two attempts using the same ID. Native network failures/timeouts can abort Shortcuts before the menu. There is no offline queue.

The backup contains placeholder credentials and endpoint. Private config, SQLite, captured data and the configured working shortcut are not included.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary data. HTTP logging is disabled to avoid logging tokens or content; launchd may collect technical stdout/stderr.
