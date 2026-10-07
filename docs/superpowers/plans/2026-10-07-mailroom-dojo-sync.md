# Mailroom ↔ Dojo Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **This file is committed byte-identical in both repositories**
> (`dojo-docs:NAME.md` means the file NAME.md in the `docs` folder of `llm-dojo-scoring` — spelled this way so
> mailroom's dangling-docs-reference test does not read it as a local path)
> (`Exios66/llm-mailroom` and `Exios66/llm-dojo-scoring`, same path
> `docs/superpowers/plans/2026-10-07-mailroom-dojo-sync.md`). Tick boxes in the
> copy of the repo the task lives in, and mirror the tick into the other copy
> in the same PR when convenient. Each task header names its repo.

**Goal:** Bring `llm-mailroom@main` and `llm-dojo-scoring@main` to one green,
mutually-pinned working state where the dojo scores exactly what the live
pipeline emits today (controlled `intent` labels, frozen-v1 specialist prompts,
current taxonomy) and mailroom consumes a dojo release that knows all of it.

**Architecture:** Four phases with one hard ordering constraint: mailroom's
taxonomy may only declare a field type that its pinned dojo already scores.
Phase 1 greens mailroom on the current pin (v0.19.1) and adopts the corrected
frozen prompts the dojo already ships. Phase 2 teaches the dojo the
controlled-intent `label` type, re-vendors the `production` prompt family from
the Phase-1 mailroom, and cuts **v0.20.0**. Phase 3 bumps mailroom to v0.20.0,
switches `intent` to `label`, and deletes mailroom's duplicate vocabulary.
Phase 4 re-pins the dojo's taxonomy fixture to the Phase-3 blob.

**Tech Stack:** Python 3.11 (mailroom ≥3.11, dojo ≥3.10), pytest, setuptools
editable installs, `uv` (optional). No linter/formatter/typechecker is
configured in either repo — do not add one.

**Spec:** This plan's own **Drift audit** section below (no separate design
doc). It was produced 2026-10-07 by installing both repos editable into one
venv (`llm-dojo-scoring@fe7aba3`, `llm-mailroom@bee7f46`) and running both test
suites plus prompt/schema/scoring probes.

## Drift audit (2026-10-07) — the spec

| # | Finding | Evidence |
|---|---|---|
| D1 | Mailroom `main` is red on the current pin: 12 failed / 1467 passed. 10 are GitBook/docs-truth drift from `GITBOOK-SITE:` web-editor commits (stale changelog pages, `mailroom-dataset/` pages missing from `docs/SUMMARY.md`, undefined `mailroom-docs` site node, stale `v0.18.0`/`0.14.0` dojo pins in `docs/pipeline-reference-llm-mailroom/{architecture,agents}.md`, missing dojo badge / `**Honest gap (dojo 0.19.1):**` copy, missing `<iframe>` in the Corpus-EDA visuals page). 1 is `test_specialists.py::test_insurance_claims_specialist_constructs_and_builds_schema` asserting `"insurance claim"` while the frozen v1 prompt says `"insurance-claims"`. 1 (`test_audit_log.py::test_concurrent_appends_keep_chain_valid`) failed only in the full run and passed alone. | `pytest -q` in mailroom |
| D2 | Mailroom serves **uncorrected** frozen v1 bytes for two specialists. Dojo `production_prompts` carries the 2026-10-05 corrections (`communication_type` → `null` not `other`; `filing_number` null unless an official number). | `src/llm/frozen_v1/corporate_records_specialist.txt` sha256 `484e64dd…` vs dojo `fe13501f…`; `correspondence_specialist.txt` `eab63b5a…` vs dojo `2b0b81ff…`. Other three identical. |
| D3 | Dojo `family="production"` no longer matches what mailroom runs. Specialists: dojo vendors pre-v1 constants (`contracts_specialist_v32`, 32 kB, still instructing the retired `key_obligations` / `termination_clauses` / `key_provisions` / `key_points` / `referenced_communications`); mailroom runs frozen v1. `sorter`, `boss`, `arbiter`, `sorter_reviewer`, `judge`, `judge-classification` still list the retired `compliance_filing` class instead of `merger_agreement`. `reporter` is now deterministic in mailroom (dojo still has the LLM prompt). `image_extractor` lacks the production doctrine block. `gmail_triage` and `relations` have no dojo entry. Only `judge-correctness` and `pdf_transcriber` match. | `mailroom llm.prompts.prompt_templates()` vs `get_prompt(agent)` |
| D4 | Mailroom `bee7f46` made `intent` a controlled per-class vocabulary (`doc_inventories.INTENT_LABELS` + `normalize_intent`, canonicalized in `enrich_extraction`). Dojo has no vocabulary and scores `intent` as fuzzy `name`, so wrong labels get partial credit and the scorer cannot tell an alias from a miss. | `correspondence` suite: `notice`→`other` = **0.5455**, `request`→`update` = **0.3077** (both must be 0.0). |
| D5 | Dojo's live-roster fixture pins taxonomy blob `ca297bd8…`; mailroom `main` is `898b0efa…` (added `gateway:`, GPU-tier `cost_models`, `vision.exclude`). Field types unchanged, so only the pin is stale — until Phase 3 changes `intent`. No generator exists for the fixture. | `git hash-object src/config/taxonomy.yaml` |
| D6 | The two repos cannot share one environment: mailroom's editable install puts `src/` on `sys.path`, so its `scripts` package shadows the dojo's repo-root `scripts/`, and `tests/test_gen_maud_catalog.py` + `tests/test_verify_gt_penalties.py` fail to collect. Mailroom's `pyproject.toml` declares the dojo as a `uv` workspace member, so this is the supported dev layout. | `ImportError: cannot import name 'gen_maud_catalog' from 'scripts' (…/llm-mailroom/src/scripts/__init__.py)` |
| D7 | Dojo `main` has one unreleased commit (`dojo-docs:EXTRACTION_SCHEMAS.md`) whose `intent` rows and "taxonomy blob" pin will be wrong after this plan. `dojo-docs:TODOS.md` "Downstream importers" items are exactly D2. | `git log v0.19.1..main` |

Verified as **not** drift (no task): money fields — dojo `parse_money` accepts
mailroom's new numeric `demand_amount` / `claimed_amount` (`218440.0`, `0`), and
a `null`-GT / `0.0`-prediction spurious fill already drops field precision to
0.5. Dojo suite: 707 passed / 5 skipped once D6 modules are excluded.

## Global Constraints

- Dojo release produced by this plan: **`v0.20.0`** (minor: new field type + new public module + `production` family content change). `pyproject.toml` `version` and `llm_dojo_scoring/__init__.py` fallback `__version__` both say `0.20.0`.
- Mailroom pin string after Phase 3: `llm-dojo-scoring @ git+https://github.com/Exios66/llm-dojo-scoring.git@v0.20.0` — changed **only** via `PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --apply --tag v0.20.0`, never by hand.
- Mailroom pin stays `v0.19.1` throughout Phase 1.
- Intent vocabularies (exact, ordered — copied from mailroom `doc_inventories.INTENT_LABELS` at `bee7f46`):
  - `corporate_record`: `governance_rules`, `corporate_action_approval`, `entity_formation`, `authority_delegation`, `investor_rights`, `other`
  - `correspondence`: `payment_demand`, `notice`, `analysis`, `request`, `update`, `meeting_invite`, `press_communication`, `other`
  - `insurance_claim`: `claim_filing`, `coverage_determination`, `loss_report`, `claim_data_record`, `other`
  - `merger_agreement`: **no** vocabulary (its `intent` stays free-form `name`; `contract` has no `intent` field).
- `normalize_intent` contract (unchanged from mailroom): result is `""` or a member of that class's own vocabulary; never another class's token; never invents `other`.
- Frozen v1 specialist bytes are immutable except through the dojo `production_prompts` catalog; mailroom never hand-edits prompt text.
- Do not edit `docs/changelog/**` by hand in mailroom — regenerate with `PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py`.
- Tagging/releasing (`git tag`, GitHub Release, `repository_dispatch`) is outward-facing: **stop and get the human's go-ahead** at Task 7 Step 6.
- Commits end with the session's attribution trailer if the executor has one.

