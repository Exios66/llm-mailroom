---
name: legalbench
description: LegalBench evaluation suite inside llm-mailroom (contract_qa and family_classification). Use when running legalbench.cli, CUAD binary QA, or family classification evals — not for document-pipeline ingest of legalbench-full.
---

# LegalBench suite

**When:** `python -m legalbench.cli`, CUAD yes/no clause questions, or 25-family
classification.  
**Not** a mailroom document-pipeline ingest. Hub `legalbench-full` is a
LegalBench task pack — do not feed it to `run_hf_pilot.py`.

## Tasks

| Task | What | Scoring |
| --- | --- | --- |
| `contract_qa` | CUAD annotations (510 × 41 = 20,910 yes/no + evidence) | accuracy, macro per-category, yes-F1, ECE |
| `family_classification` | 200 contracts → 25 families + `other` | strict + equiv accuracy, macro-F1 |

Deterministic local scoring — no LLM judge. Traces as `legalbench-<task>`
with `legalbench_*` scores. Appends the shared experiment log (sibling
`llm-entity-extraction` site data when `LEGALBENCH_SIBLING_REPO` points there).

```bash
PYTHONPATH=src python -m legalbench.cli --list-tasks
PYTHONPATH=src python -m legalbench.cli --task contract_qa --n 30 --model qwen/qwen3.7-flash
PYTHONPATH=src python -m legalbench.cli --task family_classification --n 20 --mock
```

`--mock` uses a deterministic fake model (`mock/mock-legalbench` in the log).
Corpus is local (`data/cuad/`). Reuses `BaseAgent` retry/usage — no extra deps.

Do **not** hand-edit `docs/reports/experiments/experiment_log.md` (synced
mirror). Observation name for answers: `answer-question` (index in metadata).

## Related

- Router: [mailroom-tool-router](../mailroom-tool-router/SKILL.md)
- Pipeline ingest corpora: [huggingface](../huggingface/SKILL.md)
- Tracing: [langfuse](../langfuse/SKILL.md)
