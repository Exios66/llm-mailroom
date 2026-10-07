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

- **Gmail intake hardening** (`pipeline/gmail_intake.py`): a message that keeps raising is quarantined after `MAILROOM_GMAIL_MAX_ATTEMPTS` (default 3) sweeps — marked `\Seen`, labelled `mailroom/failed`, recorded as processed — instead of being retried forever (`failed_attempts` in the state file, `quarantined` in the poll report). Attachments now stage in a same-filesystem `<inbox>.staging/*.part` and are placed with a no-clobber link (copy fallback on `EXDEV` via a dest-side `.part`), so cross-device deployments no longer fail and staging files are always removed. State is saved after every message, Message-ID-less mail is keyed `uid:<folder>:<uidvalidity>:<uid>`, and attachment filenames are capped at 120 chars with `\` and `..` neutralized.
