# Maintaining this site

This site is **[Mailroom Inc. Docs](https://mailroom-inc.gitbook.io/mailroom-inc.-docs/)**, published with [GitBook Git Sync](https://gitbook.com/docs/getting-started/git-sync) from the `docs/` folder of [Exios66/llm-mailroom](https://github.com/Exios66/llm-mailroom). `docs/README.md` is the site landing page (the space is mounted at path `/`). A merge to `main` replaces that home.

## How it is built

| File | Role |
| :--- | :--- |
| `docs/gitbook-docs.yaml` | Site Git Sync for https://mailroom-inc.gitbook.io/mailroom-inc.-docs/. GitBook's Git Sync **Project directory is `docs/`** (GITBOOK-SITE commits write this file here). Maps the `mailroom-docs` space to `directory: ./` at `path: /`. Do not change `key`. Site title stays **Mailroom Inc. Docs**. |
| `gitbook-docs.yaml` (repository root) | Fallback only: same space mapping if the Project directory is ever moved to the repo root (`directory: ./docs`, `path: /`). Live GitBook does not read this file today. |
| `.gitbook.yaml` (repository root) | Classic space Git Sync: content root is `docs/` |
| `docs/.gitbook.yaml` | Space config inside the mapped directory: `README.md` is the first page |
| `docs/README.md` | The GitBook landing page. Port of `landing/`: Fumi in the header corner with **The LLM-Mailroom — Fumi (文, "letter")**, owl banner below the badges, tags, install, pipeline walk-through, and docs shelf. GitBook's own type; it does not load Pixelify Sans. GitBook strips scripts, so the idle TUI stays on the static page. |
| `docs/SUMMARY.md` | The table of contents. Only pages listed here are published. |
| `docs/constellation/` | The constellation pages: overview, getting started, architecture, data, governance, glossary, repo index |
| `docs/constellation/repos/` | One guide per repository |
| `docs/*.md` | The llm-mailroom pipeline reference, published as-is |

`docs/wiki/` (GitHub wiki source) and `docs/assets/` are not listed in `SUMMARY.md`, so they stay off the site.

## Connecting a GitBook space (one-time)

1. In GitBook, create a space (for example "Mailroom Docs").
2. Open the space's **Configure** menu, choose **GitHub Sync**, and install the GitBook GitHub app on `Exios66/llm-mailroom`.
3. Pick the `main` branch. GitBook reads `docs/gitbook-docs.yaml` (site; Project directory = `docs/`) and `docs/.gitbook.yaml` (space) and publishes `docs/README.md` as the site home.
4. Choose **GitHub to GitBook** for the first sync so the repository content is imported rather than overwritten.

After that, a merge to `main` updates the site. Edits made in the GitBook editor come back as commits.

Mermaid diagrams render on GitHub; in GitBook they need the Mermaid integration enabled on the space, otherwise they show as code blocks.

## `path` vs `directory` (this is the usual mix-up)

`./docs` in Git Sync is **not** a URL. Two different knobs share the word "docs":

| Knob | File | Meaning | Correct value here |
| :--- | :--- | :--- | :--- |
| Git Sync **Project directory** (GitBook UI) | — | Where GitBook looks for `gitbook-docs.yaml` | **`docs/`** (live). GITBOOK-SITE commits prove this: GitBook writes `docs/gitbook-docs.yaml`. Do not move it to the repository root without also relocating that file. |
| `content.directory` | `docs/gitbook-docs.yaml` | Git folder the space reads, relative to the Project directory | `./` (this is already `docs/`) |
| `path` | `docs/gitbook-docs.yaml` | URL after the site slug | `/` (site home). `path: docs` would publish at `…/mailroom-inc.-docs/docs/` |
| `root` | `.gitbook.yaml` | Extra subfolder inside the mapped directory | `./docs/` at the repo root file, or `./` in `docs/.gitbook.yaml` |

`path` is a URL slug, not a folder name. `directory: ./docs` inside a Project directory of `docs/` would look for `docs/docs/` (that folder does not exist). Leave the GitBook UI Project directory at `docs/`. GitBook never publishes `landing/index.html`; that page is static HTML.

GitBook also replaces the markdown H1 with the `SUMMARY.md` link title. Keep that link as `* [The LLM-Mailroom](README.md)` so it matches the `# The LLM-Mailroom` heading in `docs/README.md` (the previous `* [Welcome](README.md)` title is why the live home used to read **Welcome**).

GitBook's published favicon is the site icon in **Customize** ([icons, colors, and themes](https://gitbook.com/docs/manage-your-site/customization/icons-colors-and-themes)). Git Sync cannot set it: `gitbook-docs.yaml` has no favicon field. Upload `docs/assets/mascot/hoot-icon.png` (Hoot, the pixel owl). The static `landing/` page uses that same file as `<link rel="icon">`.

## Rules for editing

- **Link, don't copy.** Each repository's own README and `docs/` stay canonical. A guide here summarizes what a newcomer needs to orient, then links to the source. This follows the llm-mailroom rule that `docs/` content is never duplicated.
- **Every new page goes in `SUMMARY.md`.** A page that is not listed is not published. Operator recipes that belong on this site (Docker compose matrix, Modal + vLLM) live under `docs/` and are listed here — `deploy/README.md` is a file index, not a GitBook page. Sandbox run reports and visuals are nested under the [local-mailroom-sandbox](repos/local-mailroom-sandbox.md) guide.
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
| Compose matrix, Mode G, or Modal vLLM knobs change | [Docker](../docker-deployment.md) and [Modal + vLLM](../modal-vllm.md) first; keep `deploy/README.md` as an index that links those pages |
| Sandbox run reports or figures change | [Run reports](repos/local-mailroom-sandbox-reports.md) and [Visuals](repos/local-mailroom-sandbox-visuals.md); keep image URLs on `Exios66/local-mailroom-sandbox` `main` |
| Fumi's artwork changes | Re-run `python src/scripts/build_mascot.py` (copies GIF/PNG/SVG/Hoot favicon into `docs/assets/mascot/` and `landing/assets/mascot/`, and `docs/assets/fumi/fumi.gif` for this home page). GitBook strips scripts and may not animate SVG, so the GIF is the one to use on this page. Keep Fumi in the header corner of `docs/README.md`; do not make her the page header. The owl banner, title, and badges stay the masthead. Re-upload `hoot-icon.png` in GitBook Customize if Hoot's sprite changes. |
