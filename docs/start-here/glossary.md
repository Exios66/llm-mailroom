# Glossary

Terms that appear across the constellation's code, docs and commit messages.

## Pipeline

**Agent.** A component that does one job on a document. LLM agents (sorter, specialists, judge, arbiter, boss) call a model through `get_llm(agent_name)`; procedural agents (PDF transcriber, image extractor, reporter, archivist) do not.

**Arbiter.** The agent that rules on a judge's "partial" or "incomplete" verdict: let the extraction stand, order a re-extraction, or send it to human review.

**Audit chain.** The hash-chained `audit_log` table. Each entry includes the hash of the previous one, so any later edit breaks the chain and is detectable with `verify_chain`.

**Bins.** The filesystem folders a document moves through: `inbox → processing/<worker_id>/ → archive | review | failed`. Only helpers in `pipeline/bins.py` move files.

**Boss.** The escalation agent for conflicting extractions. Also runs as a scheduled sweep (`pipeline.ops_monitor`).

**Confidence gate.** The thresholds in `taxonomy.yaml` (`confidence.low`, `confidence.high`, `retry_max`) that decide whether a result proceeds, retries, or goes to review.

**First pass (STP).** A document that archived in one hop with no retry, reviewer, arbiter, boss, human review or guardrail. Reported as the `success_rate` score. Straight-through processing.

**Gmail triage lane.** An auxiliary flow outside the 13-node graph. Single-attachment emails go through a free OpenRouter model for classification and key-field extraction; multi-attachment or oversized documents go to the full pipeline.

**Guardrails.** Deterministic checks (`pipeline/guards.py`) after every LLM call. A violation lowers confidence below the routing threshold so bad output retries or goes to review.

**Intake clerk.** The deterministic first step of intake (`agents/intake.py`): transcribe, normalize and prepare the text. An optional LLM pass cleans messy documents.

**Judge.** The completeness check in the extraction quality lane. Also the offline LLM-as-a-judge evaluators.

**Lane A.** The second-opinion reviewer for classification when the sorter's confidence stays in the medium band after retries.

**Lane B.** The judge-plus-arbiter quality lane for extraction.

**Manifest.** The per-document JSON record that travels with the file and is the authority for its final state.

**Matter.** A group of related documents, such as one legal case. Used as the Langfuse session id.

**Relations clerk.** An auxiliary flow that runs after archiving and finds associations between documents (same matter, shared parties, similar text).

**Sorter.** The classification agent. Decides the document class and subclass.

**Specialist.** The extraction agent for one document class, such as `contracts_specialist` or `insurance_claims_specialist`.

**Taxonomy.** `config/taxonomy.yaml` in llm-mailroom: the single source of truth for document classes, thresholds, agents and model assignments.

## Data

**Blind config / ground-truth config.** The two halves of `mailroom-dataset`. `default` has the document text and no labels; `ground_truth` has the labels. They join on `filename`. Full breakdown: [Mailroom dataset](../mailroom-dataset/mailroom-dataset.md).

**Class / subclass.** The two levels of document type. Five classes (`contract`, `merger_agreement`, `corporate_record`, `correspondence`, `insurance_claim`); each has subclasses such as `all_cash` or `bylaws`.

**Corpus feed.** A repository that produces documents for one class, such as Enron-Evaluation-Environment for `correspondence`.

**CUAD, MAUD, LegalBench.** Public legal datasets: CUAD (contract clauses), MAUD (merger agreements), LegalBench (legal reasoning tasks).

**DE-SynPUF.** CMS's synthetic Medicare claims data, the source for most insurance claims.

**Pin.** A fixed dataset revision or package tag. The constellation never reads live tips.

**Stratum.** One class-by-subclass cell. The canonical dataset has 55.

## Scoring and evaluation

**Champion.** The current best prompt version for an agent, promoted from the experiment loop.

**Embedding rescue.** A second similarity signal for names and free text when the string score is ambiguous.

**Experiment log.** An append-only `experiment_log.jsonl` (plus a rendered markdown table) that records every evaluation run.

**Field-type-aware scoring.** Scoring each field by its kind: dates and money normalized before comparison, names by fuzzy match, lists by optimal matching, free text by token F1.

**GEPA.** The prompt-mutation loop that proposes new prompt candidates from a frozen baseline.

**Mock mode.** A deterministic fake LLM used to run the machinery with no network and no spend.

**Tiers (T0 to T3).** The metric registry's importance levels in llm-dojo-scoring, from T0 headline metrics to T3 logged-only.

## Repositories and workflow

**Card.** A task on a board. Prefixes: `DMR-` (monorepo hub), `HUB-` (earlier hub era), `KANBAN-` (entity-extraction and mailroom shared board), `SAND-` (sandbox).

**Dispatch Board.** The served, issue-backed web view of the monorepo board at [https://digital-mailroom-theta.vercel.app](https://digital-mailroom-theta.vercel.app).

**Monorepo.** Digital-Mailroom: every package as a git subtree in one `uv` workspace.

**Observatory.** The hosted, public operations view of The-Mailroom.

**Producer.** The llm-mailroom API when it is serving other tools (The-Mailroom's Inbox and REVIEW desk).

**Schema mirror duty.** The rule that The-Mailroom updates its pipeline schema files in the same change window as any pipeline change to spans, nodes, agents, classes, thresholds or judge scores.

**Vendored snapshot.** A tracked copy of another package's code inside a repo (local-mailroom-sandbox's `vendor/`), so it runs without network access.

**Virtual member.** A monorepo package with `package = false`. Its code and data sit in the checkout but nothing installs from it (Enron-Evaluation-Environment, claims-data-eda, llm-mailroom-graph, mailroom-corpus-eda).
