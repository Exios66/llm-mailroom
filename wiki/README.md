# `wiki/` — The GitHub wiki pages

## What this folder is (plain English)

These are the pages published to the project's **GitHub wiki** (a separate git repo at `<repo>.wiki.git`). `wiki/sync-wiki.sh` copies these pages to the wiki.

**This folder is NOT a mirror of `docs/`.** Repository documentation lives only in `docs/`; `wiki/` holds wiki-native pages (Home, Getting-Started, FAQ, _Sidebar, _Footer) plus this README. Do not copy `docs/` pages in here — that just duplicates documentation already in the repository.

## Pushing to the GitHub wiki

```bash
./wiki/sync-wiki.sh              # uses the origin remote to derive the wiki URL
./wiki/sync-wiki.sh git@github.com:user/repo.wiki.git   # or pass it explicitly
```

The script clones the wiki repo into a temp dir, copies every `*.md` here, commits, and pushes to `master`.

## Technical reference

- Wikis are separate git repos (`<repo>.wiki.git`). The script requires a wiki to exist on GitHub and push access.
- `Getting-Started.md` summarizes the quickstart from the root `README.md`.
- `Home.md` and `_Sidebar.md` are the wiki landing page and navigation; `_Footer.md` is the wiki footer.
- Canonical repo documentation lives in `docs/` — edit there, never here.
