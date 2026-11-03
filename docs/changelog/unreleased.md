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

## Changed

- **Dojo pin → v0.27.1** (`a82843e`, the release merge of llm-dojo-scoring#118), pinned by commit SHA. It supersedes the unmerged 0.26.0 pin (#149), so this one bump takes in 0.26.0, 0.27.0 and 0.27.1.
  - 0.26.0: reviewer-lane scoring and `dojo-rescore`; the CMS insurance mapping (#93: `claim_type` and `cms_source_table` are separate fields); the five-class extraction schema registry, with an additive `schema` block on detailed scores.
  - 0.27.0 (additive): `score_document`, `get_doc_scorer` and `assess_extraction_payload`, the tiered per-document-type scorers, and a wider `score_archive_block`. Mailroom does not call them yet; the scoring nodes (roadmap M6) will.
  - 0.27.1 (review fixes): the tiered scorers never raise on a bad field value (the type extras are guarded, and `score_archive_block` keeps its pre-0.27 keys if they fail); `score_archive_block` derives the CMS table from `doc_subclass` when no `expected_subclass` is given; `entity_list_f1` / `unlabeled_fields` are null when the block itself has no ground truth to describe. Rows with a `headline_score` are judged by the headline, not `status`. None of these paths is called by mailroom yet.
  - Score movement, checked on the dojo's golden inputs through `get_suite(...).score`: one insurance CMS row (a predicted `cms_source_table` with no labelled table) moves from 0.9167 to 1.0, per the #93 entry. Every other field score is unchanged. The mock experiment (`mailroom-fixtures`) metrics are identical under both pins, and the approved baseline still passes.
  - The fallback snapshot under `vendor/llm-dojo-scoring` is replaced with the same commit's tree.
  - Dojo modules now live in subpackages; the old flat import paths still resolve through Dojo's legacy aliases. The delegation tests now resolve the canonical module name instead of hard-coding the flat one.
  - New tests: `test_scoring_imports_under_vendored_source` (the forced fallback carries the names the scoring nodes need), and parity tests for all five live classes against the dojo field maps and scorer registry, plus `test_no_extract_class_aliases`.

## Added

- **Audit tables and legacy migration** (roadmap M2 of the unified audit row; `src/storage/audit_migrate.py`). Nothing writes the new tables yet; the row and addendum writers come in M3.
  - New tables: `audit_attempts`, `audit_addenda`, `audit_meta`, `audit_trace_io` and `audit_log_legacy`. `audit_log` itself now has a unique `doc_id` and one global `seq` (no longer counted per document).
  - `ensure_schema()` runs `migrate_audit_log()` once per database, under the `audit_log:chain` write lock and in one transaction. It copies every `audit_log` row into `audit_log_legacy` with a single `INSERT ... SELECT` (so each column keeps its stored value), checks the row count and a digest of every column, and only then empties `audit_log`, adds the unique `doc_id` and `seq` indexes and writes `audit_meta` (`migrated_at`, `legacy_rows`, `legacy_tail_hashes_sha256`). A mismatch aborts with nothing changed. It skips once `audit_meta` says it ran, and two processes starting together migrate once.
  - SQLite is backed up first, to `<db dir>/backups/` (or `backup_dir`). Postgres with rows to move runs only with `MAILROOM_AUDIT_MIGRATE=1`, after your `pg_dump`; without it the migration logs `audit_migration_pending` and changes nothing. An empty `audit_log` needs no opt-in.
  - `python -m storage.audit_migrate` runs the migration by hand; `--rollback` renames the new tables to `*_v2_<timestamp>` and restores the previous release's `audit_log` from `audit_log_legacy` (rows written since stay in the renamed tables).
  - `verify_legacy_chain()` (in the migration module for now) runs the per-document hash check over `audit_log_legacy`; M4 builds the full verifier around it.
  - Interim: until the row writers replace it, the per-event writer `write_audit_entry`, its readers and the warehouse and `verify_audit_chains.py` reads follow the migration through `event_log_model()`: they use `audit_log_legacy` once the migration has run and the old `audit_log` while it is pending. New events written in that window land in `audit_log_legacy` after `audit_meta.legacy_tail_hashes_sha256` was taken.
  - Tests: `src/tests/test_audit_migrate.py` against `src/tests/fixtures/audit/legacy_audit.db`, which `src/scripts/build_legacy_audit_fixture.py` rebuilds with the previous writer. Postgres DDL is compiled for the `postgresql` dialect; the migration runs on SQLite.

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
  - `notes X.Y.Z` renders release notes; `publish X.Y.Z [--push] [--commit REF]` creates an annotated tag and a GitHub release through `gh`; `--apply` refuses a commit that is not on `origin/main` unless `--allow-unmerged`.
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
