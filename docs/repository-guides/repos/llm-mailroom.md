# llm-mailroom

**The document pipeline at the center of the constellation. This site is hosted from its `docs/` folder.**

|               |                                                                 |
| ------------- | --------------------------------------------------------------- |
| Repository    | [Exios66/llm-mailroom](https://github.com/Exios66/llm-mailroom) |
| Monorepo path | `packages/llm-mailroom` (Python package name `mailroom`)        |
| Release       | v0.8.0                                                          |
| Depends on    | llm-dojo-scoring (pinned), mailroom-ml (optional)               |

## What it does

llm-mailroom runs one LangGraph state machine per document across 13 nodes: intake, classify, retry classify, review classify, extract, retry extract, judge, arbiter, human review, boss escalation, compile report, write catalog, archive. On the happy path a document costs two LLM calls (classify and extract). Everything else is a safety net that only fires when confidence is low or results conflict.

It exposes a FastAPI service (port 8000) that embeds the inbox watcher, accepts uploads, reports status and serves the audit trail. The same API is the "producer" The-Mailroom calls for its Inbox and REVIEW desk.

## Key ideas

* **The taxonomy is the single source of truth.** Document classes, thresholds, agent-to-model mapping and token caps all live in `config/taxonomy.yaml`. Nothing is hardcoded.
* **Provider-agnostic.** Every LLM call goes through `get_llm(agent_name)`. OpenRouter is the default; local providers are a configuration change (see [Local models](../../pipeline-reference-llm-mailroom/local-models.md)).
* **SQLite first.** The catalog, audit log and optional checkpoints are plain SQLite files. Postgres is opt-in.
* **Traced end to end.** Langfuse, Braintrust or Arize Phoenix, chosen automatically from available keys. Tracing never silently turns off.
* **No truncation.** Long documents are read in overlapping windows, never cut.

## Quick start

See [Getting started, path 2](../../start-here/getting-started.md#2-run-the-pipeline-and-push-a-document-through-it).

## Its documentation

The pipeline reference pages are part of this site:

* [Pipeline architecture](../../pipeline-reference-llm-mailroom/architecture.md)
* [Agents](../../pipeline-reference-llm-mailroom/agents.md)
* [Configuration](../../pipeline-reference-llm-mailroom/configuration.md)
* [API](../../pipeline-reference-llm-mailroom/api.md)
* [Gmail intake](../../pipeline-reference-llm-mailroom/gmail-intake.md)
* [Deployment](../../pipeline-reference-llm-mailroom/deployment/)
* [Docker](../../pipeline-reference-llm-mailroom/deployment/docker-deployment.md)
* [Modal + vLLM](../../pipeline-reference-llm-mailroom/deployment/modal-vllm.md)
* [Operational procedure](../../pipeline-reference-llm-mailroom/operational-procedure.md)
* [Local models](../../pipeline-reference-llm-mailroom/local-models.md)
* [Testing](../../pipeline-reference-llm-mailroom/testing.md)
* [Sister repositories](../../pipeline-reference-llm-mailroom/sister-repos.md)

Also in the repository: the [README](https://github.com/Exios66/llm-mailroom/blob/main/README.md), [AGENTS.md](https://github.com/Exios66/llm-mailroom/blob/main/AGENTS.md) (the contributor and agent manual, including every script command), [CHANGELOG.md](https://github.com/Exios66/llm-mailroom/blob/main/CHANGELOG.md) (the GitBook [Changelog](https://mailroom-inc.gitbook.io/mailroom-inc.-docs/changelog) section is a generated mirror of that file), the [notebooks](../../../notebooks/) (00 to 14, from pipeline anatomy to the Gmail pilot), and [deploy/](../../../deploy/) (compose files, Modal app, Space payload — operator guides are the Docker and Modal pages above).