## Review Focus

1. **Correspondence with no registered form + sorter subclass `other`** — the corrected v1 prompt makes the model emit `communication_type: null`; mailroom `enrich_extraction` must not turn that back into `other` from the subclass. Expect `null`. Test lives in Task 2.
2. **Intent from another class's vocabulary** (e.g. `entity_formation` predicted on a correspondence) — the dojo `label` scorer must give 0.0, not a fuzzy partial. Test lives in Task 4.
3. **GT `intent` is `null` and prediction is `"other"`** — a spurious fill, scored like any other spurious scalar (precision drops), never as a match. Test lives in Task 4.
4. **Historical archives scored with an explicit `field_types` map that says `intent: name`** — rescoring old runs must keep the old fuzzy behaviour; `label` applies only when the type map says so. Test lives in Task 4.
5. **Mailroom and dojo installed together in one venv** (the `uv` workspace layout) — both suites must collect and pass. Test lives in Task 3 (dojo side) and Task 10 (whole-system check).

---

## Phase 1 — mailroom green on v0.19.1 (repo: `llm-mailroom`)

### Task 1: Green the mailroom docs-truth and specialist tests [llm-mailroom]

**Files:**
- Modify: `docs/SUMMARY.md` (add the `## Mailroom dataset` section with `* [Overview](mailroom-dataset/README.md)` and the six child pages the test lists)
- Modify: `gitbook-docs.yaml` and/or `.gitbook.yaml` (define a `path` for the `mailroom-docs` site node — read `test_gitbook_site_structure_imports_and_publishes_changelog` for the exact expectation)
- Modify: `docs/pipeline-reference-llm-mailroom/architecture.md`, `docs/pipeline-reference-llm-mailroom/agents.md` (stale `v0.18.0` / `0.14.0` → `v0.19.1`; restore the two `**Honest gap (dojo 0.19.1):**` callouts in `docs/agents.md`'s GitBook twin)
- Modify: `docs/README.md` (restore the dojo badge `[![Dojo](https://img.shields.io/badge/dojo-v0.19.1-6f42c1)](https://github.com/Exios66/llm-dojo-scoring/releases/tag/v0.19.1)` and the ported landing block `test_gitbook_home_ports_the_enhanced_landing` expects)
- Modify: the Corpus-EDA visuals page `test_gitbook_toc_nests_docker_modal_and_sandbox_reports` reads (restore `<iframe src="https://exios66.github.io/Mailroom-Corpus-EDA/"`)
- Regenerate: `docs/changelog/**` via the sync script
- Modify: `src/tests/test_agents/test_specialists.py:84` (`"insurance claim"` → `"insurance-claims"`, matching the frozen v1 opening `You are the insurance-claims specialist.`)

**Interfaces:**
- Consumes: nothing.
- Produces: green `pytest -q` on mailroom at pin v0.19.1 — every later mailroom task assumes it.

- [ ] **Step 1: Reproduce the 12 failures**

Run: `pytest -q -p no:cacheprovider src/tests/test_docs_truth.py src/tests/test_landing.py src/tests/test_agents/test_specialists.py src/tests/test_audit_log.py`
Expected: the 11 deterministic failures from D1 (audit-log concurrency may pass).

- [ ] **Step 2: Fix the docs to satisfy each assertion; regenerate the changelog**

Restore content the GitBook web edits dropped — prefer `git log -p -- <file>` to recover the previous text over rewriting. Then:
Run: `PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py && PYTHONPATH=src python src/scripts/sync_gitbook_changelog.py --check`
Expected: `--check` exits 0.

- [ ] **Step 3: Fix the specialist test assertion** (test-only change; the prompt bytes are frozen and correct).

- [ ] **Step 4: Audit-log concurrency check**

Run: `for i in 1 2 3 4 5; do pytest -q -p no:cacheprovider src/tests/test_audit_log.py::test_concurrent_appends_keep_chain_valid || break; done` then the full suite once.
Expected: passes every time. If it fails, it is real (not a flake): use superpowers:systematic-debugging on `src/storage/audit_log.py`'s append locking and fix it here, with the failing interleaving captured as a test. Never skip or mark it.

- [ ] **Step 5: Full suite**

Run: `pytest -q -p no:cacheprovider`
Expected: `0 failed`.

- [ ] **Step 6: Commit**

```bash
git add docs src/tests/test_agents/test_specialists.py gitbook-docs.yaml .gitbook.yaml
git commit -m "fix: restore GitBook-dropped docs truth and green main on dojo v0.19.1"
```

### Task 2: Serve specialists from the dojo `production_prompts` catalog [llm-mailroom]

Closes D2 and the dojo `dojo-docs:TODOS.md` "Downstream importers" items.

**Files:**
- Modify: `src/llm/frozen_v1/__init__.py` (`load_specialist_v1`, `specialist_sha256`)
- Delete: `src/llm/frozen_v1/{contracts,corporate_records,correspondence,insurance_claims,merger_agreement}_specialist.txt`
- Modify: `src/llm/frozen_v1/lineage.json` (sha256/chars for the two corrected rows; add `"source": "llm-dojo-scoring production_prompts"`, `"refrozen_at": "2026-10-05"` on those two; keep `frozen_at`)
- Modify: `pyproject.toml` `[tool.setuptools.package-data]` `"llm.frozen_v1"` → `["lineage.json", "README.md"]`
- Modify: `src/llm/frozen_v1/README.md`
- Test: `src/tests/test_frozen_v1_prompts.py`, `src/tests/test_doc_inventories.py`

**Interfaces:**
- Consumes: `llm_dojo_scoring.prompts.get_prompt(agent, family="production_prompts").text -> str` (exists at v0.19.1).
- Produces: `load_specialist_v1(agent_name: str) -> str` returns the dojo text verbatim; `specialist_sha256(agent_name: str) -> str` hashes `text.encode("utf-8")`. `prompt_templates()` callers are unchanged.

- [ ] **Step 1: Write the failing tests**

```python
# src/tests/test_frozen_v1_prompts.py
from llm_dojo_scoring.prompts import get_prompt
from llm.frozen_v1 import SPECIALIST_AGENTS, load_specialist_v1, specialist_sha256

def test_specialists_are_dojo_production_prompts_verbatim():
    for agent in SPECIALIST_AGENTS:
        assert load_specialist_v1(agent) == get_prompt(agent, family="production_prompts").text

def test_corrected_records_are_served():
    assert specialist_sha256("corporate_records_specialist").startswith("fe13501f")
    assert specialist_sha256("correspondence_specialist").startswith("2b0b81ff")
```

Update `test_frozen_v1_sha256_matches_lineage_catalog` so lineage rows equal `specialist_sha256(agent)` for all five. Confirm the `fe13501f`/`2b0b81ff` prefixes against the hashing rule in dojo `dojo-docs:PROMPTS.md` ("hash over the loaded text plus a trailing newline") — use whichever rule makes `contracts_specialist` hash to `d91de396…` and apply the same to all five.

```python
# src/tests/test_doc_inventories.py  (Review Focus 1)
def test_null_communication_type_with_other_subclass_stays_null():
    out = enrich_extraction({"communication_type": None}, doc_type="correspondence", subtype="other")
    assert out.get("communication_type") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest -q src/tests/test_frozen_v1_prompts.py src/tests/test_doc_inventories.py -k "dojo_production or corrected or null_communication"`
Expected: FAIL on the two sha prefixes / verbatim equality (and on the subclass test if `enrich_extraction` maps `other`).

- [ ] **Step 3: Implement** — `load_specialist_v1` returns `get_prompt(agent_name, family="production_prompts").text`; delete the five `.txt` files and their package-data entry; refresh `lineage.json`. If the Review-Focus-1 test fails, make `enrich_extraction` skip the subclass fallback when the subclass normalizes to `other` for `communication_type` (the corrected prompt's contract).

- [ ] **Step 4: Run the prompt + inventory + specialist tests, then the full suite**

Run: `pytest -q src/tests/test_frozen_v1_prompts.py src/tests/test_doc_inventories.py src/tests/test_prompts.py src/tests/test_agents && pytest -q -p no:cacheprovider`
Expected: `0 failed`.

- [ ] **Step 5: Commit**

```bash
git add -A src/llm/frozen_v1 pyproject.toml src/tests src/langchain_agents/doc_inventories.py
git commit -m "feat: serve frozen v1 specialists from dojo production_prompts (corrected records)"
```

Note for the operator (not a code step): after merge, `PYTHONPATH=src python src/scripts/sync_prompts.py` pushes the two corrected prompts to Langfuse `mailroom-<agent>` `production`.

---

## Phase 2 — dojo learns what the pipeline emits (repo: `llm-dojo-scoring`)

### Task 3: Make dojo tests co-install-safe with mailroom [llm-dojo-scoring]

Closes D6.

**Files:**
- Modify: `tests/conftest.py` (add `load_script`)
- Modify: `tests/test_gen_maud_catalog.py:11`, `tests/test_verify_gt_penalties.py:11`
- Test: `tests/test_conftest_scripts.py` (new)

**Interfaces:**
- Produces: `load_script(name: str) -> types.ModuleType` in `tests/conftest.py` — loads `<repo>/scripts/<name>.py` via `importlib.util.spec_from_file_location` under module name `dojo_scripts_<name>`, registers it in `sys.modules`, returns it. Tests use `generator = load_script("gen_maud_catalog")` instead of `from scripts import …`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_conftest_scripts.py
import sys, types
from conftest import load_script

def test_load_script_ignores_a_shadowing_scripts_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "scripts", types.ModuleType("scripts"))
    mod = load_script("gen_maud_catalog")
    assert mod.__file__.endswith("scripts/gen_maud_catalog.py")
```

- [ ] **Step 2: Run to verify it fails** — `pytest -q tests/test_conftest_scripts.py` → FAIL (`load_script` not defined).
- [ ] **Step 3: Implement `load_script`; switch both test modules to it.** Leave the existing `sys.path.insert` in place for other callers.
- [ ] **Step 4: Verify in the shared layout**

Run (from a venv with `pip install -e ../llm-mailroom[dev]` and `pip install -e .[dev]`): `python -m pytest -q -p no:cacheprovider`
Expected: `0 failed`, `0 errors`, ≥ 712 passed (707 + the two re-collected modules' tests + this one).

- [ ] **Step 5: Commit** — `git commit -am "test: load scripts/ helpers by path so a co-installed mailroom cannot shadow them"` (add the new test file).

### Task 4: Controlled-intent vocabulary and the `label` field type [llm-dojo-scoring]

Closes D4.

**Files:**
- Create: `llm_dojo_scoring/intents.py`
- Modify: `llm_dojo_scoring/field_scoring.py` (`FIELD_SCORERS` at ~L1119; type bands at ~L101; `score_extraction` per-field dispatch)
- Modify: `llm_dojo_scoring/__init__.py` (re-export `INTENT_LABELS`, `normalize_intent`)
- Test: `tests/test_intents.py` (new), `tests/test_field_scoring.py`

**Interfaces:**
- Produces (all importable from `llm_dojo_scoring` and `llm_dojo_scoring.intents`):
  - `INTENT_LABELS: dict[str, tuple[str, ...]]` — exactly the Global Constraints vocabularies, same order.
  - `INTENT_ALIASES: dict[str, str]` — port mailroom `doc_inventories._INTENT_ALIASES` at `bee7f46` verbatim (public name in the dojo).
  - `normalize_intent(doc_type: str | None, value: Any) -> str` — port of mailroom `normalize_intent` + its `_normalize`/`_compact` helpers (longest-key-wins, ≥8-char alias containment, `other` never matched by containment), with the in-class guard.
  - `INTENT_DESCRIPTIONS: dict[str, str]` — port mailroom's (Phase 3 imports it).
  - Field type `"label"` in `FIELD_SCORERS`: `score_label_field(pred, exp, embedding=None) -> float` = 1.0 when both compact (lowercase, alnum-only) to the same non-empty token, else 0.0. Never ambiguous (band excludes it from `field_is_ambiguous`).
  - In `score_extraction(doc_class, field_types, predicted, expected, doc_text=None)`, when `field_types.get("intent") == "label"` and `doc_class in INTENT_LABELS`, both sides of `intent` are replaced by `normalize_intent(doc_class, v) or v` before dispatch.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_intents.py
from llm_dojo_scoring import INTENT_LABELS, normalize_intent, get_suite
from llm_dojo_scoring.field_scoring import score_extraction

def test_vocabularies_match_mailroom_bee7f46():
    assert INTENT_LABELS["correspondence"] == ("payment_demand", "notice", "analysis", "request", "update", "meeting_invite", "press_communication", "other")
    assert INTENT_LABELS["corporate_record"][-1] == "other" and len(INTENT_LABELS["corporate_record"]) == 6
    assert INTENT_LABELS["insurance_claim"] == ("claim_filing", "coverage_determination", "loss_report", "claim_data_record", "other")
    assert "merger_agreement" not in INTENT_LABELS and "contract" not in INTENT_LABELS

def test_aliases_resolve_in_class_only():
    assert normalize_intent("correspondence", "demand_payment") == "payment_demand"
    assert normalize_intent("insurance_claim", "notice_of_loss") == "claim_filing"
    assert normalize_intent("corporate_record", "request_information") == ""
    assert normalize_intent("correspondence", "threaten litigation") == ""

LABEL = {"intent": "label", "sender": "name"}

def _intent_score(doc, exp, pred, types=LABEL):
    # signature: score_extraction(doc_class, field_types, predicted, expected, doc_text=None)
    return score_extraction(doc, types, {"sender": "A", "intent": pred}, {"sender": "A", "intent": exp}).field_scores.get("intent")

def test_label_scoring_is_exact_after_canonicalization():
    assert _intent_score("correspondence", "payment_demand", "demand_payment") == 1.0
    assert _intent_score("correspondence", "notice", "other") == 0.0
    assert _intent_score("correspondence", "request", "update") == 0.0

def test_foreign_class_token_scores_zero():          # Review Focus 2
    assert _intent_score("correspondence", "notice", "entity_formation") == 0.0

def test_null_gt_other_prediction_is_spurious_fill():  # Review Focus 3
    out = get_suite("correspondence_specialist").score_document(
        {"sender": "A", "intent": None}, {"sender": "A", "intent": "other"}, field_types=LABEL)
    assert out["extraction_precision"] < 1.0

def test_explicit_name_type_keeps_fuzzy_rescoring():   # Review Focus 4
    assert 0.0 < _intent_score("correspondence", "notice", "other", {"intent": "name", "sender": "name"}) < 1.0
```

- [ ] **Step 2: Run to verify they fail** — `pytest -q tests/test_intents.py` → FAIL (`ImportError: INTENT_LABELS`).
- [ ] **Step 3: Implement `intents.py`, `score_label_field`, the band entry, the `score_extraction` canonicalization, and the re-exports.** Do **not** change `DEFAULT_FIELD_TYPES` / the taxonomy fixture here — `intent` stays `name` in the dojo defaults until Task 11 re-pins to the Phase-3 taxonomy.
- [ ] **Step 4: Run** — `pytest -q tests/test_intents.py tests/test_field_scoring.py tests/test_live_roster_parity.py && pytest -q` → `0 failed`.
- [ ] **Step 5: Commit** — `git add llm_dojo_scoring tests && git commit -m "feat: controlled intent vocabulary and exact 'label' field type"`

### Task 5: Re-vendor the `production` prompt family from live mailroom [llm-dojo-scoring]

Closes D3. Run against mailroom **after Task 2 is merged** (so specialists are the corrected v1 bytes).

**Files:**
- Create: `scripts/sync_production_prompts.py`
- Modify: `llm_dojo_scoring/prompts/templates/*.production.md` (regenerated), `llm_dojo_scoring/prompts/catalog.yaml` (`production` rows: `version`, `source_key`, `source_commit`; bump catalog `version: 0.20.0`)
- Create: `llm_dojo_scoring/prompts/templates/{gmail_triage,relations}.production.md` + catalog rows **only if** `DEFAULT_PROFILES` already names those agents; otherwise list them under "not vendored" in `dojo-docs:PROMPTS.md`
- Modify: `dojo-docs:PROMPTS.md` (Families table: `production` specialists now equal `production_prompts` v1; `reporter` is `kind: deterministic`)
- Test: `tests/test_production_prompts.py`, `tests/test_prompts.py`

**Interfaces:**
- Consumes: mailroom `llm.prompts.prompt_templates() -> dict[str, str]` (imported from a mailroom checkout given by `--mailroom PATH`, with `PYTHONPATH=PATH/src`, `OPENROUTER_API_KEY` set to any dummy value).
- Produces: `scripts/sync_production_prompts.py --mailroom PATH [--check]` — writes each live template to `<agent>.production.md` (verbatim text, trailing newline normalized the same way the loader strips it), updates the catalog row's `source_commit` to mailroom `git rev-parse HEAD`; `--check` exits 1 listing drifted agents. `reporter`'s row becomes `kind: deterministic` with the live procedural text.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_production_prompts.py (additions)
from llm_dojo_scoring.prompts import get_prompt

LIVE = ("contracts_specialist", "corporate_records_specialist", "correspondence_specialist",
        "insurance_claims_specialist", "merger_agreement_specialist")

def test_production_specialists_are_frozen_v1():
    for a in LIVE:
        assert get_prompt(a).text == get_prompt(a, family="production_prompts").text

def test_production_family_has_no_retired_vocabulary():
    for a in ("sorter", "boss", "arbiter", "sorter_reviewer", "judge", "judge-classification", *LIVE):
        text = get_prompt(a).text
        assert "compliance_filing" not in text
        for retired in ("key_obligations", "termination_clauses", "key_provisions", "key_points", "referenced_communications"):
            assert retired not in text, (a, retired)

def test_reporter_is_deterministic():
    assert get_prompt("reporter").kind == "deterministic"
```

Fix any existing assertion in `tests/test_prompts.py` / `tests/test_production_prompts.py` that pins the old `contracts_specialist_v32` body length or text — the version label may stay `contracts_specialist_v32` only if the catalog row says the body is now v1; prefer `version: v1` with `source_key: frozen_v1`.

- [ ] **Step 2: Run to verify they fail** — `pytest -q tests/test_production_prompts.py` → FAIL on all three.
- [ ] **Step 3: Implement the script; run it** — `python scripts/sync_production_prompts.py --mailroom ../llm-mailroom`.
- [ ] **Step 4: Verify** — `python scripts/sync_production_prompts.py --mailroom ../llm-mailroom --check` exits 0; `pytest -q` → `0 failed`. Then in mailroom (still pinned v0.19.1 but with the dojo checkout installed editable): `pytest -q src/tests/test_dojo_v012.py` → note any assertion that encodes the old `production` bodies; those are fixed in Task 8, not here.
- [ ] **Step 5: Commit** — `git add scripts/sync_production_prompts.py llm_dojo_scoring/prompts dojo-docs:PROMPTS.md tests && git commit -m "feat: re-vendor production prompt family from live mailroom (frozen v1 specialists)"`

### Task 6: Dojo docs for the new surface [llm-dojo-scoring]

Closes D7.

**Files:**
- Modify: `dojo-docs:EXTRACTION_SCHEMAS.md` (title/pins → v0.20.0; `intent` rows for corporate_record / correspondence / insurance_claim → type `label` with "canonicalized via `normalize_intent`; exact match"; add `label` to the Scoring types table; Cross-class rule 6 — `production` specialists now equal `production_prompts`)
- Modify: `dojo-docs:SCORING.md` (`label` type), `dojo-docs:TODOS.md` (tick "Downstream importers" items done by mailroom Task 2; keep the eval-environment and sandbox items open)
- Modify: `CHANGELOG.md` (`[Unreleased]` → entries for Tasks 3–5 plus the existing EXTRACTION_SCHEMAS entry)

- [ ] **Step 1: Write the doc changes.**
- [ ] **Step 2: Verify** — `pytest -q tests/test_corpus.py tests/test_live_roster_parity.py && pytest -q` → `0 failed` (the UTF-8 schema-doc checks from `5b6ea5a` live there and must still pass).
- [ ] **Step 3: Commit** — `git commit -am "docs: document label intent scoring and the re-vendored production family"`

### Task 7: Cut dojo v0.20.0 [llm-dojo-scoring]

**Files:**
- Modify: `pyproject.toml` (`version = "0.20.0"`), `llm_dojo_scoring/__init__.py` (fallback `__version__ = "0.20.0"`), `CHANGELOG.md` (`## [0.20.0] - <date>`), `dojo-docs:EXTRACTION_SCHEMAS.md` / `dojo-docs:PROMPTS.md` version strings
- Test: `tests/test_consumer_compat.py`

- [ ] **Step 1: Write the failing test** — in `tests/test_consumer_compat.py`: `assert llm_dojo_scoring.__version__ == "0.20.0"` and that every name mailroom imports (the list in the **Mailroom import surface** appendix below) still resolves.
- [ ] **Step 2: Run** — FAIL on version.
- [ ] **Step 3: Bump the version strings and date the changelog.**
- [ ] **Step 4: Run** — `pytest -q` → `0 failed`; `python -m build --sdist --wheel 2>/dev/null || pip wheel . -w /tmp/dojo-wheel --no-deps` succeeds and the wheel contains `llm_dojo_scoring/intents.py` and `prompts/templates/*.md`.
- [ ] **Step 5: Commit and open/merge the PR** (`release: v0.20.0`).
- [ ] **Step 6: HUMAN GATE — tag and release.** Ask the human to approve, then: `git tag v0.20.0 <merge-sha> && git push origin v0.20.0`, publish the GitHub Release, and (optionally) fire mailroom's `repository_dispatch` `dojo-scoring-released`. Do not proceed to Phase 3 until `git ls-remote --tags origin v0.20.0` shows the tag.

---

## Phase 3 — mailroom adopts v0.20.0 (repo: `llm-mailroom`)

### Task 8: Bump the dojo pin to v0.20.0 [llm-mailroom]

**Files:**
- Modify (by script): `pyproject.toml`, every documented pin the script rewrites (README / docs badges / AGENTS.md)
- Modify: `src/tests/test_dojo_v012.py`, `src/tests/test_dojo_v019_wiring.py` (only assertions that encoded old `production` prompt bodies or the 0.19.1 version), `src/tests/test_docs_truth.py` (`**Honest gap (dojo 0.19.1):**` → `0.20.0` if the test hardcodes it)
- Regenerate: `docs/changelog/**`; add a `CHANGELOG.md` Unreleased entry

**Interfaces:**
- Consumes: dojo v0.20.0 (Task 7).
- Produces: mailroom importing `llm_dojo_scoring.__version__ == "0.20.0"`.

- [ ] **Step 1:** `PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --apply --tag v0.20.0 --dry-run` — review the planned rewrites.
- [ ] **Step 2:** Apply it, reinstall: `PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --apply --tag v0.20.0 && pip install -e ".[dev]"`; `python -c "import llm_dojo_scoring as d; assert d.__version__ == '0.20.0'"`.
- [ ] **Step 3: Run** — `pytest -q -p no:cacheprovider`; expected failures are limited to tests pinning the old dojo `production` bodies or version copy. Update those assertions to the new truth (specialists == `production_prompts`; no `compliance_filing`). Any other failure is a dojo regression: fix it in the dojo (new patch release), not in mailroom.
- [ ] **Step 4:** `PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --check` exits 0; `sync_gitbook_changelog.py --check` exits 0; full suite `0 failed`.
- [ ] **Step 5: Commit** — `git commit -am "chore: pin llm-dojo-scoring v0.20.0"`

### Task 9: Score `intent` as `label`; drop mailroom's duplicate vocabulary [llm-mailroom]

**Files:**
- Modify: `src/config/taxonomy.yaml:335,353,376` — `intent: label` for `corporate_record`, `correspondence`, `insurance_claim`; line 319 (`merger_agreement`) **stays `intent: name`**
- Modify: `src/langchain_agents/doc_inventories.py` — replace the local `INTENT_LABELS`, `_INTENT_ALIASES`, `INTENT_DESCRIPTIONS`, `normalize_intent` with re-exports from `llm_dojo_scoring.intents` (keep the names importable from `doc_inventories`; callers unchanged)
- Modify: `docs/the-pipeline-in-depth/extraction-schemas.md` (`intent` "Scoring type" column → `label` for the three classes)
- Test: `src/tests/test_doc_inventories.py`, `src/tests/test_intent_ground_truth.py`, `src/tests/test_field_scoring.py`

**Interfaces:**
- Consumes: `llm_dojo_scoring.intents.{INTENT_LABELS, INTENT_ALIASES, INTENT_DESCRIPTIONS, normalize_intent}` (Task 4).
- Produces: taxonomy blob whose sha1 Task 11 pins.

- [ ] **Step 1: Write the failing tests**

```python
# src/tests/test_doc_inventories.py
import llm_dojo_scoring.intents as dojo_intents
from langchain_agents import doc_inventories as inv

def test_intent_vocabulary_is_the_dojos():
    assert inv.INTENT_LABELS is dojo_intents.INTENT_LABELS
    assert inv.normalize_intent is dojo_intents.normalize_intent

# src/tests/test_field_scoring.py
from llm_dojo_scoring import get_field_types
def test_taxonomy_scores_controlled_intents_as_label():
    for doc in ("corporate_record", "correspondence", "insurance_claim"):
        assert get_field_types(doc)["intent"] == "label"
    assert get_field_types("merger_agreement")["intent"] == "name"
```

(Use whatever fixture the existing `test_field_scoring.py` uses to wire `configure_from_taxonomy`; `get_field_types` reads the wired taxonomy.)

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** All existing `test_doc_inventories.py` / `test_intent_ground_truth.py` alias cases from `bee7f46` must pass unchanged against the dojo implementation — they are the port's acceptance test.
- [ ] **Step 4: Run** — `pytest -q src/tests/test_doc_inventories.py src/tests/test_intent_ground_truth.py src/tests/test_field_scoring.py src/tests/test_specialist_extraction_gt.py && pytest -q -p no:cacheprovider` → `0 failed`. Record the new blob: `git hash-object src/config/taxonomy.yaml`.
- [ ] **Step 5: Commit** — `git commit -am "feat: score controlled intents as dojo 'label'; import the vocabulary from the dojo"`

### Task 10: Cross-repo parity guard in mailroom [llm-mailroom]

**Files:**
- Create: `src/tests/test_dojo_parity.py`

**Interfaces:**
- Consumes: `llm.prompts.prompt_templates()`, `llm_dojo_scoring.prompts.get_prompt`, `llm_dojo_scoring.suites.DEFAULT_FIELD_TYPES`, mailroom `load_config()` taxonomy.

- [ ] **Step 1: Write the tests**

```python
def test_live_specialists_equal_dojo_production():
    live = prompt_templates()
    for agent in SPECIALIST_AGENTS:
        assert live[agent] == get_prompt(agent).text == get_prompt(agent, family="production_prompts").text

def test_taxonomy_field_types_equal_dojo_defaults_for_live_classes():
    tax = {c["key"]: c["field_types"] for c in load_config()["doc_classes"]}
    for doc in LIVE_DOC_TYPES:
        assert tax[doc] == DEFAULT_FIELD_TYPES[doc], doc
```

The second test is **expected to fail** until Task 11's dojo patch release lands (dojo defaults still say `intent: name`). Mark it `@pytest.mark.xfail(strict=True, reason="dojo v0.20.1 re-pins taxonomy fixture (plan Task 11)")` — strict, so it turns red the moment the dojo catches up and the marker must be removed in Task 12.

- [ ] **Step 2: Run** — first test PASS, second XFAIL.
- [ ] **Step 3: Whole-system check (Review Focus 5)** — in one venv with both repos editable: run both suites; both `0 failed, 0 errors`.
- [ ] **Step 4: Commit** — `git add src/tests/test_dojo_parity.py && git commit -m "test: guard live prompts and field maps against the pinned dojo"`

---

## Phase 4 — dojo re-pins to the new taxonomy (repo: `llm-dojo-scoring`)

### Task 11: Taxonomy fixture generator + re-pin; release v0.20.1 [llm-dojo-scoring]

Closes D5. Needs Task 9 merged on mailroom `main`.

**Files:**
- Create: `scripts/gen_taxonomy_fixture.py`
- Modify: `tests/fixtures/taxonomy_field_types.json` (regenerated), `tests/test_live_roster_parity.py:4,43` (`_AUTHORITY_BLOB_SHA1`)
- Modify: `llm_dojo_scoring/suites.py` `DEFAULT_FIELD_TYPES` and `llm_dojo_scoring/corpus.py` `CORPUS_EXTRACTION_FIELDS` — `intent: "label"` for the three controlled classes
- Modify: `dojo-docs:EXTRACTION_SCHEMAS.md` taxonomy-blob pin; version → `0.20.1`; `CHANGELOG.md`

**Interfaces:**
- Produces: `scripts/gen_taxonomy_fixture.py --taxonomy PATH [--check]` — reads mailroom `taxonomy.yaml`, writes the fixture with `authority_blob_sha1` = git blob sha1 of the file bytes (`sha1(b"blob %d\0" % len(data) + data)`), `authority_sha256`, `captured_at` (today), `live_doc_types`, `doc_classes.<key>.{field_types, specialist}`; `--check` exits 1 on any difference except `captured_at`.

- [ ] **Step 1: Write the failing test** — `tests/test_gen_taxonomy_fixture.py`: run the generator (via `load_script`, Task 3) on a tiny temp taxonomy and assert the blob sha1 equals `git hash-object` of the same bytes (compute with `subprocess.run(["git","hash-object",path])`), and that `field_types` round-trip.
- [ ] **Step 2: Run** — FAIL (no script).
- [ ] **Step 3: Implement; regenerate** — `python scripts/gen_taxonomy_fixture.py --taxonomy ../llm-mailroom/src/config/taxonomy.yaml`; set `_AUTHORITY_BLOB_SHA1` to the Task 9 blob; flip `DEFAULT_FIELD_TYPES` / `CORPUS_EXTRACTION_FIELDS` intents to `label`.
- [ ] **Step 4: Run** — `python scripts/gen_taxonomy_fixture.py --taxonomy ../llm-mailroom/src/config/taxonomy.yaml --check` exits 0; `pytest -q` → `0 failed`.
- [ ] **Step 5: Commit, PR, merge; HUMAN GATE for tag `v0.20.1`** (same procedure as Task 7 Step 6).

### Task 12: Mailroom adopts v0.20.1; parity guard goes strict-green [llm-mailroom]

- [ ] **Step 1:** `PYTHONPATH=src python src/scripts/bump_dojo_scoring.py --apply --tag v0.20.1 && pip install -e ".[dev]"`
- [ ] **Step 2:** Run `pytest -q src/tests/test_dojo_parity.py` → the strict xfail now **XPASS-fails**; remove the `xfail` marker.
- [ ] **Step 3:** `pytest -q -p no:cacheprovider` → `0 failed`; `bump_dojo_scoring.py --check` and `sync_gitbook_changelog.py --check` exit 0.
- [ ] **Step 4: Commit** — `git commit -am "chore: pin llm-dojo-scoring v0.20.1; dojo parity guard is strict"`

**Done state:** both `main`s green; mailroom pins `v0.20.1`; dojo fixture pins mailroom's current taxonomy blob; dojo `production` == mailroom live prompts; `intent` scored exactly on the three controlled classes in both repos; one copy of the intent vocabulary (the dojo's).

