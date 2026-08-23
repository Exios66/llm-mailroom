# `docs/wiki/` — The GitHub wiki pages

## What this folder is (plain English)

These are the pages published to the project's **GitHub wiki** (a separate git repo at `<repo>.wiki.git`). `sync-wiki.sh` copies these pages to the wiki — AND refreshes the mirrored pages from the canonical `docs/` files first, so the wiki never drifts from the repo docs.

Two kinds of pages live here:

1. **Wiki-native pages** — edited here directly: `Home.md`, `Getting-Started.md`, `FAQ.md`, `_Sidebar.md`, `_Footer.md`, plus this README.
2. **Mirrored pages** — generated from canonical sources at sync time (do NOT edit the wiki copies by hand; edit `docs/<source>.md` instead):

| Wiki page | Canonical source |
|---|---|
| `Architecture.md` | `docs/architecture.md` |
| `Agents.md` | `docs/agents.md` |
| `Configuration.md` | `docs/configuration.md` |
| `API-Reference.md` | `docs/api.md` |
| `Deployment.md` | `docs/deployment.md` |
| `Local-Model-Cutover.md` | `docs/local-models.md` |
| `Development.md` | `docs/testing.md` |

(The in-repo documents are the canonical sources; the wiki is the map, not the territory.)

## Pushing to the GitHub wiki

```bash
./docs/wiki/sync-wiki.sh              # uses the origin remote to derive the wiki URL
./docs/wiki/sync-wiki.sh git@github.com:user/repo.wiki.git   # or pass it explicitly
```

The script clones the wiki repo into a temp dir, refreshes the mirrored pages from `docs/`, copies every `*.md` here, commits, and pushes to `master`.

## Technical reference

- Wikis are separate git repos (`<repo>.wiki.git`). The script requires a wiki to exist on GitHub and push access.
- `Getting-Started.md` summarizes the quickstart from the root `README.md`.
- `Home.md` and `_Sidebar.md` are the wiki landing page and navigation; `_Footer.md` is the wiki footer.
- Canonical repo documentation lives in `docs/` — including [`docs/sister-repos.md`](../sister-repos.md), the umbrella map of governed sibling repos referenced throughout the wiki.
