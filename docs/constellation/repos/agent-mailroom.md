# agent-mailroom

**The Mailroom pipeline on a walking office floor: a self-contained sibling implementation.**

| | |
| :--- | :--- |
| Repository | [Exios66/agent-mailroom](https://github.com/Exios66/agent-mailroom) |
| Monorepo path | `packages/agent-mailroom` |
| Release | 0.3.0 (mirrors llm-mailroom 0.7.1) |
| License | MIT |

## What it does

agent-mailroom runs the same doctrine as llm-mailroom (intake, classify, extract, judge, report, archive; two LLM calls on the happy path; five live classes) but as its own application, with its own release train. Documents walk across a pixel-art office: reception for the sorter, bays for the specialists, a judge chamber, the boss's office for escalation and human review, and a report and archive room.

Unlike The-Mailroom, it does not need Langfuse. It processes documents itself, stores them in SQLite plus filesystem bins, and draws its own floor. Agents communicate through file-based "hive mailboxes", one JSON file per message.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # OPENROUTER_API_KEY optional; without it the floor runs on mock
python -m agent_mailroom  # http://127.0.0.1:8000
```

Or with Docker: `docker compose up --build`, then open `http://127.0.0.1:8000/office/`.

## Key facts

- **Providers:** OpenRouter (default `qwen/qwen3.7-flash`), OpenAI, Ollama, vLLM, generic OpenAI-compatible, and `mock`. Missing keys fall back to mock.
- **API:** the same producer shape as llm-mailroom, all under `/v1` (`/v1/upload`, `/v1/datasets/pull`, `/v1/topics`, ...). With `MAILROOM_API_TOKEN` set, routes need a bearer token.
- **Datasets:** pulls rows from `Lucius-Morningstar` Hub datasets straight onto the inbox.
- **Taxonomy:** `config/taxonomy.yaml` mirrors llm-mailroom 0.7.1.
- **Desktop:** an Electron shell lives in `electron/`.

## Its documentation

- [README](https://github.com/Exios66/agent-mailroom/blob/main/README.md)
- [docs/ARCHITECTURE.md](https://github.com/Exios66/agent-mailroom/blob/main/docs/ARCHITECTURE.md)
- [AGENTS.md](https://github.com/Exios66/agent-mailroom/blob/main/AGENTS.md)
- [CHANGELOG.md](https://github.com/Exios66/agent-mailroom/blob/main/CHANGELOG.md)