---

## Appendix — Mailroom import surface (must survive every dojo release)

From `grep -rhoE "from llm_dojo_scoring…"` over mailroom `src/` + `notebooks/` at `bee7f46`:
`get_suite`, `list_suites`, `load_registry`, `get_field_types`, `score_extraction`, `ExtractionScoreResult`, `warm_embedding_model`, `score_category_presence`, `normalize_text`, `is_entity_list`, `parse_date`, `parse_money`, `configure`, `configure_from_taxonomy`, `field_scoring` (module, incl. private `_get_embedding`), `mailroom.score_aligned_classification`, `intake.{looks_messy, INTAKE_SPAN_KEYS, …}`, `serving.compare_serving`, `registry.MetricTier`, `pruning.headline_metrics`, `prompts.{get_prompt, list_prompts}`, `corpus.{normalize_corpus_subclass, DOC_TYPE_SUBCLASSES}`, `content_scoring.peel_non_extraction_fields`, `archive.archive_entry_hash`. Phase 3 adds `intents.{INTENT_LABELS, INTENT_ALIASES, INTENT_DESCRIPTIONS, normalize_intent}`.

## Out of scope (tracked, not done here)

- Dojo `dojo-docs:TODOS.md` items for `LLM-Mailroom-Services/eval-environment` and `Exios66/local-mailroom-sandbox` (re-freeze upstream, mirror stems) — different repos.
- Dojo scoring profiles for `gmail_triage` / `relations` (no scorer consumes them today).
- Making the other controlled `name` fields (`communication_type`, `record_type`, `claim_type`, `urgency`, `coverage_determination`) `label` — same mechanism, but no pipeline change demands it yet.

