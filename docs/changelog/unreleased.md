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

## Security

- **Tokenless API requests are served only on a loopback listener** (audit L-2). A direct `uvicorn api.main:app --host 0.0.0.0` never runs `assert_bind_allowed()`, so with no `MAILROOM_API_TOKEN` it served every route to the network. Token-protected routes now read the ASGI `server` address (the local socket the request arrived on) and answer `503` when no live token is configured and that address is not loopback; unix sockets and unknown addresses fail closed, and so does a loopback request that carries `Forwarded` / `X-Forwarded-For` / `X-Real-IP` (a same-host reverse proxy). A proxy that adds no forwarding headers still looks local: set a token behind any proxy. `/health`, FastAPI's `/docs` / `/redoc` / `/openapi.json` and live-token behaviour are unchanged. If you run the API off loopback without a token on purpose, set `MAILROOM_API_TOKEN`.
- Tokenless loopback mode also refuses requests carrying `X-Forwarded-Proto`, `X-Forwarded-Host`, `X-Forwarded-Port`, `Via`, `True-Client-IP` or `CF-Connecting-IP`, in addition to `Forwarded` / `X-Forwarded-For` / `X-Real-IP`, and requests whose `Host` or `Origin` is not loopback or that carry `Sec-Fetch-Site: cross-site`. That closes DNS rebinding, a proxy keeping the public `Host`, and a page on another site posting to localhost. A proxy that rewrites `Host` to loopback and adds no forwarding header still looks local: set a token behind any proxy.
- `MAILROOM_API_TOKEN` is now read per request, like `MAILROOM_API_TOKENS` and `MAILROOM_API_TOKEN_REVOKED`, instead of once at import. A token set after `api.main` is imported (as local-mailroom-sandbox's offline pilot does) now counts rather than leaving the app in tokenless mode.

## Added

- **Display contract for The-Mailroom** (`contract/mailroom-contract.json`, `src/scripts/export_contract.py`).
  - Built from the pipeline's runtime constants: the compiled LangGraph's nodes and edges with each node's span and Langfuse observation type, `NODE_OBSERVATION_TYPES`, taxonomy agents, the five document classes with their schema, specialist, field keys and sorter subclass catalog, failure classes, `SCORE_CONFIGS` names and Langfuse aliases, and the `confidence` thresholds.
  - The-Mailroom will vendor the file at its pinned commit and test against it (in review there), so pipeline changes that break the visualizer fail in its CI instead of rendering as `unknown`.
  - `src/tests/test_contract_export.py` fails when the committed file is stale; regenerate with `PYTHONPATH=src python src/scripts/export_contract.py --write`.
  - `release.py cut` moves the contract's `pipeline_version` with the other version locations.
  - `traced_node` tags its wrapper with `mailroom_span_name` so the exporter can read each node's span from the graph.
  - `intake-ml-triage` is now listed in `NODE_OBSERVATION_TYPES` (type `span`, as emitted).

<!-- coderabbit-ai-generated:v1 -->

<!-- coderabbit-ai-generated-section:v1:935f696589a79f921fbcf854c0af7c810b17622f93b1d24985d015cb66bcdf0f -->

- **Audit tables and legacy migration** (roadmap M2 of the unified audit row; `src/storage/audit_migrate.py`). The migration populates `audit_log_legacy` and `audit_meta`; the new row and addendum writers come in M3.
  - New tables: `audit_attempts`, `audit_addenda`, `audit_meta`, `audit_trace_io` and `audit_log_legacy`. `audit_log` itself now has a unique `doc_id` and one global `seq` (no longer counted per document).
  - `ensure_schema()` runs `migrate_audit_log()` once per database, under the `audit_log:chain` write lock and in one transaction. It copies every `audit_log` row into `audit_log_legacy` with a single `INSERT ... SELECT` (so each column keeps its stored value), checks the row count and a digest of every column, and only then empties `audit_log`, adds the unique `doc_id` and `seq` indexes and writes `audit_meta` (`migrated_at`, `legacy_rows`, `legacy_tail_hashes_sha256`). A mismatch aborts with nothing changed. It skips once `audit_meta` says it ran, and two processes starting together migrate once.
  - SQLite is backed up first, to `<db dir>/backups/` (or `backup_dir`). Postgres with rows to move runs only with `MAILROOM_AUDIT_MIGRATE=1`, after your `pg_dump`; without it the migration logs `audit_migration_pending` and changes nothing. An empty `audit_log` needs no opt-in.
  - `python -m storage.audit_migrate` runs the migration by hand; `--rollback` renames the new tables to `*_v2_<timestamp>` and restores the previous release's `audit_log` from `audit_log_legacy` (rows written since stay in the renamed tables).
  - `verify_legacy_chain()` (in the migration module for now) runs the per-document hash check over `audit_log_legacy`; M4 builds the full verifier around it.
  - Interim: until the row writers replace it, the per-event writer `write_audit_entry`, its readers and the warehouse and `verify_audit_chains.py` reads follow the migration through `event_log_model()`: they use `audit_log_legacy` once the migration has run and the old `audit_log` while it is pending. New events written in that window land in `audit_log_legacy` after `audit_meta.legacy_tail_hashes_sha256` was taken.
  - Tests: `src/tests/test_audit_migrate.py` against `src/tests/fixtures/audit/legacy_audit.db`, which `src/scripts/build_legacy_audit_fixture.py` rebuilds with the previous writer. Postgres DDL is compiled for the `postgresql` dialect; the migration runs on SQLite.


<!-- /coderabbit-ai-generated:v1 -->
- **Audit row and addendum writers** (roadmap M3; `src/audit/`). Nothing in the pipeline calls them yet; the terminal nodes adopt them in M7.
  - `write_terminal_row()` writes one `audit_log` row per document with the next global `seq`, under the `audit_log:chain` lock, with its `audit_attempts` sub-rows in the same transaction. A second terminal write for a document becomes a `resumed` addendum. A `seq` conflict is retried a bounded number of times, then raises `AuditWriteError`.
  - `write_addendum()` chains `audit_addenda` per document, starting from the row's hash (kinds `rescore`, `resumed`, `review_decision`, `relations_linked`, `recovered_detail`). It raises `NoAuditRow` when the document has no row.
  - Both refuse with `AuditMigrationPending` until the M2 migration has run. `get_row()` returns a row with its sub-rows and addenda.
  - Row hashes are byte-identical with the dojo's `archive_entry_hash`. Tests: `src/tests/test_audit_writer.py`.

## Changed

- **Dojo pin → v0.28.0** (`cab9768`, llm-dojo-scoring#124), superseding the unreleased v0.27.2 pin (`1cd4aba`, whose fixes 0.28.0 includes). Step G2 of the mailroom-dataset v10 plan ([v10 consumer plan](https://github.com/Exios66/Mailroom-Corpus-EDA/blob/main/docs/plans/v10-consumer-patches.md)). The pin stays an exact commit (the `v0.28.0` tag's commit): `bump_dojo_scoring.py --apply --tag v0.28.0` now resolves a release tag to its commit itself (it used to write the tag, which `test_dojo_source.py` rejects), and the vendored fallback snapshot (`vendor/llm-dojo-scoring`) is the same commit.
  - **Field maps mirror the Dojo.** `taxonomy.yaml` `field_types` for contract, merger, correspondence and insurance equal the Dojo 0.28.0 `DEFAULT_FIELD_TYPES` (the file hashes to blob `44e0ab59`, which the Dojo's `tests/fixtures/taxonomy_field_types.json` pins). `schemas/documents.py`: `ContractExtraction` v2 (17 scored fields: `party_roles`, `signatories`, `agreement_date`, `expiration_date`, `termination_notice_period`, `dispute_forum`, `payment_terms`, `territory` are new; the v1 `merger_consideration` / `maud_clauses` stay as parse-compat fields the Dojo accepts but never scores) and `MergerAgreementExtraction` v2 (21: `acquirer`, `target`, `merger_sub`, `signatories`, `per_share_cash_amount`, `exchange_ratio`, both termination fees, `outside_date`, `financial_advisors`, `legal_counsel`).
  - **Closed labels.** `cuad_family`, `merger_consideration`, merger `intent`, `communication_type`, `urgency`, `claim_type` and `coverage_determination` are `label` fields: aliases fold, then exact match (no containment credit, so `demand` no longer matches `attorney_demand`).
  - **Comparability.** `field_map_sha` changes for those four classes (corporate is unchanged); wrong closed labels score 0 where they had partial credit; contract rows no longer count spurious merger keys as false positives; `schema_valid` follows the larger v2 maps. v9 rows carry no GT for the new v2 fields, so contract and merger headlines read `partial` until v10.
  - `bump_dojo_scoring.py` now also moves the README pin note's short-SHA form (dropping its stale "(the Dojo vX release commit)"), the architecture page's "library (vX," mention, and the "Latest" release rows (only forward, only when the pin is a newer release), which had stayed at v0.27.2.
  - `contract/mailroom-contract.json` regenerated (`export_contract.py --write`).

## Removed

- **Railway deployment.** `railway.json` and the Railway-only `nixpacks.toml` fallback are deleted (Railway stops honouring Config as Code on 2026-12-01; the owner retired the platform, The-Mailroom ADR-005). The hosted producer is the Hugging Face Docker Space paired with The-Mailroom Observatory. The deployment docs now describe the Space and generic container hosts.
  - The `auto` provider's local-Phoenix skip now keys on a Hugging Face Space (`SPACE_ID` / `HF_SPACE_ID`) instead of `RAILWAY_*`, with the same intent: hosted containers have no local collector.
  - The off-loopback bind error names Hugging Face Spaces instead of Railway.
  - `src/tests/test_railway_listen.py` is now `src/tests/test_listen_guards.py` and asserts that no Railway config comes back.

## Fixed

- **Vendored Dojo fallback was missing its package data.** `.gitignore`'s `data/` rule dropped `schemas/data/extraction_status.json` from `vendor/llm-dojo-scoring`, so under the fallback (`MAILROOM_DOJO_SOURCE=vendored`, or a broken install) `extraction_schema()` raised `FileNotFoundError`. The 0.27.2 snapshot includes it, and `.gitignore` re-includes that directory.
- **`load_manifest_cases` crashed from an installed wheel.** It imported `scripts.prepare_samples` before checking for the manifest, and the wheel ships neither, so it raised `ModuleNotFoundError` instead of returning no cases. The import now sits behind the manifest check. `scripts` and `legalbench` stay checkout-only, and the wheel smoke test now asserts that.
- **Warehouse exports could exceed driver bind limits.** `fetch_terminal_documents` and `fetch_audit_rows_for_doc_ids` passed every doc id to one `IN` clause, which goes over the limit at about 32k ids (999 on SQLite before 3.32). Both now query 500 unique ids at a time. Audit rows above the 50-id path stay ordered by doc id, then seq and timestamp, with doc ids now compared in Python's code-point order on every database.
