# Digital-Mailroom

**The integration monorepo: committed core packages in one checkout and one virtualenv.**

| | |
| :--- | :--- |
| Repository | [LLM-Mailroom-Services/Digital-Mailroom](https://github.com/LLM-Mailroom-Services/Digital-Mailroom) |
| Role | Integration hub; owns cross-repo wiring and governance, imports committed standalone source |
| Live board | [Dispatch Board](https://digital-mailroom-theta.vercel.app) |
| Wiki | [GitHub wiki](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/wiki), sourced from `docs/wiki/` |

## What it does

Digital-Mailroom imports core packages under `packages/` as git subtrees, wired into one `uv` workspace with one `uv.lock`. [DMR-093](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/issues/165) defines a **five-package core**: `llm-dojo-scoring`, `llm-mailroom`, `local-mailroom-sandbox`, `The-Mailroom`, and `mailroom-corpus-eda`. The layout below is that target until the separate hub pruning PR lands after [#162](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/pull/162); hub `main` retains the seven-member DMR-079 layout in the meantime. The target installs four built packages editable and retains `mailroom-corpus-eda` as the sole virtual member.

Author and test package implementation and documentation in the owning standalone repository first, then commit and merge to its own `main` before Digital-Mailroom imports the committed source with `scripts/sync_packages.py pull`. The hub owns integration wiring, governance, and generated vendor snapshots.

It also carries the governance tooling for the whole family: the cross-repo task board, the served Dispatch Board, label and taxonomy drift checks, and the hub release chain.

## Quick start

```bash
git clone https://github.com/LLM-Mailroom-Services/Digital-Mailroom.git
cd Digital-Mailroom
uv sync
uv run pytest packages/llm-mailroom/src/tests   # one package per invocation
```

## Layout (DMR-093 target)

```
Digital-Mailroom/
├── AGENTS.md            workspace rules and cross-package conventions
├── governance/          TASKS.md (the DMR board) and governance tooling
├── board-site/          the served Dispatch Board (Vercel)
├── docs/                v7 taxonomy contract, docclass contract, wiki source
├── scripts/             board_state, sync_packages, taxonomy_parity, release_chain, ...
└── packages/            the five core packages
    ├── llm-dojo-scoring
    ├── llm-mailroom
    ├── local-mailroom-sandbox
    ├── The-Mailroom
    └── mailroom-corpus-eda
```

| Package | Installed? |
| :--- | :--- |
| `llm-mailroom`, `llm-dojo-scoring`, `The-Mailroom`, `local-mailroom-sandbox` | Yes, editable |
| `mailroom-corpus-eda` | No: the one virtual core member (`package = false`) |

## External corpus feeders and preservation (DMR-093 target)

The two feeds remain active standalone repositories. The branches below preserve the full hub checkout at `59c879d9`, with the original package paths intact. Final PR162 must retain these exact feeder package trees before pruning.

| Feeder guide | Standalone source | Published hub preservation |
| :--- | :--- | :--- |
| [Enron-Evaluation-Environment](enron-evaluation-environment.md) | [Exios66/Enron-Evaluation-Environment](https://github.com/Exios66/Enron-Evaluation-Environment) | [feature/enron-evaluation-environment](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/tree/feature/enron-evaluation-environment/packages/Enron-Evaluation-Environment) |
| [claims-data-eda](claims-data-eda.md) | [Exios66/claims-data-eda](https://github.com/Exios66/claims-data-eda) | [feature/claims-data-eda](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/tree/feature/claims-data-eda/packages/claims-data-eda) |

Use a separate checkout or worktree of a preservation branch to inspect the original complete workspace. Any selective restoration to the five-package core must deliberately reconcile membership, sync entries, dependencies, and tests.

## Key tools

| Command | What it does |
| :--- | :--- |
| `python scripts/sync_packages.py status` | Drift between each subtree and its `Exios66/*` upstream |
| `python scripts/sync_packages.py pull --package <name> --squash` | Import committed standalone source after its own `main` merge |
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
