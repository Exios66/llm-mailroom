---
tags:
  - feature
  - improvement
  - fix
---

# Unreleased

{% hint style="info" %}
This GitBook Changelog space is generated from the repository
[`CHANGELOG.md`](https://github.com/Exios66/llm-mailroom/blob/main/CHANGELOG.md) (Keep a Changelog). Do not hand-edit these
pages. Regenerate with `PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py`.
{% endhint %}

Unreleased work on `main`. Canonical source: [`CHANGELOG.md`](https://github.com/Exios66/llm-mailroom/blob/main/CHANGELOG.md).

## Added

- **Extraction-gate hook** (`agents.extraction_gate`). It is opt-in and advisory.
  - A host-registered gate reviews each extraction in `extract_node` / `retry_extract_node`.
  - The handoff carries the **sorter's** `doc_subclass` (else `contract_subtype`), never a ground-truth label, so 1.3.0+ question sets apply their per-template rules.
  - The answer is recorded on state (`extraction_gate`); from `extract_node` it is also in the `extracted` audit entry (the retry path writes no audit entry). Routing is unchanged.
  - An extraction whose fields are all None / empty (the `guard_extraction` emptiness rule) is skipped as `empty_extraction`, not sent. Gate answer values must be JSON (`allow_nan=False`; tuples become lists); anything else is recorded as a malformed answer instead of being lost when the audit entry is written.
  - Gate calls keep their own abandoned-thread list and cap, so stalled gate calls never block the decision backend.
  - Gate failures are recorded, not raised.
  - The bounded thread runner behind the decision backend is factored out as `_run_bounded` and shared by both.
  - Docs: `docs/pipeline-reference-llm-mailroom/decision-reviewer.md`. Tests: `src/tests/test_extraction_gate_hook.py`.

- **Release tool** (`src/scripts/release.py`, tests in `src/tests/test_release_tool.py`). It is stdlib-only and gives the package the same version-bump workflow as llm-dojo-scoring.
  - `status` and `check` report the chain across `pyproject.toml`, `CHANGELOG.md` (headings, compare links), every documented "current release" string, tags, and the GitBook mirror.
  - `cut X.Y.Z` stamps `[Unreleased]`, opens a fresh one, moves every version location together and regenerates `docs/changelog/`. It plans fully in memory first, so a location it cannot move aborts before anything is written.
  - `notes X.Y.Z` renders release notes; `publish X.Y.Z [--push] [--commit REF]` creates an annotated tag and a GitHub release through `gh`.
  - Mutating commands are dry runs unless `--apply`; `--json` returns a `next` list; exit codes are 0/1/2. It never force-pushes, deletes or moves tags, or amends commits.
  - It does not touch the Dojo dependency pin; that stays with `bump_dojo_scoring.py`.

- **Vendored Dojo fallback** (`src/dojo_source`, `vendor/llm-dojo-scoring`).
  - The installed release (the pyproject git pin) stays the primary Dojo.
  - A tracked snapshot of the same pinned commit (0.25.0, `e35504db`) is served only when the release is missing or fails while importing, so scoring survives a broken install or dependency outage.
  - Every package that imports the Dojo registers the lazy import hook; nothing is imported eagerly.
  - `MAILROOM_DOJO_SOURCE=auto|installed|vendored` picks the policy; `dojo_source.status()` reports the source that served the import.
  - `src/tests/test_dojo_source.py` checks the snapshot against the pyproject pin and covers the missing and broken-release fallbacks.

## Changed

- The production sorter is the frozen `sorter_v15`: V14 plus the corpus class boundaries
  (B1-B3: indentures and securities rights instruments are corporate_record; all mailbox
  text is correspondence) and subclass conventions (S1-S5: CMS carrier vs outpatient by the
  Medicare notice, Certificates of Designation as officer_certificate, charter amendments,
  board-adopted bylaw amendments, the correspondence marker precedence, contract family over
  `other`, merger consideration from the conversion section). On the 2,979-document train
  split V14 got 122 classes and 664 subclasses wrong in these patterns. The sha256 is locked
  in `test_sorter_prompt_freeze.py`, which refuses V14 as production; a managed prompt
  without the guard clauses is not served over the pin.
- The sorter's subclass catalog (user message) is the Dojo sorter guide: one corpus meaning
  per key, the correspondence precedence order, and no `certificate_of_formation` (a scoring
  key with no ground-truth rows).
- A decision review that is parked goes to the classification arbiter before a person; the
  graph node `arbitrate-classification` is typed `agent` in `NODE_OBSERVATION_TYPES`.
- Dojo pin: llm-dojo-scoring 0.25.0, the release merge commit `e35504d` (Dojo PR #91: sorter
  guide, unannotated merger subclass, the classification arbiter node). Pinned by commit until
  the `v0.25.0` tag is published; then run
  `PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --apply --tag v0.25.0`.

## Fixed

- Routing: `routing_coverage_expected_fields` no longer falls back to checking
  every ground-truth key (and parking contracts/mergers at human review) when no
  ground-truth key is a registered schema field for the document type. Returns
  the scoped (possibly empty) subset instead.

- DMR-113: consume the interim post-v0.24.2 Dojo commit `718e326` (explicit
  `total_tokens: 0` zero-usage fix); the immutable pin is identified as interim
  and will move to the next released commit.

- DMR-113: keep archived documents requeueable after a doc-type override, treat
  an unreadable processing root as a missing source candidate instead of a 500,
  refuse offline SHA-to-tag rewrites in the Dojo bump tool, reject malformed
  40-hex prefixes in the docs-truth pin guard, and resolve the vendored Dojo
  sibling on a direct `run_hf_pilot` run. Correct the current Dojo version and
  field-scoring module references.

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
