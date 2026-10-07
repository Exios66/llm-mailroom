# llm-mailroom

**The document pipeline at the center of the constellation. This site is hosted from its `docs/` folder.**

| | |
| :--- | :--- |
| Repository | [Exios66/llm-mailroom](https://github.com/Exios66/llm-mailroom) |
| Monorepo path | `packages/llm-mailroom` (Python package name `mailroom`) |
| Release | v0.8.0 |
| Depends on | llm-dojo-scoring (pinned), mailroom-ml (optional) |

## What it does

llm-mailroom runs one LangGraph state machine per document across 13 nodes: intake, classify, retry classify, review classify, extract, retry extract, judge, arbiter, human review, boss escalation, compile report, write catalog, archive. On the happy path a document costs two LLM calls (classify and extract). Everything else is a safety net that only fires when confidence is low or results conflict.

It exposes a FastAPI service (port 8000) that embeds the inbox watcher, accepts uploads, reports status and serves the audit trail. The same API is the "producer" The-Mailroom calls for its Inbox and REVIEW desk.

## Key ideas

- **The taxonomy is the single source of truth.** Document classes, thresholds, agent-to-model mapping and token caps all live in `config/taxonomy.yaml`. Nothing is hardcoded.
- **Provider-agnostic.** Every LLM call goes through `get_llm(agent_name)`. OpenRouter is the default; local providers are a configuration change (see [Local models](../../local-models.md)).
- **SQLite first.** The catalog, audit log and optional checkpoints are plain SQLite files. Postgres is opt-in.
- **Traced end to end.** Langfuse, Braintrust or Arize Phoenix, chosen automatically from available keys. Tracing never silently turns off.
- **No truncation.** Long documents are read in overlapping windows, never cut.

## Quick start

See [Getting started, path 2](../getting-started.md#2-run-the-pipeline-and-push-a-document-through-it).

## Its documentation

The pipeline reference pages are part of this site:

- [Pipeline architecture](../../architecture.md)
- [Agents](../../agents.md)
- [Configuration](../../configuration.md)
- [API](../../api.md)
- [Gmail intake](../../gmail-intake.md)
- [Deployment](../../deployment.md)
- [Docker](../../docker-deployment.md)
- [Modal + vLLM](../../modal-vllm.md)
- [Operational procedure](../../operational-procedure.md)
- [Local models](../../local-models.md)
- [Testing](../../testing.md)
- [Sister repositories](../../sister-repos.md)

Also in the repository: the [README](https://github.com/Exios66/llm-mailroom/blob/main/README.md), [AGENTS.md](https://github.com/Exios66/llm-mailroom/blob/main/AGENTS.md) (the contributor and agent manual, including every script command), [CHANGELOG.md](https://github.com/Exios66/llm-mailroom/blob/main/CHANGELOG.md) (the GitBook [Changelog](https://mailroom-inc.gitbook.io/mailroom-inc.-docs/changelog) section is a generated mirror of that file), the [notebooks](https://github.com/Exios66/llm-mailroom/tree/main/notebooks) (00 to 14, from pipeline anatomy to the Gmail pilot), and [deploy/](https://github.com/Exios66/llm-mailroom/tree/main/deploy) (compose files, Modal app, Space payload — operator guides are the Docker and Modal pages above).