---

# Addendum (2026-10-07, second pass) — Phases 5–8

Appended after Tasks 1–11 shipped (dojo v0.21.0 released; mailroom PRs #104/#105
and dojo PR #39 open; **Task 12 still pending** on those merges plus a new dojo
tag). Source: three read-only audits (GitBook residue, Langfuse wiring, dojo
needs). Task numbering continues; Task 12 keeps its slot. Repos per task header.

## Addendum — Global Constraints

- GitBook docs now live in `Exios66/mailroom-documentation`. Mailroom keeps only: `AGENTS.md`, `README.md`, `CHANGELOG.md`, the flat operator pages under `docs/` (agents, architecture, api, configuration, deployment, docker-deployment, modal-vllm, local-models, testing, gmail-intake, operational-procedure, sister-repos), `docs/wiki/**`, `docs/superpowers/plans/**`, `docs/reports/**`, `landing/**`, `.cursor/skills/**`, `.opencode/skills/**`. Redefining `docs/` as no longer the source of truth is a **human decision**, out of scope here.
- Never hand-edit prompt text; dojo `production_prompts` stays the prompt authority (Global Constraints above still bind).
- Langfuse work implements from **fresh docs** (`curl -s https://langfuse.com/llms.txt`, pages via `.md` suffix); SDK floor `langfuse>=4.9,<5` (`mask_otel_spans` first ships in 4.9.0).
- Every new Langfuse score name must exist in the dojo registry (KANBAN-061 import-time check in `observability/scores.py`).
- Outward-facing steps (push to another repo, tag, release, emem writes) are human gates, as in Task 7 Step 6.
- emem is **never** on the pipeline path and never decides a score; checkpoints are verified data (signature + pinned hashes) before use.

