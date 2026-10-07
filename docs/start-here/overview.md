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

In August 2026 they were gathered into one monorepo, **Digital-Mailroom**, which holds every package as a git subtree inside a single `uv` workspace. The monorepo is now where cross-repo development happens; the individual `Exios66/*` repositories remain the release vehicles that deployments install from.

## The constellation at a glance

### Hub and coordination

| Repository                                                         | What it is                                                                                                                                     |
| ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| [Digital-Mailroom](../repository-guides/repos/digital-mailroom.md) | The monorepo. Every package below lives in `packages/`. Holds the cross-repo task board (`governance/TASKS.md`) and the served Dispatch Board. |
| [mailroom-issues](../repository-guides/repos/mailroom-issues.md)   | The issue hub for anything that spans more than one repository: epics, RFCs, and the label taxonomy. No code.                                  |

### The ten packages (monorepo members)

| Package                                                                                    | Layer              | What it is                                                                                                                                                                                                                                                                                                                                                                                             |
| ------------------------------------------------------------------------------------------ | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [llm-mailroom](../repository-guides/repos/llm-mailroom.md)                                 | Pipeline           | The 13-node LangGraph pipeline and FastAPI producer. The center of everything.                                                                                                                                                                                                                                                                                                                         |
| [llm-dojo-scoring](../repository-guides/repos/llm-dojo-scoring.md)                         | Scoring            | The shared, deterministic, field-type-aware scoring library every other repo imports.                                                                                                                                                                                                                                                                                                                  |
| [llm-entity-extraction](../repository-guides/repos/llm-entity-extraction.md)               | Prompt experiments | Where sorter and specialist prompts are bred and measured before they reach the pipeline.                                                                                                                                                                                                                                                                                                              |
| [local-mailroom-sandbox](../repository-guides/repos/local-mailroom-sandbox/)               | Surface            | Runs the full pipeline offline on Ollama, vLLM, llama.cpp or LM Studio, plus Modal GPU studies. GitBook: [docs](../repository-guides/repos/local-mailroom-sandbox/local-mailroom-sandbox-docs.md), [run reports](../repository-guides/repos/local-mailroom-sandbox/local-mailroom-sandbox-reports.md), [visuals](../repository-guides/repos/local-mailroom-sandbox/local-mailroom-sandbox-visuals.md). |
| [The-Mailroom](../repository-guides/repos/the-mailroom.md)                                 | Surface            | Pixel-art visualizer and hosted Observatory, driven entirely by the pipeline's Langfuse traces.                                                                                                                                                                                                                                                                                                        |
| [agent-mailroom](../repository-guides/repos/agent-mailroom.md)                             | Surface            | A self-contained mailroom on a walking office floor, same doctrine, independent release train.                                                                                                                                                                                                                                                                                                         |
| [llm-mailroom-graph](../repository-guides/repos/llm-mailroom-graph.md)                     | Surface            | Interactive knowledge-graph site of the pipeline's code.                                                                                                                                                                                                                                                                                                                                               |
| [Mailroom-Corpus-EDA](../repository-guides/repos/mailroom-corpus-eda.md)                   | Data               | EDA of the canonical `mailroom-dataset` and the central Hugging Face upload helpers. GitBook: [Mailroom dataset](../mailroom-dataset/mailroom-dataset.md), [visualizations](../mailroom-dataset/visualizations.md).                                                                                                                                                                                    |
| [Enron-Evaluation-Environment](../repository-guides/repos/enron-evaluation-environment.md) | Data               | Builds the `correspondence` corpus from the CMU Enron emails.                                                                                                                                                                                                                                                                                                                                          |
| [claims-data-eda](../repository-guides/repos/claims-data-eda.md)                           | Data               | Builds the `insurance_claim` corpus from CMS DE-SynPUF Medicare claims.                                                                                                                                                                                                                                                                                                                                |

### Organization repositories outside the monorepo

| Repository                                                         | What it is                                                                            |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| [eval-environment](../repository-guides/repos/eval-environment.md) | Per-node performance evals, pilots and calibration suites over the canonical dataset. |
| [mailroom-ml](../repository-guides/repos/mailroom-ml.md)           | The ModernBERT intake fast-path classifier: training, calibration, ONNX serving.      |

### Adjacent

| Repository                                                                   | What it is                                                                               |
| ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| [atticus-investigation](../repository-guides/repos/atticus-investigation.md) | LegalBench classification prompt-engineering pipeline. Same methodology, separate focus. |

Older copies, forks and earlier monorepo attempts also exist. They are listed with their status in the [Repository index](../how-it-fits-together/repo-index.md) so nobody develops in the wrong place.

## Where the truth lives

| Question                                     | Canonical answer                                                                                                                                                                                                                                                                                                      |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| What code is current?                        | `Digital-Mailroom/packages/<name>` for development; `Exios66/<name>` for releases.                                                                                                                                                                                                                                    |
| Which document classes and thresholds exist? | `llm-mailroom`'s `config/taxonomy.yaml`.                                                                                                                                                                                                                                                                              |
| What is the dataset?                         | [`Lucius-Morningstar/mailroom-dataset`](https://huggingface.co/datasets/Lucius-Morningstar/mailroom-dataset) on Hugging Face, pinned by revision. See [Mailroom dataset](../mailroom-dataset/mailroom-dataset.md) (strata, EDA, figures) and [Data and corpora](../how-it-fits-together/data-and-corpora.md) (rules). |
| How is quality scored?                       | `llm-dojo-scoring`. Nobody re-implements a metric locally.                                                                                                                                                                                                                                                            |
| Who is working on what?                      | `Digital-Mailroom/governance/TASKS.md` for hub work; each package's own board for package work. See [Governance](../how-it-fits-together/governance.md).                                                                                                                                                              |
| Where do cross-repo bugs go?                 | `mailroom-issues`.                                                                                                                                                                                                                                                                                                    |

## Current versions

Snapshot taken 2026-10-07 from each repository's README and pins. Check the repository itself before relying on a number.

| Item             | Version                                                |
| ---------------- | ------------------------------------------------------ |
| llm-mailroom     | v0.8.0                                                 |
| llm-dojo-scoring | v0.19.1 latest; llm-mailroom pins v0.19.1              |
| The-Mailroom     | v0.5.0                                                 |
| agent-mailroom   | 0.3.0                                                  |
| mailroom-dataset | schema v9, revision `ed7576b6` (v9.1), 3,302 documents |
