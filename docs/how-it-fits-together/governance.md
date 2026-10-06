# Governance and workflow

The constellation runs on a small set of shared rules. They exist so that several people and several AI agents can work in parallel without overwriting each other or claiming work is done when it is not.

## The working agreement (every repository)

* **Read the board first.** Before starting any task, read the board that owns it.
* **No card, no work.** Claim a card before editing. One owner per card.
* **Evidence before "done".** A card closes only with green tests for what it touched, a clean working tree, and the commit(s) named in its evidence.
* **Changelog in the ship commit.** Each repo keeps a [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) `CHANGELOG.md`; the entry rides in the same commit as the change.
* **Prompts are append-only.** New prompt versions are added; old ones are never edited in place.
* **Pins are audited.** After an upstream release, the pin, the tag and the import-time version must agree.

## Which board, which tracker

| Work                                                                  | Where it is tracked                                                                                                                                                                                               | Card prefix  |
| --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| Cross-package work in the monorepo (wiring, sync, hub docs, releases) | [`Digital-Mailroom/governance/TASKS.md`](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/governance/TASKS.md), served live at the [Dispatch Board](https://digital-mailroom-theta.vercel.app) | `DMR-0NN`    |
| llm-entity-extraction and llm-mailroom package work                   | `llm-entity-extraction/governance/MESSAGE_BOARD.md` (one shared board for both repos)                                                                                                                             | `KANBAN-0NN` |
| local-mailroom-sandbox                                                | `local-mailroom-sandbox/governance/TASKS.md`                                                                                                                                                                      | `SAND-NN`    |
| mailroom-ml                                                           | `mailroom-ml/governance/TASKS.md` (light tracking)                                                                                                                                                                | —            |
| Anything spanning several repos, or needing an org decision           | [mailroom-issues](https://github.com/LLM-Mailroom-Services/mailroom-issues) issues, epics and RFCs                                                                                                                | issue number |
| A bug in one package                                                  | that package's own GitHub issues                                                                                                                                                                                  | issue number |

Older card prefixes such as `HUB-0NN` come from earlier monorepo boards and still appear in commit messages and docs.

### Filing an issue in the right place

mailroom-issues has a decision tree in [`docs/ROUTING.md`](https://github.com/LLM-Mailroom-Services/mailroom-issues/blob/main/docs/ROUTING.md). In short:

| Situation                                               | File it in                  |
| ------------------------------------------------------- | --------------------------- |
| Workstream across several repos                         | mailroom-issues, as an epic |
| Platform or architecture decision                       | mailroom-issues, as an RFC  |
| Bug in monorepo tooling (`board_state.py`, sync driver) | Digital-Mailroom            |
| Bug in one package                                      | that package's repo         |

Issues there carry four label families: `type/*`, `domain/*`, `priority/*` and `status/*`.

## Monorepo board laws

From the hub's [Board Governance](https://github.com/LLM-Mailroom-Services/Digital-Mailroom/blob/main/docs/wiki/Board-Governance.md) page:

| Lane              | Meaning                                         |
| ----------------- | ----------------------------------------------- |
| `unassigned`      | Queued and free to claim                        |
| `assigned`        | Claimed, nothing started                        |
| `in_progress`     | Any work exists; label the card before the code |
| `needs_attention` | Blocked, in review, or waiting on a decision    |
| `done`            | Finished, verified, evidenced, archived         |

* Commit messages reference the card: `DMR-0NN: <summary>`.
* Stage explicit paths only (`git add <paths>`), never `git add .`, because the monorepo is a shared checkout.
* Critical or cross-package cards get a linked GitHub issue; lane moves are mirrored as issue comments and the issue closes in the same commit that archives the card.
* `python scripts/board_state.py check` validates the board; edits made on the served board are pulled back with `board_state.py pull-issues --apply`.

## Releases and syncing

1. Develop in `Digital-Mailroom/packages/<name>`.
2. When cutting a release of a package, push it to its `Exios66/<name>` repository with `python scripts/sync_packages.py push --package <name>`.
3. Tag the release in the upstream repo. Deployments (Docker, Railway, Hugging Face Spaces) install from that tag.
4. Bump the pins in dependent packages. llm-mailroom's dojo pin is bumped automatically by a workflow when llm-dojo-scoring publishes; inside the monorepo, run `src/scripts/bump_dojo_scoring.py` by hand at release time.

Changes made directly in a standalone repo come back into the monorepo with `sync_packages.py pull`. Docs that describe the constellation are maintained in the standalone repo and arrive in the monorepo the same way.

## Documentation rules

* Each repository's own `docs/` folder is the source of truth for that repository. This site links to those docs rather than copying them.
* In llm-mailroom, evaluation write-ups go under `docs/reports/<kind>/` via `src/scripts/new_report.py`, and `docs/wiki/` holds GitHub-wiki-only pages that are never copies of `docs/`.
* How to keep this site current: [Maintaining this site](../about-this-site/maintaining.md).
