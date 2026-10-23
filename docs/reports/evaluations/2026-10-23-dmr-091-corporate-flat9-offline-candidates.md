# DMR-091 corporate Flat9 offline candidates

- **Date**: 2026-10-23 (UTC)
- **Kind**: evaluations (Offline judge/quality evaluations over corpora)
- **Status**: offline, unmeasured, not live; no adoption or promotion

## Scope

Two independently selectable instruction candidates for the corporate-records
Flat9 request contract. Prepared from pipeline source
`85a0009ac3efb86aaae403e52afb75355b10d409`, with Dojo source frozen at
`3eb56be1c445b2f71c461ffa5f26660ca8516f98` (0.23.0).
The parent is Dojo family `production`, lineage key
`corporate_records_specialist_v6`, model-visible SHA256
`fe13501f667fcd9f64f7dfbf8b0f124f81d9dd039f4109288b43f106f82b1399`.
It has 5,865 characters / 5,889 UTF-8 bytes. The leading Markdown provenance
comment is not part of this model-visible identity.

Both new heads are complete literal strings in
`src/langchain_agents/prompt_candidates/corporate_flat9.py`; neither is derived
at import time from a moving production alias. Per-key metadata records the
parent family/key/hash, source commits, one replacement, child hash/length,
and `offline_unmeasured_not_live` status. The local names were available in
the frozen pipeline registry. They do not register or impersonate canonical
Dojo v8/v9 identities.

## Method

The CodeAnt forensic review of 2026-10-23 identified two different concerns:

- Charter amendments: live v6 allows `corporate_action_approval` or
  `entity_formation`, while all 80 charter-amendment labels in the previously
  audited cached v9.1 corpus are `entity_formation`. These are label-support
  counts, not observed model failures. Dojo scores the two intents distinctly.
- Powers of attorney: v6 permits a principal-name fallback in `entity_name`.
  Flat9 has no separate `principal` property. Restricting the field to the
  stated owning company removes that text ambiguity without adopting the
  richer subclass schema. The previously audited 28 v9.1 POAs have no
  entity_name/principal labels; entity-name accuracy cannot be inferred.

The corpus remains `Lucius-Morningstar/mailroom-dataset` v9.1, tag commit
`bc9eab280044befb51e19dda3071d290a8677f42`, data commit
`ed7576b676343e0b402ec5412cded301e629bdee`. This proposal neither edits the
corpus nor fetches it again. The corpus evidence comes from the prior
`dmr091-codeant-corporate-prompt-review.md` forensic report, not a new model run.

## Findings

**Charter-only key:** `corporate_records_specialist_flat9_charter_v0`.
Replace exactly once:

```diff
- intent corporate_action_approval or entity_formation as stated.
+ intent entity_formation.
```

Head: 5,826 characters / 5,850 UTF-8 bytes; SHA256
`7f7d0dbcb7f7184d2e22cab1f1d65d8c03b25a24b4dda587402f7f68d35992c3`.
The POA fallback remains unchanged in this candidate. A genuine board
resolution retains `corporate_action_approval`; a charter amendment reciting
board approval still receives the charter-specific instruction.

**POA-only key:** `corporate_records_specialist_flat9_poa_v0`.
Replace exactly once:

```diff
- entity_name or principal name in entity_name when corp POA
+ entity_name as the owning company (registrant); never put an individual principal or attorney-in-fact in entity_name; null if no company is stated
```

Head: 5,953 characters / 5,977 UTF-8 bytes; SHA256
`e3c692abf0902f9998ad22c9f0623785d8000127ec489d9c61df77baf994aaf2`.
The charter instruction remains unchanged in this candidate. Existing
signatory instructions and the ban on inference from filenames/letterhead
remain intact. No principal/attorneys-in-fact output properties are added.

The request still requires exactly `entity_name`, `record_type`,
`effective_date`, `signatories`, `jurisdiction`, `filing_number`, `intent`,
`subject_matter`, `keywords`, with `additionalProperties: false`. Its canonical
sorted compact JSON SHA256 remains
`b268b685c61466ec02d6328ffd64848011afb0c9111573bace34a47ac89c1628`.
A person's name remains a valid string type: schema validation alone cannot
establish that a company was correctly extracted.

## Explicit offline selection

From the source checkout with the existing pinned dependencies available:

