# Documentation

Dedicated operator manuals for the offline sandbox ([Exios66/local-mailroom-sandbox](https://github.com/Exios66/local-mailroom-sandbox)). Canonical files live in that repository's `docs/`; this page is the GitBook map so every dedicated sandbox doc is on the site TOC. Run results: [Run reports](local-mailroom-sandbox-reports.md). Charts and `sandbox watch` stills: [Visuals](local-mailroom-sandbox-visuals.md). Pipeline GPU serve (this repo): [Modal + vLLM](../../../pipeline-reference-llm-mailroom/deployment/modal-vllm.md).

Parent guide: [local-mailroom-sandbox](./).

## Setting up

| Page                                                                                                               | What it covers                                        |
| ------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| [QUICKSTART.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/setting-up/QUICKSTART.md)         | Install, full CLI reference, workflows                |
| [LAYOUT.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/setting-up/LAYOUT.md)                 | Which folder owns what (frozen by tests)              |
| [providers.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/setting-up/providers.md)           | Ollama, vLLM, Modal, llama.cpp, LM Studio, OpenRouter |
| [remote-serving.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/setting-up/remote-serving.md) | Remote OpenAI `/v1` cutover                           |
| [docker-offline.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/setting-up/docker-offline.md) | Offline Docker path                                   |
| [sister-repos.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/setting-up/sister-repos.md)     | How the sandbox sits next to the pipeline             |

## Running and evaluating

| Page                                                                                                  | What it covers                           |
| ----------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| [SANDBOX-GUIDE.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/SANDBOX-GUIDE.md) | End-to-end sandbox guide                 |
| [evals.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/evals.md)                 | Runners, matrix, scoring, experiment log |
| [tracing.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/tracing.md)             | Langfuse v4 data model and tags          |
| [jobs.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/jobs.md)                   | Long-running jobs (`sandbox run`)        |
| [datasets/](https://github.com/Exios66/local-mailroom-sandbox/tree/main/docs/datasets)                | Dataset card                             |

## GPU studies and runbooks

| Page                                                                                                                      | What it covers                           |
| ------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| [BENCHMARK-PROGRAM.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/BENCHMARK-PROGRAM.md)             | Benchmark program                        |
| [scale-matrix.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/scale-matrix.md)                       | Scale matrix                             |
| [SPECIALIST-GRID-PLAN.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/SPECIALIST-GRID-PLAN.md)       | Specialist grid (SAND-37 family)         |
| [RUN-COST-DERIVATION.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/RUN-COST-DERIVATION.md)         | How GPU $ / document is derived          |
| [modal/benchmark-l4.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/modal/benchmark-l4.md)           | L4 catalog pointer                       |
| [modal/modal-doc-jobs.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/modal/modal-doc-jobs.md)       | Modal document jobs                      |
| [modal/modal-serving-ops.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/modal/modal-serving-ops.md) | Modal serving operations                 |
| [runbooks/](https://github.com/Exios66/local-mailroom-sandbox/tree/main/docs/runbooks)                                    | Operator runbooks per specialist and GPU |

## Watch UI

| Page                                                                                                                                                    | What it covers                                                    |
| ------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| [pretty-logging/mailroom-themed-logging.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/pretty-logging/mailroom-themed-logging.md) | `sandbox watch`                                                   |
| [pretty-logging/mailroom-watch-web.md](https://github.com/Exios66/local-mailroom-sandbox/blob/main/docs/pretty-logging/mailroom-watch-web.md)           | Browser watch cheat sheet                                         |
| [docs/assets/watch/](https://github.com/Exios66/local-mailroom-sandbox/tree/main/docs/assets/watch)                                                     | Captured stills (on [Visuals](local-mailroom-sandbox-visuals.md)) |

## Related

* [Run reports](local-mailroom-sandbox-reports.md)
* [Visuals](local-mailroom-sandbox-visuals.md)
* [local-mailroom-sandbox](./)
* Pipeline [Docker](../../../pipeline-reference-llm-mailroom/deployment/docker-deployment.md) and [Modal + vLLM](../../../pipeline-reference-llm-mailroom/deployment/modal-vllm.md)
