---
name: dojo-scoring
description: llm-dojo-scoring pin and mailroom scoring suites (v0.12.1). Use when adding scores, specialist suites, class match, field types, or SCORE_CONFIG names — never invent metrics absent from the installed registry.
---

# llm-dojo-scoring (pinned engine)

**When:** Extraction/classification KPIs, specialist suites, field types,
honesty gaps, Langfuse score configs.  
Pin: `llm-dojo-scoring @ git+https://github.com/Exios66/llm-dojo-scoring.git@v0.12.1`

## Rules

1. **Exact class match** — `merger_agreement` is not `contract`. Do not call
   dojo `align_doc_type` for mailroom class KPIs.
2. **Registry only** — `SCORE_CONFIGS` / `ensure_score_configs()`. Do not
   emit a new Langfuse score name that is not in the installed dojo registry
   (`field_presence` stays unemitted).
3. **Suites** — `observability/specialist_suites.py` `get_suite(doc_class)`.
   One suite per live extract class. `merger_agreement` shares the
   `contracts_specialist` *agent* but uses the rebound MAUD suite.
4. **GT** — Hub labels win; post-hoc fills remaining schema fields
   (`observability/posthoc_gt.py`) with provenance. Never bill post-hoc as
   official Hub gold.
5. **Honesty** — `compliance_filing` has zero Hub rows (local pack only).
   CMS `determination_consistency` is gated when GT is all-approved.

## Where it lives

| Piece | Path |
| --- | --- |
| Field scorer shim | `observability/field_scoring.py` |
| Class KPIs | `observability/classification_scoring.py` |
| Specialist suites | `observability/specialist_suites.py` |
| Score configs | `observability/scores.py` |
| Taxonomy `field_types:` | `src/config/taxonomy.yaml` |

v0.12.1 is additive on 0.11.0 formulas. Serving comparison lives in
`llm_dojo_scoring.serving` (`get_suite("local_vs_api")`). Production
`prompt_templates()` stay in this repo; `llm_dojo_scoring.prompts` is the
scored snapshot (anti-priming).

## Related

- Router: [mailroom-tool-router](../mailroom-tool-router/SKILL.md)
- Hub GT: [huggingface](../huggingface/SKILL.md)
