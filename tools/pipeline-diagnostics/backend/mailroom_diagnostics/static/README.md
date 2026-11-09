# Generated static assets

The React build lands here in D1.1. This file is intentionally committed so
the nested wheel always contains the `static/` package directory; there is no
`index.html` yet, so the app serves its build-instructions page and the JSON
API keeps working.

Do not hand-edit generated assets in this directory. Build them from
`frontend/` with the committed lockfile and package them through the nested
`pyproject.toml` package-data rules.