## Addendum — Review Focus

1. **Doc-reference guard after deletions** — `test_referenced_docs_exist_or_annotated` must stay green once page trees vanish; no kept file may link a removed page.
2. **Masking must not blank the evaluation signal** — redaction removes emails/raw text from child generations but judge input (`pipeline-result`) and score payloads stay intact.
3. **Triage trace on failure** — a failing triage lane still flushes and still carries `mailroom` + environment tags.
4. **Prompt cache after a transient fetch failure** — `None` is never cached; a later call recovers the managed prompt.
5. **`label` on all-null ground truth** — null GT + predicted `other` stays a spurious fill for the five new label fields too.
6. **Experiment gate on an empty/mock dataset** — must fail closed (non-zero exit), never pass vacuously.

## Phase 5 — mailroom working tree clean-up (repo: `llm-mailroom`)

Order matters: trim the tests/scripts that read the doomed files first, then delete.

### Task 13: Land the rescued null-string fix [llm-mailroom]

Branch `claude/extraction-null-strings`, commit `b9fda96` (rescued from the discarded dirty tree; backup patch in the 2026-10-07 session scratchpad only).

**Files:** `src/pipeline/extraction_normalize.py`, `src/tests/test_extraction_null_strings.py`.

- [ ] **Step 1:** `pytest -q -p no:cacheprovider src/tests/test_extraction_null_strings.py` → 18 passed.
- [ ] **Step 2:** Full suite `pytest -q -p no:cacheprovider` → `0 failed`; open the PR.

