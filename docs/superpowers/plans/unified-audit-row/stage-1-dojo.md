# Stage 1: dojo 0.27.0, tiered document scorers

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the sandbox's complete per-document scoring into the dojo as a registry of per-type scorers, and widen `score_archive_block` additively. Release this as 0.27.0.

**Architecture:** Tier 0 (`common.py`) handles every document. Tier 1/2 live in one module per document type behind a `DocScorer` protocol, and `score_document` dispatches through `DOC_SCORERS`. The code moves without changing any rule: goldens captured from the sandbox before the move prove every number is unchanged.

**Tech Stack:** Python 3.11, uv, pytest, ruff. Work on branch `claude/doc-scorers-0.27` in `llm-dojo-scoring`.

**Spec:** `docs/superpowers/specs/2026-11-03-unified-audit-row-design.md` (§3, §3.1, §8) in llm-mailroom.

## Global Constraints

- Additive only: no existing public name, parameter or key changes meaning. `overall_score` stays the field-map mean; `schema_valid` stays "every live-map key present"; `status`/`reason` unchanged.
- Never import mailroom from the dojo. Pydantic models come in through `schema_lookup`.
- `gt_metadata` and `corporate_record_*` names (used by Mailroom-Corpus-EDA) are unchanged.
- Never hand-edit versions; `uv.lock` is generated; new module paths need no legacy alias, but existing aliases in `_layout.LEGACY_MODULES` must keep working.
- Gate per task: `uv run --extra dev pytest tests -q` and `uvx ruff check llm_dojo_scoring tests scripts examples` pass.
- Any change to public names: update `tests/fixtures/consumer_surface.json` scan expectations and `docs/guides/PUBLIC_API.md` (guarded by `tests/test_public_api_doc.py`).

## Review Focus

