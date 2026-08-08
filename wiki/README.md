# `wiki/` — The GitHub wiki pages

## What this folder is (plain English)

This is a **mirror of `docs/`**, formatted for publishing to the project's GitHub wiki. The docs live in two places on purpose: `docs/` (in this repository) and `wiki/` (published at the repo's GitHub wiki URL). `wiki/sync-wiki.sh` copies these pages to the wiki.

**If you're reading this to understand the code:** skip this folder and read `docs/` instead — it's the same content, easier to reach.

**If you're editing docs:** edit both `docs/` and `wiki/` so they stay in sync.

## File mapping

| `docs/` | `wiki/` |
|---|---|
| `architecture.md` | `Architecture.md` |
| `agents.md` | `Agents.md` |
| `configuration.md` | `Configuration.md` |
| `deployment.md` | `Deployment.md` |
| `api.md` | `API-Reference.md` |
| `testing.md` | `Development.md` |
| `local-models.md` | `Local-Model-Cutover.md` |
| — | `Getting-Started.md`, `Home.md`, `FAQ.md`, `_Sidebar.md`, `_Footer.md` |

## Pushing to the GitHub wiki

```bash
./wiki/sync-wiki.sh              # uses the origin remote to derive the wiki URL
./wiki/sync-wiki.sh git@github.com:user/repo.wiki.git   # or pass it explicitly
```

The script clones the wiki repo into a temp dir, copies every `*.md` here, commits, and pushes to `master`.

## Technical reference

- Wikis are separate git repos (`<repo>.wiki.git`). The script requires a wiki to exist on GitHub and push access.
- `Getting-Started.md` duplicates the quickstart from the root `README.md`.
- Keep `docs/` and `wiki/` in sync when changing user-facing docs; `AGENTS.md` (repo root) notes this duplication.
