# Landing page

The GitHub Pages site for llm-mailroom: `index.html` is the whole page (no build step).
`.github/workflows/pages.yml` publishes it on every push to `main` that touches `landing/` or the mascot,
copying `docs/assets/mascot/` into `assets/mascot/` on the way.

Preview locally:

```bash
mkdir -p /tmp/site/assets && cp -r landing/. /tmp/site/ && cp -r docs/assets/mascot /tmp/site/assets/
python -m http.server -d /tmp/site 8080
```

The terminal on the page is a simulation with sample filenames; it does not talk to a running pipeline.
