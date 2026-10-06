# mailroom-ml

**The ModernBERT intake fast-path classifier: training, calibration, evaluation and ONNX serving.**

| | |
| :--- | :--- |
| Repository | [LLM-Mailroom-Services/mailroom-ml](https://github.com/LLM-Mailroom-Services/mailroom-ml) |
| Status | Standalone contractor repo, not a governed monorepo member |
| Serves | llm-mailroom, eval-environment, Mailroom-Corpus-EDA, and the mailroom-issues intake-overhaul epic (#85) |
| Board | `governance/TASKS.md` (light tracking) |

## What it does

Most documents are easy to classify. mailroom-ml trains a ModernBERT encoder that handles those quickly and cheaply, so the LLM sorter only has to deal with the messy, ambiguous and sparse tail.

- **Heads.** One `doc_type` head for the five classes, plus one subclass head per class. Head vocabularies come from the observed ground truth, not a hardcoded list.
- **Windowing.** 8,192-token context with 512-token overlap and a plurality vote across windows.
- **Calibration.** Temperature scaling per head, and an out-of-distribution energy probe.
- **Routing.** A document takes the fast path only when calibrated probability, window agreement and margin together clear the gate. Everything else, including catch-all `other`, goes to the LLM.
- **Serving.** ONNX on CPU is the primary path; a Modal app is the fallback.

## How it connects to the pipeline

llm-mailroom's intake node can call it through `agents/bert_intake.py`. The integration is fail-open: with `MAILROOM_BERT_INTAKE` off (the default), the package missing, the model missing, or any error, intake carries on with the deterministic clerk. The result is always recorded as an `intake_handoff` so downstream steps can rely on it being present.

## Quick start

```bash
git clone https://github.com/LLM-Mailroom-Services/mailroom-ml.git
cd mailroom-ml
uv sync --extra dev
uv run pytest -m "not fullcorpus"
```

Extras: `train` (torch, transformers), `serve` (onnxruntime, fastapi), `deploy` (modal). The core tests pass without any of them.

## Rules worth knowing

- Pin Hub revisions; never train or evaluate on a live tip. The canonical eval corpus is `mailroom-dataset` at `ed7576b6`; training uses `mailroom-modernbert-training`.
- The training set is leak-free and clerk-normalized. Do not reintroduce a title or filename label leak.
- Subclass surfaces are consumed from the canonical taxonomy, never redefined here.

## Its documentation

- [README](https://github.com/LLM-Mailroom-Services/mailroom-ml/blob/main/README.md)
- [AGENTS.md](https://github.com/LLM-Mailroom-Services/mailroom-ml/blob/main/AGENTS.md)
- [docs/mailroom-modernbert-classifier-model-card.md](https://github.com/LLM-Mailroom-Services/mailroom-ml/blob/main/docs/mailroom-modernbert-classifier-model-card.md) — model card
- [docs/intake-classifier-combined-plan.md](https://github.com/LLM-Mailroom-Services/mailroom-ml/blob/main/docs/intake-classifier-combined-plan.md) — the plan
- [docs/enrichment-publish-runbook.md](https://github.com/LLM-Mailroom-Services/mailroom-ml/blob/main/docs/enrichment-publish-runbook.md) — enrichment tiers and publishing
