# atticus-investigation

**A LegalBench classification prompt-engineering pipeline. Adjacent to the Mailroom, using the same methodology.**

| | |
| :--- | :--- |
| Repository | [Exios66/atticus-investigation](https://github.com/Exios66/atticus-investigation) |
| Relationship | Eval sibling; not a monorepo member |

## What it does

atticus-investigation runs registered prompt versions against registered models on [LegalBench](https://huggingface.co/datasets/nguha/legalbench) classification tasks. It uses OpenRouter for inference (every model pinned to specific providers, so ablations are not corrupted by silent backend changes) and Braintrust for experiment logging.

It is the methodological template the constellation follows: prompt versions times models, paired-bootstrap accuracy deltas with 95% confidence intervals, and new tasks added as pure configuration (one `TaskSpec`, one prompt file). 155 of the 162 LegalBench configs can be added this way.

## Its documentation

- [README](https://github.com/Exios66/atticus-investigation/blob/main/README.md)
- [pipeline-plan.md](https://github.com/Exios66/atticus-investigation/blob/main/pipeline-plan.md) — design spec
- [docs/task_registry.md](https://github.com/Exios66/atticus-investigation/blob/main/docs/task_registry.md) — adding tasks
- [docs/legalbench_catalog.md](https://github.com/Exios66/atticus-investigation/blob/main/docs/legalbench_catalog.md) — all 162 configs
- [docs/eda_maud.md](https://github.com/Exios66/atticus-investigation/blob/main/docs/eda_maud.md) — MAUD deep dive
- [docs/prompt_provenance.md](https://github.com/Exios66/atticus-investigation/blob/main/docs/prompt_provenance.md)
- [docs/braintrust_setup.md](https://github.com/Exios66/atticus-investigation/blob/main/docs/braintrust_setup.md)
