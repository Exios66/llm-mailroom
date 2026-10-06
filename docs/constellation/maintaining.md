# Maintaining this site

This site is published with [GitBook Git Sync](https://gitbook.com/docs/getting-started/git-sync) from the `docs/` folder of [Exios66/llm-mailroom](https://github.com/Exios66/llm-mailroom).

## How it is built

| File | Role |
| :--- | :--- |
| `gitbook-docs.yaml` (repository root) | Site Git Sync: maps the `mailroom-docs` space to `./docs` at path `/`, so this space **is** the published GitBook landing |
| `.gitbook.yaml` (repository root) | Classic space Git Sync: content root is `docs/` |
| `docs/.gitbook.yaml` | Space config inside the mapped directory: `README.md` is the first page |
| `docs/README.md` | The GitBook landing page. Port of `landing/`: Fumi in the header corner, owl banner, **The LLM-Mailroom** title, README badges, install, pipeline walk-through. GitBook strips scripts, so the idle TUI stays on the static page. |
| `docs/SUMMARY.md` | The table of contents. Only pages listed here are published. |
| `docs/constellation/` | The constellation pages: overview, getting started, architecture, data, governance, glossary, repo index |
| `docs/constellation/repos/` | One guide per repository |
| `docs/*.md` | The llm-mailroom pipeline reference, published as-is |

`docs/wiki/` (GitHub wiki source) and `docs/assets/` are not listed in `SUMMARY.md`, so they stay off the site.

## Connecting a GitBook space (one-time)

1. In GitBook, create a space (for example "Mailroom Docs").
2. Open the space's **Configure** menu, choose **GitHub Sync**, and install the GitBook GitHub app on `Exios66/llm-mailroom`.
3. Pick the `main` branch. GitBook reads `gitbook-docs.yaml` (site) and `.gitbook.yaml` (space) and publishes `docs/README.md` as the site home.
4. Choose **GitHub to GitBook** for the first sync so the repository content is imported rather than overwritten.

After that, a merge to `main` updates the site. Edits made in the GitBook editor come back as commits.

Mermaid diagrams render on GitHub; in GitBook they need the Mermaid integration enabled on the space, otherwise they show as code blocks.

## Rules for editing

- **Link, don't copy.** Each repository's own README and `docs/` stay canonical. A guide here summarizes what a newcomer needs to orient, then links to the source. This follows the llm-mailroom rule that `docs/` content is never duplicated.
- **Every new page goes in `SUMMARY.md`.** A page that is not listed is not published.
- **Use relative links between pages on this site** (`repos/llm-mailroom.md`, `../architecture.md`) and full GitHub URLs for anything in another repository.
- **Date facts that drift.** Versions, pins and counts carry an "as of" date; when a release moves them, update [Overview](overview.md) and the affected guide.
- **Keep the GitHub wiki separate.** `docs/wiki/` remains wiki-only and is not a mirror of these pages.

## When a repository changes

| Change | Update |
| :--- | :--- |
| New repository joins the constellation | Add a guide under `repos/`, list it in `SUMMARY.md`, [Overview](overview.md), [Repository index](repo-index.md) and `repos/README.md` |
| Repository archived or superseded | Move it to the copies or earlier-monorepos table in the [Repository index](repo-index.md) |
| A release changes versions or pins | [Overview](overview.md) version table, the repo's guide, the [dependency table](architecture.md#dependency-summary) |
| Dataset revision changes | [Data and corpora](data-and-corpora.md) |
| Pipeline nodes or classes change | The pipeline reference pages first; then [Architecture](architecture.md) and [Glossary](glossary.md) if terms changed |
| Fumi's artwork changes | Re-run `python src/scripts/build_mascot.py` (copies GIF/PNG/SVG into `docs/assets/mascot/`, `landing/assets/mascot/`, and `docs/assets/fumi/fumi.gif` for this home page). GitBook strips scripts and may not animate SVG, so the GIF is the one to use on this page. Keep Fumi in the header corner of `docs/README.md`; do not make her the page header. The owl banner, title, and badges stay the masthead. |
