# Frozen v1 specialist prompts (production)

Byte-identical copies of the local-mailroom-sandbox / eval-environment
**frozen v1** specialist stems. These are the production extraction prompts
for the LangGraph `extract` / `retry_extract` nodes.

| Agent | Sandbox stem | eval-environment key |
| --- | --- | --- |
| `contracts_specialist` | `contracts_specialist_v33_simplified` | `contracts_specialist_v1` |
| `corporate_records_specialist` | `corporate_records_specialist_simplified` | `corporate_records_specialist_v6` (corrected; derived from `_v1`) |
| `correspondence_specialist` | `correspondence_specialist_simplified` | `correspondence_specialist_v7` (corrected; derived from `_v1`) |
| `insurance_claims_specialist` | `insurance_claims_specialist_simplified` | `insurance_claims_specialist_v1` |
| `merger_agreement_specialist` | `merger_agreement_specialist_simplified` | `merger_agreement_specialist_v1` |

Source: the `llm-dojo-scoring` `production` catalog, which records
`source_commit` for the pipeline-owned production snapshot. Originally frozen
from `Exios66/local-mailroom-sandbox` `config/prompts/` on 2026-09-26
(sha256-locked in `lineage.json`) and re-frozen 2026-10-05.

The dojo's `production_prompts` family tracks evaluation candidates separately.
Its corporate-records v7 (`253559fcb7ac6b36878453821a4fab0aa94228ad5add77d5aeabb06bb265134c`,
5,462 characters) has not been promoted to this pipeline's production freeze:
corporate-records v6 remains `fe13501f667fcd9f64f7dfbf8b0f124f81d9dd039f4109288b43f106f82b1399`
(5,865 characters). The other four specialist stems match across those dojo
families. All five production hashes and lengths remain unchanged. A sanctioned
re-freeze must update the lineage record explicitly; selecting a newer eval
candidate is not a re-freeze.

**Corrected roles.** `corporate_records_specialist` (sha256 `fe13501f…`) and
`correspondence_specialist` (sha256 `2b0b81ff…`) serve the 2026-10-05
dojo-corrected bytes (`communication_type` -> `null` not `other`;
`filing_number` null unless an official number). Eval-environment records them
as the mutations `corporate_records_specialist_v6` /
`correspondence_specialist_v7`; `_v1` is unchanged and remains its default.
`lineage.json` keeps the uncorrected v1 digest under `derived_from`. The
legacy `*.txt` files for these two roles still hold those uncorrected v1 bytes
(`484e64dd…`, `eab63b5a…`); nothing loads them.

The **sorter** is not in this catalog. Production classification stays on
`sorter_v14` (V12 CUAD-subtype lineage + mailroom pipeline doctrine) — the
strongest sorter this pipeline has — served through `get_managed_prompt`
from the classify nodes.

Entity-extraction lineage keys (`contracts_specialist_v1`…`v33`,
`sorter_v0`…`v14`) remain in `langchain_agents/prompts.py` as frozen
history for eval loops. They are not the production specialist pin.

> **Note:** the `*.txt` files in this directory are superseded by the
> `llm-dojo-scoring` `production` catalog (see `__init__.py`). They are
> no longer packaged and are slated for removal.
