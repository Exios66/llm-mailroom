# Digital-Mailroom

**The monorepo: every constellation package in one checkout and one virtualenv.**

| | |
| :--- | :--- |
| Repository | [LLM-Mailroom-Services/Digital-Mailroom](https://github.com/LLM-Mailroom-Services/Digital-Mailroom) |
| Role | Hub; development source of truth for cross-repo work |
| Live board | [Dispatch Board](https://digital-mailroom-theta.vercel.app) |
| Wiki | [GitHub wiki](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/wiki), sourced from `docs/wiki/` |

## What it does

Digital-Mailroom holds all ten constellation packages under `packages/` as git subtrees, each with its own history, wired into a single `uv` workspace with one `uv.lock`. A contributor clones one repository, runs `uv sync`, and has every package installed editable with no cross-repo import juggling.

It also carries the governance tooling for the whole family: the cross-repo task board, the served Dispatch Board, label and taxonomy drift checks, and the hub release chain.

## Quick start

```bash
git clone https://github.com/LLM-Mailroom-Services/Digital-Mailroom.git
cd Digital-Mailroom
uv sync
uv run pytest packages/llm-mailroom/src/tests   # one package per invocation
```

## Layout

```
Digital-Mailroom/
├── AGENTS.md            workspace rules and cross-package conventions
├── governance/          TASKS.md (the DMR board) and governance tooling
├── board-site/          the served Dispatch Board (Vercel)
├── docs/                v7 taxonomy contract, docclass contract, wiki source
├── scripts/             board_state, sync_packages, taxonomy_parity, release_chain, ...
└── packages/            the ten packages
```

| Package | Installed? |
| :--- | :--- |
| `llm-mailroom`, `llm-dojo-scoring`, `llm-entity-extraction`, `The-Mailroom`, `agent-mailroom`, `local-mailroom-sandbox` | Yes, editable |
| `Enron-Evaluation-Environment`, `claims-data-eda`, `llm-mailroom-graph`, `mailroom-corpus-eda` | No: virtual members (`package = false`) |

## Key tools

| Command | What it does |
| :--- | :--- |
| `python scripts/sync_packages.py status` | Drift between each subtree and its `Exios66/*` upstream |
| `python scripts/sync_packages.py pull --all` / `push --package <name>` | Bring upstream changes in, or publish monorepo changes out |
| `python scripts/board_state.py status` / `check` | Board snapshot and invariant check |
| `python scripts/taxonomy_parity.py` | Document-class taxonomy drift gate |
| `python scripts/github_labels.py audit` | Label taxonomy drift gate |
| `python scripts/release_chain.py status` / `cut X.Y.Z` | Hub release chain |

## Rules worth knowing

- Member `pyproject.toml` files keep their published git pins. Workspace redirection happens only through `[tool.uv.sources]`, so `pip install .` inside a package still builds the release.
- Nested `.github/workflows/` inside packages do not run here.
- Heavy assets (example PDFs, screenshots, report archives) are pruned; tests that need them skip.
- Stage explicit paths only; never `git add .` in the shared checkout.

## Its documentation

- [README](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/README.md) — the hub manual
- [AGENTS.md](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/AGENTS.md) — workspace rules
- [docs/v7-taxonomy.md](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/v7-taxonomy.md) — canonical class and subclass terminology
- [docs/DOCCLASS_CONTRACT.md](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/DOCCLASS_CONTRACT.md) — document-class contract
- [docs/TESTING.md](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/TESTING.md) — testing across the workspace
- Wiki source: [Getting Started](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/wiki/Getting-Started.md), [Architecture](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/wiki/Architecture.md), [Board Governance](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/wiki/Board-Governance.md), [Served Board](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/wiki/Served-Board.md), [Sub-Package Sync](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/wiki/Sub-Package-Sync.md), [Releases](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/wiki/Releases.md), [HF Corpus](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/wiki/HF-Corpus.md), [Offline Sandbox](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/wiki/Offline-Sandbox.md), [Subagents](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/wiki/Subagents.md)