### Task 14: Decouple tests and scripts from GitBook pages [llm-mailroom]

**Files:**
- Modify: `src/tests/test_docs_truth.py` — delete `test_docker_and_modal_pages_cover_operator_matrix`, the start-here/constellation/repository-guides/the-pipeline-in-depth params of `test_release_docs_match_package_version` and `test_release_docs_match_declared_dojo_pin`, `test_release_operator_docs_do_not_describe_scoring_as_pending`, `test_release_gitbook_page_and_feed_preserve_all_notes` and the changelog-parser tests that import `sync_gitbook_changelog`. Keep `test_referenced_docs_exist_or_annotated` and `test_current_dojo_pin_outside_changelog`.
- Modify: `src/tests/test_bump_dojo_scoring.py` (DOC_TARGETS path list), `src/scripts/bump_dojo_scoring.py` (`DOC_TARGETS` — drop the removed page trees), `src/tests/test_landing.py` (gitbook-yaml tree walk), `src/tests/test_build_mascot.py` (GitBook gif assertion), `src/scripts/affected_tests.py` (prune GitBook mappings).

**Interfaces:** Produces a suite that no longer reads any page slated for deletion in Task 15/16.

- [ ] **Step 1: Write the guard test first** — in `test_bump_dojo_scoring.py`: `test_doc_targets_all_exist` asserts every path in `bump_dojo_scoring.DOC_TARGETS` exists on disk. Run → FAIL after Step 2's deletion preview (`git rm --cached` dry-run via monkeypatched path list), PASS once targets are pruned.
- [ ] **Step 2:** Make the edits above. Run `pytest -q -p no:cacheprovider src/tests/test_docs_truth.py src/tests/test_bump_dojo_scoring.py src/tests/test_landing.py src/tests/test_build_mascot.py src/tests/test_affected_tests.py` → `0 failed`.
- [ ] **Step 3:** Commit — `git commit -am "test: stop asserting on GitBook-hosted pages"`.

### Task 15: Remove GitBook infrastructure [llm-mailroom]

**Files:** delete `.gitbook.yaml`, `gitbook-docs.yaml`, `docs/.gitbook.yaml`, `docs/gitbook-docs.yaml`, `docs/.gitbook/**`, `docs/changelog/**`, `src/scripts/sync_gitbook_changelog.py`, the GitBook summary file under `docs/`; edit `AGENTS.md` (the two `sync_gitbook_changelog` command lines and the GitBook-docs bullet), `deploy/README.md`, `.cursor/skills/modal/SKILL.md` (drop the "listed in the summary" remark).

- [ ] **Step 1: HUMAN GATE.** `docs/constellation/**` and `docs/changelog/**` are **not** in `mailroom-documentation`. Ask the human: copy them there first, or discard. Do not delete before the answer.
- [ ] **Step 2:** Delete + edit as listed; `grep -rniE 'gitbook|SUMMARY\.md' . --exclude-dir=.git --exclude-dir=.superpowers` returns only historical `CHANGELOG.md` lines and this plan.
- [ ] **Step 3:** `pytest -q -p no:cacheprovider` → `0 failed`. Commit — `git commit -am "chore: remove GitBook infrastructure (docs moved to mailroom-documentation)"`.

### Task 16: Remove migrated GitBook page trees [llm-mailroom]

**Files:** delete `docs/{start-here,how-it-fits-together,the-pipeline-in-depth,pipeline-reference-llm-mailroom,repository-guides,mailroom-dataset,about-this-site,constellation}/**`, `docs/assets/fumi/`, root `tree.html` (human confirms it is a stray artifact), and fix links in `README.md`, `docs/README.md`, `.cursor/skills/**`. Add a `CHANGELOG.md` `[Unreleased]` → `### Removed` entry.

- [ ] **Step 1:** Guard test — `test_referenced_docs_exist_or_annotated` stays the acceptance test (Review Focus 1); run it before and after.
- [ ] **Step 2:** Delete, then repoint every dangling link to the `mailroom-documentation` URL or a kept page.
- [ ] **Step 3:** `pytest -q -p no:cacheprovider` → `0 failed`; `git status --short` empty after commit — `git commit -am "chore: drop GitBook page trees now hosted in mailroom-documentation"`.