- A predicted payload that is prose, a JSON string, `None` or a list → `score_document` returns adherence with `parse_error` and a block with `status` set; it never raises.
- `doc_type` given as an alias or specialist name (`contracts_specialist`, `Contract`) → resolves through `DOC_TYPE_ALIASES` to the same scorer.
- A subclass surface not in `DOC_TYPE_SUBCLASSES` (e.g. `License_Agreements`, `voicemail`) → normalises to the canonical key or `other`, never `None`, when the type has subclasses.
- MAUD/CUAD labels present but the prediction has none of the clause keys → headline is the benchmark's 0.0, not a field-map fallback.
- An insurance row with a CMS subclass and no `cms_source_table` in ground truth → the derived table is scored as today (dojo #93).

---

### Task D1: Golden inputs and sandbox goldens

**Repo:** llm-dojo-scoring
**Spec:** §3, §8
**Depends:** R0

**Files:**
- Create: `tests/fixtures/doc_scorers/inputs.json`. These are synthetic rows `{case_id, doc_type, doc_subclass, expected_subclass, predicted, expected, doc_text}` and contain no corpus text.
- Create: `scripts/capture_sandbox_goldens.py`. It runs inside a sandbox checkout's venv: `python scripts/capture_sandbox_goldens.py --sandbox <path> --out tests/fixtures/doc_scorers/goldens.json`.
- Create: `tests/fixtures/doc_scorers/goldens.json`. It holds `{"sandbox_commit": ..., "cases": {case_id: {"payload": score_extraction_row(...), "adherence": assess_extraction_payload(...)}}}`.
- Create: `tests/test_doc_scorer_fixtures.py`.

- [x] **Step 1: Write `test_inputs_cover_every_type_and_subclass`.** For every type in `CORPUS_DOC_TYPES`, every key in `DOC_TYPE_SUBCLASSES[type]` and every value in `CORPUS_SUBCLASS_SURFACES[type]` appears in at least one case. The test also requires at least one case each for: MAUD labels, CUAD labels, no ground truth, a prose payload, a `None` payload, a CMS insurance row without `cms_source_table`, and a corporate row with no subclass.
- [x] **Step 2: Run it.** Expected: FAIL (no fixture).
- [x] **Step 3: Write the inputs and the capture script.** The script loads the sandbox's `mailroom_sandbox.eval.scoring.score_extraction_row` and `mailroom_sandbox.eval.schema_adherence.assess_extraction_payload`. It records `git -C <sandbox> rev-parse HEAD`, the dojo version actually imported, and the per-case pydantic verdict (`typed_valid`), and it writes JSON with `sort_keys=True`. Load it in tests via `tests/conftest.py:load_script`.
- [x] **Step 4: Capture** with the sandbox at its current `main`, but with this dojo checkout at the v0.26.0 tag installed over its vendored 0.25.0 (`pip install -e <dojo>` into the sandbox venv; review item A15). That way the goldens hold the sandbox's logic on today's dojo internals. Then run the fixture test. Expected: PASS. Also add `test_goldens_match_inputs`, which checks that every case has a golden and that `dojo_version == "0.26.0"`.
- [x] **Step 5: Commit** (`test: golden inputs and sandbox goldens for doc scorers`). (Exios66/llm-dojo-scoring#116, `0a7d14d`, `e72033b`)

### Task D2: `assess_extraction_payload` in the dojo

**Repo:** llm-dojo-scoring
**Spec:** §3
**Depends:** D1

**Files:**
- Modify: `llm_dojo_scoring/pipeline/sandbox_schema_adherence.py`. Add `coerce_predicted_payload`, `schema_keys_for` and `assess_extraction_payload`, moved from the sandbox's `eval/schema_adherence.py`, next to the existing `assess_extraction_facts`.
- Test: `tests/test_sandbox_schema_adherence.py`

**Interfaces:**
- Produces: `assess_extraction_payload(predicted: Any, doc_type: str, doc_subclass: str | None = None, *, schema_lookup: Callable[[str, str | None], type | None] | None = None) -> dict`. It returns exactly the keys `parse_error`, `schema_valid`, `schema_adherence`, `parse_failure_reason`, `extra_schema_keys`, `legacy_schema_keys`, `prose_wrapper`, `recovered_from_raw`. With no `schema_lookup`, `schema_valid` uses the structural fallback already in `assess_extraction_facts`. Corporate subclasses use `corporate_record_schema.entity_field_types` as today.

- [x] **Step 1: Write `test_adherence_matches_goldens`.** For every case it calls the function with `GoldenSchemaLookup`, a test stub that returns a model whose `model_validate` replays the case's recorded `typed_valid`, so the dojo tests never import mailroom (review item A16). The result must equal the golden `adherence`. Also add `test_adherence_without_lookup_never_raises`, which covers prose, `None`, a list and a JSON string.
- [x] **Step 2: Run them.** Expected: FAIL.
- [x] **Step 3: Move the code.** The sandbox's direct `from schemas.documents import get_extraction_schema` becomes the `schema_lookup` call.
- [x] **Step 4: Run them.** Expected: PASS. Run the full gate.
- [x] **Step 5: Commit.** (Exios66/llm-dojo-scoring#116, `ba5f2a0`)

### Task D3: Tier 0 common scorer and extraction scoping

**Repo:** llm-dojo-scoring
**Spec:** §3, §3.1
**Depends:** D1

**Files:**
- Create: `llm_dojo_scoring/scoring/doc_scorers/__init__.py` (empty for now)
- Create: `llm_dojo_scoring/scoring/extraction_scope.py`. It holds `is_empty_gt`, `applicable_expected_fields`, `scope_predicted_fields` and `scope_extraction_pair`, moved from the sandbox's `eval/extraction_scope.py` together with the Hub alias table.
- Create: `llm_dojo_scoring/scoring/doc_scorers/common.py`
- Test: `tests/test_doc_scorers_common.py`

**Interfaces:**
- Produces: `score_common(doc_type: str, predicted: Mapping | None, expected: Mapping | None, *, doc_subclass: str | None, doc_text: str | None, detailed: bool) -> dict`. It returns the sandbox payload's tier-0 keys: scoped fields, suite `overall_extraction_score`, `scoring_method` (sandbox name), the PRF keys (`extraction_precision`, `extraction_recall`, `extraction_f1`, `extraction_f2`, `entity_list_f1`), `unlabeled_fields`, `status`, `reason`. `detailed` selects `suite.score(detailed=True)`.

- [ ] **Step 1: Write `test_common_matches_golden_tier0_keys`.** For every case, the tier-0 keys must equal the golden payload's. A key a type module later overrides (`overall_extraction_score` and `scoring_method` for MAUD/CUAD cases) is compared against `suite_overall_extraction_score`.
- [ ] **Step 2: Run it.** Expected: FAIL.
- [ ] **Step 3: Implement** by moving the code from the sandbox's `score_extraction_row`, from the start up to just before the MAUD/CUAD branch.
- [ ] **Step 4: Run it.** Expected: PASS. Run the full gate.
- [ ] **Step 5: Commit.**

### Task D4: `DocScorer` protocol, registry and `generic`

**Repo:** llm-dojo-scoring
**Spec:** §3.1
**Depends:** D3

**Files:**
- Modify: `llm_dojo_scoring/scoring/doc_scorers/__init__.py`
- Create: `llm_dojo_scoring/scoring/doc_scorers/generic.py`
- Test: `tests/test_doc_scorers_registry.py`

**Interfaces:**
- Produces:
  - `class DocScorer(Protocol)` with `doc_type: str`, `subclasses: tuple[str, ...]`, `detailed: bool` (whether tier 0 uses the detailed suite path), `subclass(raw: Any) -> str | None`, `extras(predicted, raw_expected, *, subclass: str | None, doc_text: str | None) -> dict` and `headline(common: dict, extras: dict) -> tuple[float | None, str | None]`. `headline` returns `(score, metric_name)`; `(None, None)` means no headline override.
  - `DOC_SCORERS: dict[str, DocScorer]` and `get_doc_scorer(doc_type: str) -> DocScorer`, which resolves aliases through `DOC_TYPE_ALIASES` and falls back to `generic`.

- [ ] **Step 1: Write the tests.**
  - `test_every_native_type_resolves`: each `NATIVE_DOC_TYPES` entry returns a scorer whose `doc_type` is that type, or `generic` for the three corpus-absent types until they get modules.
  - `test_unknown_type_is_generic`.
  - `test_aliases_resolve`: `contracts_specialist` → contract scorer, case-insensitive.
  - `test_registry_doc_types_match_subclass_table`: every registered type's `subclasses` equals `DOC_TYPE_SUBCLASSES[type]`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the protocol, the registry and `generic`. `generic` has no subclasses, `detailed=False`, empty extras and no headline.
- [ ] **Step 4: Run them.** Expected: PASS (only `generic` is registered so far; the type tests are parametrised over registered types plus the generic fallback).
- [ ] **Step 5: Commit.**

### Task D5: Type modules for contract and merger (benchmark headlines)

**Repo:** llm-dojo-scoring
**Spec:** §3.1
**Depends:** D4

**Files:**
- Create: `llm_dojo_scoring/scoring/doc_scorers/contract.py`. Its extras are `score_sandbox_cuad` when `cuad_clause_labels` is present, and its headline is `cuad_presence_f1` with metric `+cuad`.
- Create: `llm_dojo_scoring/scoring/doc_scorers/merger_agreement.py`. Its extras are `score_sandbox_maud` when `maud_clause_labels` is present, and its headline is `maud_accuracy` with metric `+maud`.
- Modify: the registry
- Test: `tests/test_doc_scorers_types.py`

- [ ] **Step 1: Write `test_type_parity[contract|merger_agreement]`.** For each case of the type, `score_common` plus the module's extras and headline must reproduce the golden payload exactly, including `suite_overall_extraction_score`, `overall_extraction_score` and `scoring_method`. Add `test_benchmark_labels_without_clause_keys_score_zero` (Review Focus).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement both modules.** Subclass normalisation goes through `normalize_corpus_subclass`.
- [ ] **Step 4: Run.** Expected: PASS, then the full gate.
- [ ] **Step 5: Commit.**

### Task D6: Type modules for corporate record, correspondence and insurance claim

**Repo:** llm-dojo-scoring
**Spec:** §3.1
**Depends:** D4

**Files:**
- Create: `llm_dojo_scoring/scoring/doc_scorers/corporate_record.py`, with `detailed=True` and per-record-type field alignment.
- Create: `llm_dojo_scoring/scoring/doc_scorers/correspondence.py`. Its extras are `score_correspondence_content` (topic and sentiment) when the ground truth carries them.
- Create: `llm_dojo_scoring/scoring/doc_scorers/insurance_claim.py`. Its extras are `score_claims_extras` and the canonical `claim_type`. The tier-2 hook derives `cms_source_table`.
- Modify: the registry
- Test: `tests/test_doc_scorers_types.py`

- [ ] **Step 1: Write `test_type_parity[corporate_record|correspondence|insurance_claim]`** the same way as D5. Add `test_unknown_subclass_surface_normalises` (Review Focus) and `test_cms_row_without_table_scores_derived_table` (Review Focus).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement the three modules.**
- [ ] **Step 4: Run.** Expected: PASS, then the full gate.
- [ ] **Step 5: Commit.**

### Task D7: `score_document` dispatcher and wider `score_archive_block`

**Repo:** llm-dojo-scoring
**Spec:** §3, §3.1
**Depends:** D2, D5, D6

**Files:**
- Create: `llm_dojo_scoring/scoring/doc_scorers/dispatch.py`
- Modify: `llm_dojo_scoring/pipeline/archive.py` (`score_archive_block`)
- Modify: `llm_dojo_scoring/__init__.py` (exports), `docs/guides/PUBLIC_API.md`, `tests/fixtures/consumer_surface.json`, `CHANGELOG.md` (`[Unreleased]` Added)
- Test: `tests/test_score_document.py`, `tests/test_archive_scoring.py`

**Interfaces:**
- Produces:
  - `score_document(doc_type: str, predicted: Any, expected: Mapping | None, *, doc_text: str | None = None, doc_subclass: str | None = None, expected_subclass: str | None = None, schema_lookup=None) -> dict`. It returns the golden payload keys plus `scorer: {doc_type, module, subclass, tiers_run}`.
  - `score_archive_block(..., doc_subclass: str | None = None)` is the only new parameter; `doc_text` and `expected_subclass` already exist. New block keys:
    - `headline_score` and `headline_metric` (null unless a type module sets a headline);
    - `unlabeled_fields`;
    - `entity_list_f1`;
    - `scorer`.
  - Unchanged keys: `overall_score` (field-map mean), `method` (gets `+maud`/`+cuad` through the existing `method=` path), `schema_valid`, `status`, `reason`, `trace`.

- [ ] **Step 1: Write the tests.**
  - `test_score_document_matches_goldens`: every case reproduces the golden payload byte-identically, with `scorer` removed.
  - `test_archive_block_existing_keys_unchanged`: run `score_archive_block` on every case with and without `doc_subclass`. Every key that existed before compares equal to a snapshot taken at the start of this task (committed in Step 1 as `tests/fixtures/doc_scorers/archive_block_before.json`).
  - `test_archive_block_headline`: for MAUD/CUAD cases, `headline_score` equals the golden `overall_extraction_score` and `overall_score` equals the golden `suite_overall_extraction_score`.
  - `test_no_gt_is_unscorable`.
  - `test_score_document_never_raises_on_bad_payload` (Review Focus).
  - `test_corpus_eda_names_unchanged`: `gt_metadata` and every `corporate_record_*` export still resolve with their current signatures (spec §8).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the dispatcher (tier 0 → type extras → headline) and the archive-block widening.
- [ ] **Step 4: Run them**, plus `tests/test_public_api_doc.py` and the consumer-surface tests. Expected: PASS. Then run the full gate.
- [ ] **Step 5: Commit.**

### Task D8: Release 0.27.0

**Repo:** llm-dojo-scoring
**Spec:** §9
**Depends:** D7

- [ ] **Step 1: Run** `python scripts/release.py cut 0.27.0` (dry run), then `--apply`, then `uv lock`. Expected: `release.py check` prints OK.
- [ ] **Step 2: Open the PR** (owner asks) and get it merged.
- [ ] **Step 3: Publish** with `python scripts/release.py publish 0.27.0` from the merge commit. The release fires `repository_dispatch: dojo-scoring-released` to llm-mailroom.
- [ ] **Step 4: Tick D1–D8 here** in a follow-up PR that links the merged PRs.
