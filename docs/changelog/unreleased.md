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

- DMR-111: refuse ambiguous legacy basename reads after failed moves or metadata
  changes. Current stage, matter or class does not prove historical file ownership;
  document-ID paths and genuinely unique basenames remain available.

- DMR-111: preserve document bytes when a worker processing filename already
  exists, including review resume; require unambiguous ownership for legacy
  failed/archive/processing basename lookups; and prevent delayed warehouse
  snapshots from replacing newer document revisions. Malformed revision data
  fails before publication instead of silently discarding evidence.

- Adopt the published Dojo v0.24.2 scoring release for standalone installs and
  deployment builds, matching the scoring engine already used by Digital-Mailroom.
  Refresh documented dependency pins and the installed-version regression together
  (DMR-111); this does not change production prompt selection.

## Documentation

- DMR-093: document the pending five-package Digital-Mailroom core, source-first ownership, and published Enron/claims preservation branches. External feeder guides and provenance remain available; hub pruning lands separately after PR162.
- CodeAnt review: scope the repository-index import instructions to imported core packages; issue, evaluation, and external-feeder repositories retain their own workflows.