## Phase 6 — Langfuse wiring (repo: `llm-mailroom`)

Invoke the project `langfuse` skill and fetch current docs before each task. Task 17 is independent of Phase 5.

### Task 17: SDK floor, triage trace tagging, daemon-thread flush [llm-mailroom]

**Files:** `pyproject.toml` (`langfuse>=4.9,<5`), `src/pipeline/watcher.py` (the two duplicated triage-trace blocks near L1035 and L1264), `src/observability/langfuse_setup.py`, `AGENTS.md` (drop the claim that `on_dropped` detects drops on v4).

**Interfaces:** Produces `_triage_trace_kwargs(claimed: Path, intake_meta: dict) -> dict` in `pipeline/watcher.py` returning `tags=["mailroom", <env>, "source-gmail", "route-triage"]`, `environment=default_environment()`, curated `input` (filename, size — never raw text), used by both sites.

- [ ] **Step 1: Failing tests** in `src/tests/test_gmail_triage_tracing.py`: `test_triage_trace_carries_mailroom_and_env_tags`, `test_triage_trace_input_has_no_document_text`, `test_triage_lane_flushes_on_failure` (Review Focus 3; fake client records `flush` call when the lane raises).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement helper, apply at both sites, call `flush_langfuse()` in a `finally` of `_run_triage_lane`, the relations judgment and echo dispatch. **Step 4:** Run → PASS; full suite green.
- [ ] **Step 5:** Commit — `git commit -am "feat(tracing): tag and flush the Gmail triage trace like every pipeline trace"`.

### Task 18: Masking hook [llm-mailroom]

**Files:** `src/observability/langfuse_setup.py` (`client_kwargs()`), new `src/observability/masking.py`, `src/tests/test_trace_masking.py`.

**Interfaces:** Produces `mask_otel_spans(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult | None` (types from `langfuse.types`; signature per the fetched masking docs) redacting email addresses everywhere and dropping `gen_ai.prompt.*` / completion bodies on generations **only when** `MAILROOM_TRACE_REDACT=1`; `intake_meta` reaches trace metadata through an allowlist (`source`, `route`, `message_id_hash`). Iterate the read-only batch in `params.spans`; return `MaskOtelSpansResult(span_patches=...)` with sparse `OtelSpanPatch` values keyed by the batch's span identifiers, or `None` to leave the batch unchanged.

- [ ] **Step 1: Failing tests:** `test_emails_redacted_in_generation_input`, `test_pipeline_result_judge_input_untouched` (Review Focus 2), `test_intake_meta_allowlist_drops_sender`. **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS + full suite. **Step 5:** commit `feat(tracing): redact PII from exported spans`.

### Task 19: Prompt cache that expires [llm-mailroom]

**Files:** `src/llm/prompts.py` (`get_managed_prompt`, `_prompt_cache`), `src/tests/test_prompts.py`.

- [ ] **Step 1: Failing tests:** `test_none_result_is_not_cached` (Review Focus 4), `test_cache_entry_expires_after_ttl` (inject clock; TTL from `MAILROOM_PROMPT_CACHE_TTL`, default 60). **Step 2:** FAIL. **Step 3:** Add TTL, never store `None`, pass `fallback=` to `get_prompt` so offline still links. **Step 4:** PASS + suite. **Step 5:** commit `fix(prompts): expire the managed-prompt cache and never cache misses`.

### Task 20: Score configs for intent/triage; default `config_id` [llm-mailroom]

Depends on Task 24's registry names for the triage score; do the intent half now.

**Files:** `src/observability/scores.py` (`SCORE_CONFIGS`, `score_trace`/`create_trace_score`), `src/observability/suite_scoring.py` (`SUITE_EXTRA_SCORE_NAMES`), tests in `src/tests/test_scores.py`.

**Interfaces:** Produces BOOLEAN `intent_correct` (via `llm_dojo_scoring.intents.normalize_intent`), and `config_id` defaulting from a `name → id` map built in `ensure_score_configs()`.

- [ ] **Step 1: Failing tests:** `test_intent_correct_registered_in_dojo_registry`, `test_score_trace_defaults_config_id`. **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS + suite. **Step 5:** commit `feat(scores): emit intent_correct and attach score-config ids`.

### Task 21: Dataset experiment runner [llm-mailroom]

**Files:** create `src/scripts/run_experiment.py`; modify `src/scripts/sync_dataset.py` (`--source hf` via `pipeline.hf_corpus_loader`, pinned revision in item metadata); tests `src/tests/test_run_experiment.py`.

**Interfaces:** `run_experiment.py --dataset NAME [--mock|--real] [--max-items N] [--baseline PATH]` runs `run_pipeline` per dataset item through the current SDK experiment API, item evaluators `class_correct` + dojo `score_with_suite`, run evaluators macro accuracy / F1; exit 1 on regression versus the baseline JSON. The same module also exposes `experiment(context: RunnerContext)` for `langfuse/experiment-action`, calls `context.run_experiment(...)` to honor action-injected dataset/version/metadata, and returns the experiment result; raise SDK `RegressionError(result=result)` on baseline regression. Both entrypoints share evaluators and baseline comparison, reject empty datasets, and support mock pipeline execution. Keep CLI parsing under `if __name__ == "__main__":`; the action entrypoint reads mock mode, item limit, and baseline path from documented environment settings supplied by Task 22.

- [ ] **Step 1: Failing tests:** `test_mock_experiment_scores_every_item`, `test_regression_vs_baseline_exits_nonzero`, `test_empty_dataset_fails_closed` (Review Focus 6), `test_action_entrypoint_matches_cli_regression_gate` (mock `RunnerContext`; assert returned result on pass and `RegressionError` on regression). **Step 2:** FAIL. **Step 3:** Implement from the fetched experiments-via-sdk page. **Step 4:** PASS; `PYTHONPATH=src python src/scripts/run_experiment.py --dataset mailroom-pilot --mock` exits 0 and a run appears under Experiments (self-audit loop). **Step 5:** commit `feat(eval): run Langfuse dataset experiments from the pilot manifest and HF corpus`.

### Task 22: CI experiment gate [llm-mailroom]

**Files:** create `.github/workflows/langfuse-experiment.yml`, `docs/superpowers/baselines/experiment-baseline.json` (committed approved baseline).

- [ ] **Step 1:** Workflow on `pull_request` uses `langfuse/experiment-action` with `experiment_path: src/scripts/run_experiment.py`, invoking Task 21's `experiment(context: RunnerContext)` entrypoint with mock mode and the committed baseline configured through its documented environment settings (SDK ≥ 4.9, Task 17). Set `should_fail_on_regression: true` and `should_fail_on_script_error: true`. Skip the credentialed job for fork PRs (`github.event.pull_request.head.repo.full_name != github.repository`); for same-repository PRs, check required `LANGFUSE_*` credentials through step environment variables and skip the action with an explicit reason if unavailable. Runs with credentials must enforce the baseline regression gate. **Step 2:** Verify with `actionlint` if available, plus a dry local run → exit 0 on baseline, exit 1 after a hand-degraded copy; verify the action contract with a mocked `RunnerContext` and the workflow conditions for fork, missing-credential, and credentialed runs. **Step 3: HUMAN GATE** — repository secrets (`LANGFUSE_*`) are the human's to add. **Step 4:** commit `ci: gate PRs on Langfuse experiment regression`.

## Phase 7 — dojo expansion for mailroom's needs (repo: `llm-dojo-scoring` unless noted)

Order from the dojo audit: 23 → 24 → 25 → 26 → 27 → 28 → 29. Each ends with a dojo suite run and, where the mailroom consumes it, a cross-repo parity test in mailroom.

### Task 23: `label` for the remaining controlled fields [dojo + llm-mailroom]

Fields: `record_type`, `communication_type`, `urgency`, `claim_type`, `coverage_determination` (mailroom taxonomy lines currently `name`).

**Files:** dojo `llm_dojo_scoring/intents.py` (generalize to a per-`(doc_class, field)` vocabulary map; keep `INTENT_LABELS`/`normalize_intent` public), `field_scoring.py`, `suites.py`, `tests/test_label_fields.py`; mailroom `src/config/taxonomy.yaml`, `src/langchain_agents/doc_inventories.py`.

