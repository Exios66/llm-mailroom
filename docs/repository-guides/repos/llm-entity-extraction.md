# llm-entity-extraction

**The prompt experiment loop: where sorter and specialist prompts are bred and measured.**

| | |
| :--- | :--- |
| Repository | [Exios66/llm-entity-extraction](https://github.com/Exios66/llm-entity-extraction) |
| Monorepo path | `packages/llm-entity-extraction` |
| Experiment log site | [exios66.github.io/llm-entity-extraction](https://exios66.github.io/llm-entity-extraction/) |
| Board | `governance/MESSAGE_BOARD.md`, shared with llm-mailroom |

## What it does

Each evaluation tests **one prompt version at a time** against models over CUAD, LegalBench and MAUD corpora, using the same LangChain agents the pipeline runs. Every run appends one record to `reports/experiment_log.jsonl`, rendered as a markdown log and as a filterable website.

The sorter is evaluated on three jobs:

1. **Vision classification** of real PDFs, all pages in one call.
2. **LegalBench multi-class tasks**, such as CUAD clause questions and the MAUD question suite.
3. **Hierarchical document classification**: class plus subclass (`sorter_docclass_v7` is the champion).

Specialists are evaluated on field extraction; the champion contracts prompt is `contracts_specialist_v31` at the time of writing.

## How its output reaches production

Winning prompts are vendored into llm-mailroom (`langchain_agents/`) with their full version lineage. llm-mailroom also keeps a synced copy of this repo's experiment log at `docs/reports/experiments/experiment_log.md`, which must never be hand-edited.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt                       # core: agent, prompt, scoring
cp config/environments/.env.example config/environments/.env    # OPENROUTER_API_KEY
pip install -r requirements/tracing.txt               # add batches as needed: tracing, evals, datasets, reporting, embeddings, dev
```

Then run the loop with the scripts under `scripts/eval/` (for example `run_classification_eval.py`). The README's command gallery covers classification, extraction, chained, subtype and A/B runs.

## Rules worth knowing

- Tracing defaults to local Arize Phoenix. Braintrust hosts eval datasets read-only; its experiment logging is off by default to save quota.
- Scoring comes from llm-dojo-scoring; the local scoring modules are thin re-export shims.
- Prompt versions are append-only. Add a new version rather than editing one.

## Its documentation

- [README](https://github.com/Exios66/llm-entity-extraction/blob/main/README.md)
- [docs/SCORING.md](https://github.com/Exios66/llm-entity-extraction/blob/main/docs/SCORING.md) — how runs are scored
- [docs/configuration.md](https://github.com/Exios66/llm-entity-extraction/blob/main/docs/configuration.md)
- [docs/sister-repos.md](https://github.com/Exios66/llm-entity-extraction/blob/main/docs/sister-repos.md)
- Wiki source in [docs/wiki/](https://github.com/Exios66/llm-entity-extraction/tree/main/docs/wiki): Getting Started, Architecture, Eval Runners, Experiment Log, Scoring, Phoenix Tracing, Langfuse Traces, Annotation Queues, Release Process
- [governance/MESSAGE_BOARD.md](https://github.com/Exios66/llm-entity-extraction/blob/main/governance/MESSAGE_BOARD.md) — the shared board
