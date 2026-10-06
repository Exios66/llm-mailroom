# Maintaining this site

This site is published with [GitBook Git Sync](https://gitbook.com/docs/getting-started/git-sync) from the `docs/` folder of [Exios66/llm-mailroom](https://github.com/Exios66/llm-mailroom).

## How it is built

| File | Role |
| :--- | :--- |
| `.gitbook.yaml` (repository root) | Tells GitBook the site root is `docs/` |
| `docs/README.md` | The landing page |
| `docs/SUMMARY.md` | The table of contents. Only pages listed here are published. |
| `docs/constellation/` | The constellation pages: overview, getting started, architecture, data, governance, glossary, repo index |
| `docs/constellation/repos/` | One guide per repository |
| `docs/*.md` | The llm-mailroom pipeline reference, published as-is |

`docs/wiki/` (GitHub wiki source) and `docs/assets/` are not listed in `SUMMARY.md`, so they stay off the site.

## Connecting a GitBook space (one-time)

1. In GitBook, create a space (for example "Mailroom Docs").
2. Open the space's **Configure** menu, choose **GitHub Sync**, and install the GitBook GitHub app on `Exios66/llm-mailroom`.
3. Pick the `main` branch. GitBook reads `.gitbook.yaml` and finds `docs/`.
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
| Fumi's artwork changes | Copy the new animated `fumi.gif` export over `assets/fumi/fumi.gif` (shown on the home page). GitBook strips scripts and may not animate SVG, so the GIF is the one to use |