**Interfaces:** Produces `FIELD_VOCABS: dict[tuple[str, str], tuple[str, ...]]` and `normalize_field(doc_class, field, value) -> str`; `normalize_intent` becomes a thin wrapper.

- [ ] **Step 1: Failing tests:** for each of the five fields `test_alias_scores_one`, `test_out_of_vocabulary_scores_zero`, `test_null_gt_other_pred_is_spurious_fill` (Review Focus 5), and `test_explicit_name_map_keeps_fuzzy_rescoring`. **Step 2:** FAIL. **Step 3:** Implement; mailroom flips the five fields to `label` only after the dojo release is pinned (ordering constraint from the original Architecture paragraph). **Step 4:** both suites green; regenerate the fixture with `gen_taxonomy_fixture.py`. **Step 5:** commit per repo; **HUMAN GATE** for the dojo tag.

### Task 24: `gmail_triage` + `relations` prompts, triage scorer, registry export [dojo + llm-mailroom]

**Files:** dojo `profiles.py` (`DEFAULT_PROFILES`), `prompts/catalog.yaml`, `scripts/sync_production_prompts.py`, new `llm_dojo_scoring/triage.py`, `registry.py`, `tests/test_triage.py`; mailroom `src/tests/test_dojo_parity.py`, `src/observability/scores.py`.

**Interfaces:** Produces `score_triage(triage: dict, gt: dict, final_class: str | None = None) -> dict[str, float]` with `triage_class_correct`, `triage_prior_agreement`, `triage_extraction_field_f1` (via `score_extraction`), `triage_schema_clamp_rate`, `triage_handoff_precision`; and `dojo registry --export-score-configs` emitting `{name, data_type, min, max}` for every metric tagged `emit_in: pipeline`.

- [ ] **Step 1: Failing tests:** six hand-built triage dicts with exact expected metrics; `test_registry_export_has_bounds_for_pipeline_metrics`; mailroom `test_prompt_templates_all_vendored` (every key of `prompt_templates()` has a dojo entry) and `test_score_configs_equal_dojo_export_subset`. **Step 2:** FAIL. **Step 3:** Implement; vendor with `sync_production_prompts.py`. **Step 4:** both suites green. **Step 5:** commits; mailroom wires `triage_class_correct` onto the Gmail triage trace (closes Task 20's second half).

### Task 25: Full hash-chain verification [dojo]

**Files:** `llm_dojo_scoring/archive.py`, `tests/test_archive_chain.py`.

**Interfaces:** `verify_chain(entries: Sequence[dict]) -> ChainResult(valid: bool, break_index: int | None, reason: str)` — `prev_hash` linkage, strictly monotonic timestamps, scope separation, per-stage event order; metrics `chain_valid`, `chain_break_index`.

- [ ] **Step 1: Failing tests:** valid 5-entry chain passes; tampered detail, reordered pair, repeated timestamp, wrong `prev_hash` each fail with the right `break_index`; plus a mailroom-side `test_audit_chain_passes_dojo_verify` feeding `storage.audit_log.get_audit_chain` output. **Steps 2–5** as usual.

### Task 26: Sliding-window merge scorer [dojo]

**Files:** `llm_dojo_scoring/intake.py`, `tests/test_window_merge.py`.

**Interfaces:** `score_window_merge(text: str, windows: list[Window], merged: MergeResult, gt_class: str | None) -> dict[str, float]` → `window_coverage`, `section_offset_roundtrip`, `merge_vote_correct`, `window_disagreement_rate`.

- [ ] **Step 1: Failing tests:** synthetic long text → coverage 1.0; deliberately dropped tail → < 1.0; off-by-one offset fails round-trip; 2-vs-1 wrong majority fails. Steps 2–5 as usual; mailroom adds a test feeding real `agents.intake.sliding_windows` output.

### Task 27: Router and gateway-tier serving identity [dojo + llm-mailroom]

**Files:** dojo `serving.py` (`ServingIdentity.routed_model`, `.tier`, kind `router`), `cost.py` (`priced: bool`; free pool = 0.0 unpriced, excluded from totals), new `compare_tiers()`; mailroom `src/scripts/sync_langfuse_logs.py` (carry `generation.model`).

- [ ] **Step 1: Failing tests:** three records with one prompt and different served models group into one `router` run with a per-model breakdown; free-pool cost excluded from aggregate; `compare_tiers` returns per-tier quality, p95 latency, cold-start fraction (503 count) and cost-per-correct-document, and flags tier/alias drift. Steps 2–5 as usual.

### Task 28: ModernBERT fast-path scorer [dojo]

Depends on Task 27 (paired on/off runs need a flag on run identity).

**Files:** new `llm_dojo_scoring/bert_intake.py`, `tests/test_bert_intake.py`.

**Interfaces:** `score_bert_intake(rows: Sequence[dict]) -> dict[str, float]` → `bert_fast_path_precision`, `bert_route_coverage`, `bert_availability_rate`, `bert_prior_harm_rate`, `bert_latency_p95`; rows with `reason != "ok"` excluded from precision, counted in availability.

- [ ] Steps 1–5 as usual; the acceptance test is a wrong `fast_path` row driving precision below 1.0. Gates the sorter-skip decision (#90).

### Task 29: Relations clerk scorer [dojo]

**Files:** new `llm_dojo_scoring/relations.py`, `tests/test_relations.py`. **First sub-step:** define a labelled pair set (matter ids from the HF corpus) in the test fixtures — design before code.

**Interfaces:** `score_relations(edges, gold_pairs) -> dict` → per-`rtype` precision/recall/F1, `llm_edge_lift`, `near_miss_band_calibration`, `ledger_chain_valid`, `ledger_monotonic_ts` (reuses Task 25's `verify_chain`).

- [ ] Steps 1–5 as usual; 5-document toy graph with hand-computed P/R/F1; tampered ledger row fails; duplicated timestamp flagged. Output informs whether `relations.llm` should go live.

## Phase 8 — long-horizon run memory (optional, last)

### Task 30: emem checkpoint sink for long runs [dojo]

**Files:** new `llm_dojo_scoring/experiment_checkpoint.py`, `tests/test_experiment_checkpoint.py`; mailroom long scripts (`run_pilot.py`, `run_vision_sweep.py`, `sync_hf_ground_truth.py`) take `--resume`.

**Interfaces:** `CheckpointSink` protocol — `write(state: RunState) -> str`, `read() -> RunState | None`; `RunState = {run_id, dataset_sha, taxonomy_blob_sha1, prompt_sha256: dict, completed_doc_ids, partial_metrics}`. Implementations: `JsonFileSink` (default, hermetic) and `EmemSink` (adapter over the emem note tools; signature verified with the skill's `verify_note` before trust).

- [ ] **Step 1: Failing tests:** run killed after 3 of 6 documents resumes and scores only the last 3 with final metrics equal to an uninterrupted run; a checkpoint whose `prompt_sha256` or `taxonomy_blob_sha1` differs is rejected; `EmemSink` rejects an unsigned/foreign note (stub transport). **Steps 2–4** as usual.
- [ ] **Step 5: HUMAN GATE** before any real write to `emem.dev` (identity creation and external publish); default stays `JsonFileSink`.

## Addendum — Execution order and gates

1. Task 12 (original) after #104/#105/dojo#39 merge and the dojo tag.
2. Phase 5: 13 → 14 → (gate) 15 → 16.
3. Phase 6: 17 → 18 → 19 → 20 → 21 → 22 (parallel with Phase 5 where files do not overlap).
4. Phase 7: 23 → 24 → 25 → 26 → 27 → 28 → 29; one dojo release per two tasks, each a human gate.
5. Phase 8 last.

**Human gates collected:** Task 15 Step 1 (copy or discard `constellation/` + `changelog/`), Task 16 (`tree.html`), Task 22 Step 3 (CI secrets), every dojo tag/release in Phase 7, Task 30 Step 5 (emem writes).

**Out of scope still:** eval-environment and local-mailroom-sandbox re-freezes (other repos); making `docs/` no longer the source of truth.
