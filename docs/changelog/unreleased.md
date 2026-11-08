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

## Fixed

- **Watcher concurrency is bounded.** Every watchdog event, startup-scan file and rescan tick used to start its own pipeline thread (400 inbox files ran 400 pipelines at once). Files now wait in a de-duplicated queue served by `watcher.max_concurrent_docs` workers (taxonomy.yaml, default 4; env `MAILROOM_MAX_CONCURRENT_DOCS`). `Watcher.stop()` discards files that have not started; they stay in the inbox. Needs a watcher/API restart.
- **Stale-claim reconcile no longer hijacks in-flight documents.** `claim_file` / `requeue_from_review` stamp the claim time (the hard link kept the upload's old mtime, so a document that waited in a backlog looked orphaned at once), and the periodic reconcile skips files this process is running or has queued.
- **Catalog writes are race-free upserts.** `write_matter_record` / `write_document_record` use one `INSERT ... ON CONFLICT DO UPDATE` (SQLite and PostgreSQL; other dialects keep the old path), so concurrent runs no longer fail with `UNIQUE constraint failed` and drop the stage update. Transient SQLite `database is locked` errors are retried with jittered backoff, and `busy_timeout` is now set before the WAL pragma (`SQLITE_BUSY_TIMEOUT_MS`, default 5000).
