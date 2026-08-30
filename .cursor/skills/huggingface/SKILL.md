---
name: huggingface
description: Hugging Face Hub corpora for llm-mailroom (docclass-merged v5, run_hf_pilot, HF_TOKEN) and the producer Docker Space publisher (publish_space.py, MAILROOM_PIPELINE_URL). Use for Hub datasets, class/subtype examples, Enron/CMS/CUAD pulls, or publishing the REVIEW-resolve producer; prefer committed fixtures and --mock when network-free work is enough. Never invent stand-in class texts.
---

# Hugging Face (pipeline corpora)

**When:** Hub dataset work, `run_hf_pilot.py`, `HF_TOKEN`, class/subtype
examples, or Modal/vLLM weight downloads.  
**Prefer** committed fixtures / `--mock` for default tests. Never require Hub
in pytest. Never invent stand-in texts for a class or subtype.

## What to load

| Asset | Id / path | Network? |
| --- | --- | --- |
| Targeted full corpus | `Lucius-Morningstar/docclass-merged` schema **v5** (1,210 docs) | Yes (`--real`) |
| Class × subclass pack | `docclass-pilot` (48 strata) + `notebooks/fixtures/huggingface/class_subclass_examples.json` | JSON pack is offline |
| Enron correspondence | `enron-correspondence-dedup` (~247k) — **do not** load all rows by default | Yes |
| Local PDFs | `docs/examples/samples/` | No — fixtures, not the class catalog |
| `compliance_filing` | **zero Hub rows** — local pack / mock only | No |

Registry: `src/pipeline/hf_corpora.py`. `--real` is gated by
`scripts/prepare_samples.py:is_real_sample` (synthetics are mock-only).

```bash
PYTHONPATH=src python src/scripts/run_hf_pilot.py --check
PYTHONPATH=src python src/scripts/run_hf_pilot.py --mock
PYTHONPATH=src python src/scripts/run_hf_pilot.py --examples --mock
# needs HF_TOKEN + OPENROUTER_API_KEY; real Hub rows only
PYTHONPATH=src python src/scripts/run_hf_pilot.py --real
```

Session `pilot-hf-<stamp>`, tag `source-docclass-merged` (or the corpus
`source-*` from `hf_corpora.py`). GT on trace input/metadata:
`expected_hf_class`, `expected_doc_class`, `expected_subclass`.

## Auth

```bash
export HF_TOKEN=hf_...
```

Modal/vLLM gated weights: [modal](../modal/SKILL.md). Full Hub CLI depth:
Cursor **hf-cli** plugin skill — this skill stays mailroom-scoped.

## Producer Space (Observatory + REVIEW)

The-Mailroom Observatory ([PR #30](https://github.com/Exios66/The-Mailroom/pull/30))
needs a public HTTP producer for Inbox **Queue a document**
(`POST /v1/upload`) and REVIEW resolve. That image is this repo's root
`Dockerfile` (`python -m api.main` on `:7860`), not a Hub dataset. The
floor Space is published from The-Mailroom (`mailroom-observatory`).

```bash
PYTHONPATH=src python src/scripts/publish_space.py --check
PYTHONPATH=src python src/scripts/probe_hosted_spaces.py --offline
HF_TOKEN=hf_... MAILROOM_API_TOKEN=change-me \
  PYTHONPATH=src python src/scripts/publish_space.py --repo Lucius-Morningstar/mailroom-producer
```

Live pair (Hub user ``Lucius-Morningstar``): Observatory
https://lucius-morningstar-mailroom-observatory.hf.space — producer
https://lucius-morningstar-mailroom-producer.hf.space. Then set on
The-Mailroom / its Space secrets:

```
MAILROOM_PIPELINE_URL=https://lucius-morningstar-mailroom-producer.hf.space
MAILROOM_PIPELINE_TOKEN=$MAILROOM_API_TOKEN
MAILROOM_PIPELINE_API_PREFIX=/v1
```

`--check` is network-free. Never bake tokens into the Space git tree.
Checklist: [`deploy/space/PAIRING.md`](../../../deploy/space/PAIRING.md).
Re-probe: `PYTHONPATH=src python src/scripts/probe_hosted_spaces.py`.

## Boundaries

- `legalbench-full` is a LegalBench CLI pack, not document-pipeline ingest
  ([legalbench](../legalbench/SKILL.md)).
- `merger_agreement` is its own class (not a CUAD alias).
- Attribution: `docs/examples/samples/ATTRIBUTION.md`.

## Related

- Router: [mailroom-tool-router](../mailroom-tool-router/SKILL.md)
- Scoring: [dojo-scoring](../dojo-scoring/SKILL.md)
