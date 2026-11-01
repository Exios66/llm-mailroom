# Overview

## What the Mailroom does

The Mailroom is a document-processing system for legal and business paperwork. A document arrives (by upload, by watched folder, or by email), and the Mailroom:

1. **Takes it in.** It transcribes PDFs and images, cleans the text, and records where it came from.
2. **Classifies it.** A sorter agent decides which of five document classes it belongs to (`contract`, `merger_agreement`, `corporate_record`, `correspondence`, `insurance_claim`) and which subclass inside that class.
3. **Extracts fields.** A specialist agent for that class pulls out the fields that matter: parties, dates, amounts, obligations, claim numbers, and so on.
4. **Checks its own work.** Confidence gates, a judge and an arbiter, a "boss" escalation agent, and a human review queue catch low-confidence or conflicting results.
5. **Files it.** A procedural reporter writes the report, the catalog is updated, and the document is archived with a hash-chained audit log entry that makes later tampering detectable.

Every step is traced (Langfuse, Braintrust or Arize Phoenix), every prompt is versioned, and every claim about quality is backed by a deterministic score.

## Why there are so many repositories

The pipeline is only one part of the work. Around it sit the things a production LLM system needs: datasets with ground truth, a scoring library everyone agrees on, an experiment loop for prompts, an evaluation harness for each node, a cheaper ML classifier for easy documents, a way to run everything offline, and tools to watch it run. Each of those grew up as its own repository with its own release train.

Digital-Mailroom integrates committed core packages as git subtrees inside a single `uv` workspace. Each standalone repository remains authoritative for its implementation, documentation, and release train. Author and test package implementation and documentation in the owning standalone repository first, then commit and merge to its own `main` before Digital-Mailroom imports the committed source with `scripts/sync_packages.py pull`. The hub owns integration wiring, governance, and generated vendor snapshots.

## The constellation at a glance

### Hub and coordination

| Repository | What it is |
| :--- | :--- |
| [Digital-Mailroom](repos/digital-mailroom.md) | The integration hub. Imports committed standalone source into `packages/` and refreshes vendor snapshots. Holds the cross-repo task board (`governance/TASKS.md`) and the served Dispatch Board. |
| [mailroom-issues](repos/mailroom-issues.md) | The issue hub for anything that spans more than one repository: epics, RFCs, and the label taxonomy. No code. |

### The five core packages (DMR-093 target)

[DMR-093](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/issues/165) defines the target below. Until the separate hub pruning PR lands after [#162](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/pull/162), hub `main` retains the seven members left by DMR-079. The target has four built packages and one virtual member, `mailroom-corpus-eda`.

| Package | Layer | What it is |
| :--- | :--- | :--- |
| [llm-mailroom](repos/llm-mailroom.md) | Pipeline | The 13-node LangGraph pipeline and FastAPI producer. The center of everything. |
| [llm-dojo-scoring](repos/llm-dojo-scoring.md) | Scoring | The sole owner of all metrics, scoring formulas, normalization rules, and suites; consumers import its APIs. |
| [local-mailroom-sandbox](repos/local-mailroom-sandbox.md) | Surface | Owns the local/offline harness on Ollama, vLLM, llama.cpp or LM Studio and the Modal/vLLM test interface; consumes committed pipeline and dojo source. GitBook: [docs](repos/local-mailroom-sandbox-docs.md), [run reports](repos/local-mailroom-sandbox-reports.md), [visuals](repos/local-mailroom-sandbox-visuals.md). |
| [The-Mailroom](repos/the-mailroom.md) | Surface | The visualizer and hosted Observatory used by both pipeline and sandbox, through compatible producer and trace contracts. |
| [Mailroom-Corpus-EDA](repos/mailroom-corpus-eda.md) | Data | EDA of the canonical `mailroom-dataset` and the central Hugging Face upload helpers. GitBook: [Mailroom dataset](../mailroom-dataset/), [visualizations](../mailroom-dataset/visualizations.md). |

### External corpus feeders (DMR-093 target)

These standalone feeds and their guides remain active. The published preservation branches retain the full hub checkout at `59c879d9`, including each original package path. Final PR162 must retain these exact feeder package trees before pruning.

| Repository | What it is | Published hub preservation |
| :--- | :--- | :--- |
| [Enron-Evaluation-Environment](repos/enron-evaluation-environment.md) | Builds the `correspondence` corpus from the CMU Enron emails. | [feature/enron-evaluation-environment](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/tree/feature/enron-evaluation-environment/packages/Enron-Evaluation-Environment) |
| [claims-data-eda](repos/claims-data-eda.md) | Builds the `insurance_claim` corpus from CMS DE-SynPUF Medicare claims. | [feature/claims-data-eda](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/tree/feature/claims-data-eda/packages/claims-data-eda) |

### Organization repositories outside the monorepo

| Repository | What it is |
| :--- | :--- |
| [eval-environment](repos/eval-environment.md) | Per-node performance evals, pilots and calibration suites over the canonical dataset. |
| [mailroom-ml](repos/mailroom-ml.md) | The ModernBERT intake fast-path classifier: training, calibration, ONNX serving. |

### Adjacent

| Repository | What it is |
| :--- | :--- |
| [atticus-investigation](repos/atticus-investigation.md) | LegalBench classification prompt-engineering pipeline. Same methodology, separate focus. |

Older copies, forks, earlier monorepo attempts and the repositories retired from the core in DMR-079 (llm-entity-extraction, agent-mailroom, llm-mailroom-graph) also exist. They are listed with their status in the [Repository index](repo-index.md) so nobody develops in the wrong place.

## Where the truth lives

| Question | Canonical answer |
| :--- | :--- |
| What code is current? | The owning standalone `Exios66/<name>` repository for implementation, documentation, and releases; Digital-Mailroom imports its merged commits for integration. |
| Which document classes and thresholds exist? | `llm-mailroom`'s `config/taxonomy.yaml`. |
| What is the dataset? | [`Lucius-Morningstar/mailroom-dataset`](https://huggingface.co/datasets/Lucius-Morningstar/mailroom-dataset) on Hugging Face, pinned by revision. See [Mailroom dataset](../mailroom-dataset/) (strata, EDA, figures) and [Data and corpora](data-and-corpora.md) (rules). |
| How is quality scored? | `llm-dojo-scoring`. Nobody re-implements a metric locally. |
| Who is working on what? | `Digital-Mailroom/governance/TASKS.md` for hub work; each package's own board for package work. See [Governance](governance.md). |
| Where do cross-repo bugs go? | `mailroom-issues`. |

## Current versions

Package versions and pins refreshed 2026-10-23 from each repository's README and pins; the Dojo release and consumer pin were updated 2026-10-25. Check the repository itself before relying on a number.

| Item | Version |
| :--- | :--- |
| llm-mailroom | v0.8.1 |
| llm-dojo-scoring | v0.24.2 latest; llm-mailroom pins e35504db12f09aeda07ae116663c177acd8c0d6e |
| The-Mailroom | v0.5.0 |
| mailroom-dataset | schema v9, revision `ed7576b6` (v9.1), 3,302 documents |
