# Landing page

The llm-mailroom landing page. `index.html` is the whole page, with no build step. Its mascot files live in `assets/mascot/`, and `src/scripts/build_mascot.py` keeps them in sync with `docs/assets/mascot/`. That means this folder deploys as-is to any static host.

The **published** landing for the constellation docs site is GitBook. `docs/README.md` is that home page: same masthead (owl banner, **The LLM-Mailroom**, README badges), Fumi in the header corner, install commands, pipeline walk-through. GitBook strips scripts, so the idle mail-floor terminal stays here.

## Deploy on Posit Connect Cloud (no GitHub Actions needed)

Connect Cloud publishes static HTML straight from a public GitHub repo and redeploys on every push to the chosen branch.

1. Sign in at [connect.posit.cloud](https://connect.posit.cloud) and click **Publish**.
2. Pick **Static Document** as the framework.
3. Enter the repository `Exios66/llm-mailroom` and the branch `main`.
4. Set the primary file to `landing/index.html`.
5. Leave auto-republish on and click **Publish**.

## Other hosts

- **GitBook (the published home):** [Mailroom Inc. Docs](https://mailroom-inc.gitbook.io/mailroom-inc.-docs/). Merge to `main`. Site Git Sync reads `gitbook-docs.yaml` (`directory: ./docs`, `path: /`) and space Git Sync reads `.gitbook.yaml`; both publish `docs/README.md` as that URL. See `docs/constellation/maintaining.md`.
- **GitHub Pages:** GitHub Pages cannot select `landing/` as a branch source folder. It accepts only the branch root or `/docs`. Publish by copying the contents of `landing/` to the root of a dedicated `gh-pages` branch, or use a workflow that deploys this folder when Actions is available.
- **Netlify, Cloudflare Pages, or Vercel:** publish directory `landing`, with no build command.

## Preview locally

```bash
python -m http.server -d landing 8080
```

The terminal on the page is a simulation with sample filenames. It doesn't talk to a running pipeline.
