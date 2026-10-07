# Frozen v1 specialist prompts (production)

Byte-identical copies of the local-mailroom-sandbox / eval-environment
**frozen v1** specialist stems. These are the production extraction prompts
for the LangGraph `extract` / `retry_extract` nodes.

| Agent | Sandbox stem | eval-environment key |
| --- | --- | --- |
| `contracts_specialist` | `contracts_specialist_v33_simplified` | `contracts_specialist_v1` |
| `corporate_records_specialist` | `corporate_records_specialist_simplified` | `corporate_records_specialist_v1` |
| `correspondence_specialist` | `correspondence_specialist_simplified` | `correspondence_specialist_v1` |
| `insurance_claims_specialist` | `insurance_claims_specialist_simplified` | `insurance_claims_specialist_v1` |
| `merger_agreement_specialist` | `merger_agreement_specialist_simplified` | `merger_agreement_specialist_v1` |

Source: `Exios66/local-mailroom-sandbox` `config/prompts/` (sha256-locked in
`lineage.json`, frozen 2026-09-26). Do not edit the `.txt` files in place —
a sanctioned re-freeze updates `lineage.json` and the stems together.

The **sorter** is not in this catalog. Production classification stays on
`sorter_v14` (V12 CUAD-subtype lineage + mailroom pipeline doctrine) — the
strongest sorter this pipeline has — served through `get_managed_prompt`
from the classify nodes.

Entity-extraction lineage keys (`contracts_specialist_v1`…`v33`,
`sorter_v0`…`v14`) remain in `langchain_agents/prompts.py` as frozen
history for eval loops. They are not the production specialist pin.

> **Note:** the `*.txt` files in this directory are superseded by the
> `llm-dojo-scoring` `production_prompts` catalog (see `__init__.py`). They are
> no longer packaged and are slated for removal.
