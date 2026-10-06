# Landing page

The llm-mailroom landing page. `index.html` is the whole page, with no build step. Its mascot files live in `assets/mascot/`, and `src/scripts/build_mascot.py` keeps them in sync with `docs/assets/mascot/`. That means this folder deploys as-is to any static host.

## Deploy on Posit Connect Cloud (no GitHub Actions needed)

Connect Cloud publishes static HTML straight from a public GitHub repo and redeploys on every push to the chosen branch.

1. Sign in at [connect.posit.cloud](https://connect.posit.cloud) and click **Publish**.
2. Pick **Static Document** as the framework.
3. Enter the repository `Exios66/llm-mailroom` and the branch `main`.
4. Set the primary file to `landing/index.html`.
5. Leave auto-republish on and click **Publish**.

## Other hosts

- **GitHub Pages:** set the source to the `landing/` contents on a branch. This needs GitHub Actions minutes available on the account.
- **Netlify, Cloudflare Pages, or Vercel:** publish directory `landing`, with no build command.

## Preview locally

```bash
python -m http.server -d landing 8080
```

The terminal on the page is a simulation with sample filenames. It doesn't talk to a running pipeline.