```python
import hashlib
from agents.corporate_records_specialist import CorporateRecordsSpecialist
from langchain_agents.prompts import get_prompt
from langchain_agents.prompt_candidates.corporate_flat9 import CANDIDATE_METADATA
from langchain_agents.specialist_agents import CORPORATE_RECORDS_SCHEMA

key = "corporate_records_specialist_flat9_charter_v0"
# To inspect the separate POA lesson, explicitly select:
# key = "corporate_records_specialist_flat9_poa_v0"
head = get_prompt(key)
assert hashlib.sha256(head.encode("utf-8")).hexdigest() == CANDIDATE_METADATA[key]["sha256"]

# Pure message rendering only: bypass __init__ (no provider/client setup).
probe = object.__new__(CorporateRecordsSpecialist)
system, user = probe._structured_messages(
    "OFFLINE PROBE", CORPORATE_RECORDS_SCHEMA, system_prompt=head
)
full_system = probe.system_prompt_with_skills(system)
print(key, CANDIDATE_METADATA[key])
print(hashlib.sha256(full_system.encode("utf-8")).hexdigest())
```

This does not invoke extraction or install a candidate as the agent default.
The corporate agent constructor has no `prompt_version` selection parameter.

The two explicit keys are appended after `bind_production_specialists` in
`PROMPT_VERSIONS`. All 66 previous registry values, all 18 managed production
templates, default-version metadata, and the Flat9 schema were compared before
and after and remain identical. The normal `llm.prompts.prompt_templates()`
route used by `src/scripts/sync_prompts.py` excludes both candidates.
The legacy uppercase `langchain_agents.prompts.PROMPT_TEMPLATES()` remains an
all-version history exporter, including offline candidates and older eval
versions; it is not the managed production sync catalog. Do not use that
history exporter as a deployment list.

Live production v6 stays at `fe13501f...`; evaluation v7 stays at
`253559fcb7ac6b36878453821a4fab0aa94228ad5add77d5aeabb06bb265134c`.
With the frozen source skill appendix and JSON-mode note, final system hashes
are v6 `f3854c7551379a53cb7c6539c542732ff013023403327c49b9c39ddffe706757`,
charter candidate `c82dd12b1a5a286206b5a2dca9ff4101c2a5670e93abe9c0c9729ca9335c2fe7`,
and POA candidate `ca5c15b97c3df13205269f7ab9407569d2069e4321550ad5245cd9481724ab6a`.
These hashes cover the rendered system string, not a complete provider request
or remotely managed Langfuse bytes.

## Deterministic verification

The new `src/tests/test_corporate_flat9_candidates.py` exercises real registry
selection, real pure message rendering, request-schema identity, full literal
and single-replacement identities, preserved live/eval heads, and managed sync
catalog exclusion. The same two contract assertion helpers were executed
against the exact frozen v6 head: **2 failed**, one per lesson, as expected.
On the explicit candidates both pass. New-file suite: **22 passed**. Combined
with existing prompt, frozen-v1, and mutation-lineage suites: **50 passed**.
All ran offline with frozen Dojo imports and a socket/DNS-denying audit hook;
no dependency installation or model call was performed.

```bash
PYTHONPATH=src python -m pytest -p no:cacheprovider \
  src/tests/test_corporate_flat9_candidates.py src/tests/test_prompts.py \
  src/tests/test_frozen_v1_prompts.py src/tests/test_prompt_mutation_lineage.py -q
```

Board-resolution controls retain the approval rule. POA synthetic controls
cover a distinct individual principal, an entity principal, no stated company,
and several attorneys-in-fact, with signed-person lists preserved. The inputs
are rendered into real messages; hand-labeled expected payloads are checked
against the Pydantic Flat9 type model. No fake provider returns those answers.
These checks cannot establish that an LLM follows the instructions.

Frozen Dojo label-score controls are hand-computed: formation versus approval
is 0, formation versus formation is 1; genuine board approval versus approval
is 1, approval versus formation is 0. An unlabeled POA entity-name control has
intent score/F1 1 while entity_name is explicitly excluded from field scores.
That 1 is not entity-name accuracy. No scorer or ground-truth label is changed.

## Recommendations and unresolved live status

**The current LIVE charter defect and POA semantic ambiguity are not resolved
by adding these offline candidates.** Each candidate addresses only its own
lesson and is unmeasured. Neither has passed a model A/B, noise-floor,
full-corpus regression, or GEPA acceptance gate; neither joins a measured
frontier or qualifies as release-grade. No claim of improved LLM metrics,
entity-name accuracy, cost, robustness, or recovered failures is made.
No combined candidate is proposed. Rollout calls/tokens/spend: zero.

A later human must concretely select a candidate before any live adoption.
Any future model evaluation requires the same corpus fingerprint, seed, model,
scorer, chunking and rendered request identity; the POA question additionally
needs separately reviewed entity labels. Production adoption, catalog freezes,
managed-prompt sync, source releases/imports and vendor generation remain
separate later work. Preserve v6/v7 history. Do not resolve CodeAnt threads as
live-fixed on the strength of this proposal. The remotely managed production
prompt was not inspected.
