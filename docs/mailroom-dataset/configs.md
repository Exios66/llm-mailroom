# Configs

`mailroom-dataset` ships five Hugging Face configs. The first two are the evaluation contract every repository joins. The other three are ingress-simulation extras.

Pin the revision (`v9.1` / `ed7576b6`). Never read the live tip.

## `default` (blind)

3,302 rows. Document text and metadata only. **Zero labels by construction.**

| Column | Role |
| --- | --- |
| `filename` | Join key (and the split hash input) |
| `doc_text` | Full document text. Never truncated on this surface. |
| `prompt` | Empty on this corpus (`prompt_all_empty: true` in the integrity report) |
| `metadata` | Provenance and non-label aggregates (string-like; 53-key union) |
| `split` | `train` or `test` |

An LLM that only sees `default` cannot read class, subclass, intent, clause spans, or the evaluation contract.

**v9.1 quality revision:** 509 v8 contract rows once carried `clause_count` and 152 merger rows `maud_label_count` inside the blind `metadata` blob. Those are label-derived aggregates. v9.1 moved them to `gt_fields.clause_count` / `gt_fields.maud_label_count` so they never appear on the blind surface.

## `ground_truth`

3,302 rows, joined 1:1 on `filename`. The integrity pass reports unique filenames on both sides, equal filename sets, and identical 2,979/323 splits.

### Identity and provenance

`document_id`, `content_sha256`, `normalized_text_sha256`, `source_corpus`, `source_document_id`, `source_filename`, `source_revision`, plus `annotation_*` (source, method, model, prompt version, confidence, reviewer, timestamp).

The pipeline loader checks `content_sha256` against the bytes of `doc_text` at load time.

### Classification gold

`expected` (the five live classes), `expected_subclass` (the 55 strata).

### Evaluation contract (§84)

`expected_specialist`, `expected_stage`, `retry_expected`, `review_expected`, `review_reason`, `expected_post_retry_state`. These tell an eval harness which pipeline path a document is *supposed* to take — not what a model predicted.

### Matter / group / thread

`matter_id`, `matter_construction`, `group_id`, `group_role`, `thread_position`, `thread_size`, `thread_evidence`, `relationships`, `related_document_ids`.

### Field gold (`gt_fields` and expanded columns)

After expansion the ground-truth config carries on the order of 65 columns. Class-specific fields include:

| Block | Fields |
| --- | --- |
| CUAD contracts | `cuad_clause_labels`, `clause_count`, `label_evidence` |
| MAUD mergers | `maud_clause_labels`, `maud_label_count` |
| Insurance | `claim_number`, `policy_number`, `insurer`, `insured_party`, `claim_type`, `date_of_loss`, `date_filed`, `claimed_amount`, `adjuster`, `damages_description`, `coverage_determination`, `denial_reasons`, `supporting_documents` |
| Correspondence | `intent`, `intent_source`, `intent_confidence`, `intent_status`, `content_topic`, `topic_evidence`, `sentiment_label`, `sentiment_score`, `sentiment_evidence` |
| Shared | `subject_matter`, `keywords`, `gt_presence`, `token_estimate`, `context_window_band` |

Nulls are class-scoped, not missing data. Insurance fields are null on non-claim rows (1,152 non-claim documents). Correspondence topic/sentiment columns are null outside that class. `adjuster` is empty on CMS DE-SynPUF, GNOTHEIA property, and INSURBIAS rows (no adjuster in those sources); BDR auto rows carry pseudonyms.

List-typed fields (`denial_reasons`, `supporting_documents`, `relationships`, `related_document_ids`) are valid JSON arrays (`[]` = no items). Clause labels are JSON objects (`{}` = none — the 91 EX-10 contracts).

## `bundles` (50)

Synthetic multi-document bundle families built over real anchors, flagged `synthetic_constructed`. Used to exercise grouping and matter construction, not as extra gold documents.

## `streams` (62)

An interleaved ingress stream (`RUN-SIM-001`) with distractors. Simulates a watched inbox rather than a tidy eval draw.

## `fixtures` (32)

Recovery and adversarial fixtures: calibration quartet, arbiter cases, failure stages. Used to calibrate review/retry routing, not to score classification accuracy.

## How the pipeline loads it

llm-mailroom does **not** depend on `datasets`. `pipeline/hf_corpus_loader.py` walks parquet (with a `/rows` pagination fallback), joins `default` to `ground_truth` on `filename`, and proves `content_sha256 == sha256(doc_text)`. Pilots pass `ground_truth=` into `run_pipeline` so Langfuse judges see expected class/stage/fields.

Eval-environment's subset grammar (`src/evals/cases.py`) samples this same pin. mailroom-ml trains on `mailroom-modernbert-training` / `mailroom-finetune`, not on this eval surface.

## Family datasets

| Dataset | Role |
| --- | --- |
| [`mailroom-dataset`](https://huggingface.co/datasets/Lucius-Morningstar/mailroom-dataset) | This corpus |
| [`mailroom-corpus`](https://huggingface.co/datasets/Lucius-Morningstar/mailroom-corpus) | Frozen v8 parent (2,000 rows, `eafe1ab4`) |
| [`docclass-pilot`](https://huggingface.co/datasets/Lucius-Morningstar/docclass-pilot) | 48 class × subclass examples, one per stratum in that pack |
| [`enron-correspondence-dedup`](https://huggingface.co/datasets/Lucius-Morningstar/enron-correspondence-dedup) | 247,523-row Enron pool |
| [`mailroom-cuad-contracts-full`](https://huggingface.co/datasets/Lucius-Morningstar/mailroom-cuad-contracts-full) | Byte-verified CUAD mirror |
| [`cms-desynpuf-insurance-claims`](https://huggingface.co/datasets/Lucius-Morningstar/cms-desynpuf-insurance-claims) | Rendered CMS EOB documents |
| `mailroom-finetune`, `mailroom-modernbert-training` | Training copies for mailroom-ml |
| `legalbench-full` | LegalBench task pack (not a pipeline-ingest corpus) |

Collection: [Mailroom Corpus Family](https://huggingface.co/collections/Lucius-Morningstar/mailroom-corpus-family-6aa715cce29d415b0db92473).
