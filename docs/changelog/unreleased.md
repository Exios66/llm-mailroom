---
tags:
  - fix
---

# Unreleased

{% hint style="info" %}
This GitBook Changelog space is generated from the repository
[`CHANGELOG.md`](https://github.com/Exios66/llm-mailroom/blob/main/CHANGELOG.md) (Keep a Changelog). Do not hand-edit these
pages. Regenerate with `PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py`.
{% endhint %}

Unreleased work on `main`. Canonical source: [`CHANGELOG.md`](https://github.com/Exios66/llm-mailroom/blob/main/CHANGELOG.md).

### Fixed

- **Durable Gmail echo outbox** (`pipeline/mail_outbox.py`): completion echoes are queued in `mail_outbox.sqlite` (WAL) under `echo:<doc_id>:<stage>` instead of the in-memory dedup set, so a restart never re-sends and a failed send is retried by a background `OutboxWorker` with exponential backoff (`min(3600, 30*2^attempts)`s, `dead` after 8 attempts, never auto-revived). `send_intake_echo` enqueues and drains inline (True only when the row is `sent`); `dispatch_intake_echo` builds and enqueues on one non-joined daemon thread and wakes the worker. Delivery is at-least-once; rows are claimed with a 300s lease so two processes never send the same row, and a row going dead is logged and counted (`echoes_dead`). One SMTP connection per drain; echo `Message-ID` now uses the mailbox domain rather than `@mailroom.local`.
- **Gmail intake hardening** (`pipeline/gmail_intake.py`): a message that keeps raising is quarantined after `MAILROOM_GMAIL_MAX_ATTEMPTS` (default 3) sweeps — marked `\Seen`, labelled `mailroom/failed`, recorded as processed — instead of being retried forever (`failed_attempts` in the state file, `quarantined` in the poll report). Attachments now stage in a same-filesystem `<inbox>.staging/*.part` and are placed with a no-clobber link (copy fallback on `EXDEV` via a dest-side `.part`), so cross-device deployments no longer fail and staging files are always removed. State is saved after every message, Message-ID-less mail is keyed `uid:<folder>:<uidvalidity>:<uid>`, connection-level IMAP errors abort the sweep without counting against any message, orphaned `.part` files older than an hour are swept, and attachment filenames are capped at 120 chars / 200 UTF-8 bytes with `\` and `..` neutralized.
