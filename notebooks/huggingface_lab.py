"""Lucius-Morningstar Hugging Face corpus lab — the reusable module behind
``notebooks/11_huggingface_corpora.ipynb``.

Default path is **network-free**: every helper reads the committed Dataset
Viewer snapshot under ``notebooks/fixtures/huggingface/`` (catalog + first
rows, dated in ``catalog.json``, plus ``class_subclass_examples.json`` —
one Hub row per v5 class × subclass stratum from ``docclass-pilot``). Live Hub / Dataset Viewer calls are
opt-in (``live=True`` / ``MAILROOM_HF_LIVE=1``) and imported lazily so the
notebook guard's module-scope network scan stays clean.

Dataset Viewer API shape follows the huggingface-datasets skill
(``https://datasets-server.huggingface.co``): ``/is-valid``, ``/splits``,
``/first-rows``, ``/rows``, ``/search``, ``/filter``, ``/size``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ORG = "Lucius-Morningstar"
ORG_URL = f"https://huggingface.co/{ORG}"
VIEWER_BASE = "https://datasets-server.huggingface.co"
HUB_API = "https://huggingface.co/api"

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "huggingface"

# Hub repo id → local snapshot filename (the slug after the org).
_SNAPSHOT_NAME = {
    "Lucius-Morningstar/docclass-merged": "docclass-merged.json",
    "Lucius-Morningstar/docclass-pilot": "docclass-pilot.json",
    "Lucius-Morningstar/enron-correspondence-dedup": "enron-correspondence-dedup.json",
    "Lucius-Morningstar/cms-desynpuf-insurance-claims": "cms-desynpuf-insurance-claims.json",
    "Lucius-Morningstar/mailroom-cuad-contracts": "mailroom-cuad-contracts.json",
    "Lucius-Morningstar/mailroom-cuad-contracts-full": "mailroom-cuad-contracts-full.json",
    "Lucius-Morningstar/legalbench-full": "legalbench-full.json",
}
EXAMPLES_PACK = FIXTURES / "class_subclass_examples.json"


def class_subclass_examples() -> dict[str, Any]:
    """Committed class × subclass examples from docclass-pilot (v5 parent)."""
    return json.loads(EXAMPLES_PACK.read_text(encoding="utf-8"))


def live_requested() -> bool:
    """True when the operator opted into Hub traffic.

    Public datasets do not need a token; the flag exists so CI / notebook
    guards never hit the network by accident. ``HF_TOKEN`` is used when set
    (higher rate limits / gated repos) but is not required.
    """
    flag = os.environ.get("MAILROOM_HF_LIVE", "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def catalog(*, live: bool = False) -> dict[str, Any]:
    """Org catalog: published datasets mapped onto mailroom doc classes.

    Offline: committed snapshot. Live: Hub ``/api/datasets?author=`` merged
    with Dataset Viewer ``/is-valid`` + ``/size`` (keeps the mapping table
    from the snapshot so class wiring cannot drift).
    """
    snap = json.loads((FIXTURES / "catalog.json").read_text())
    if not live:
        snap["source"] = "offline-snapshot"
        return snap
    remote = _hub_get(f"datasets?author={ORG}&limit=100")
    by_id = {d["id"]: d for d in snap["datasets"]}
    refreshed = []
    if isinstance(remote, list):
        for item in remote:
            rid = item.get("id")
            base = dict(by_id.get(rid) or {"id": rid, "mailroom_classes": [], "role": "(unmapped)"})
            base["downloads"] = item.get("downloads")
            base["likes"] = item.get("likes")
            base["last_modified"] = item.get("lastModified")
            refreshed.append(base)
        # Keep snapshot-only entries that the live list missed.
        seen = {d["id"] for d in refreshed}
        for d in snap["datasets"]:
            if d["id"] not in seen:
                refreshed.append(d)
        snap["datasets"] = refreshed
    snap["source"] = "live-hub"
    return snap


def list_datasets(*, live: bool = False) -> list[dict[str, Any]]:
    return catalog(live=live)["datasets"]


def preview(repo_id: str, *, live: bool = False, offset: int = 0, length: int = 8) -> dict[str, Any]:
    """First-row window for one dataset.

    Offline: snapshot (offset is applied locally). Live: Dataset Viewer
    ``/rows`` (length capped at 100 by the API).
    """
    if live:
        meta = next((d for d in list_datasets(live=False) if d["id"] == repo_id), {})
        splits = meta.get("splits") or [{"config": "default", "split": "train"}]
        cfg, sp = splits[0]["config"], splits[0]["split"]
        payload = _viewer_get(
            "/rows",
            dataset=repo_id,
            config=cfg,
            split=sp,
            offset=offset,
            length=min(int(length), 100),
        )
        rows = []
        for item in payload.get("rows") or []:
            row = item.get("row") if isinstance(item, dict) else item
            if isinstance(row, dict):
                rows.append(_slim_row(row))
        return {
            "dataset": repo_id,
            "config": cfg,
            "split": sp,
            "source": "live-viewer",
            "num_rows_total": payload.get("num_rows_total"),
            "rows": rows,
            "features": [f.get("name") for f in (payload.get("features") or []) if isinstance(f, dict)],
        }
    name = _SNAPSHOT_NAME.get(repo_id)
    if not name:
        raise KeyError(f"no offline snapshot for {repo_id!r}")
    data = json.loads((FIXTURES / name).read_text())
    rows = list(data.get("rows") or [])
    if offset:
        rows = rows[offset:]
    rows = rows[:length]
    data = dict(data)
    data["rows"] = rows
    data["source"] = "offline-snapshot"
    return data


def search(repo_id: str, query: str, *, live: bool = False, length: int = 8) -> dict[str, Any]:
    """Text search. Offline: substring match over snapshot rows. Live: Viewer ``/search``."""
    q = (query or "").strip().lower()
    if live:
        meta = next((d for d in list_datasets(live=False) if d["id"] == repo_id), {})
        splits = meta.get("splits") or [{"config": "default", "split": "train"}]
        cfg, sp = splits[0]["config"], splits[0]["split"]
        payload = _viewer_get(
            "/search",
            dataset=repo_id,
            config=cfg,
            split=sp,
            query=query,
            offset=0,
            length=min(int(length), 100),
        )
        rows = []
        for item in payload.get("rows") or []:
            row = item.get("row") if isinstance(item, dict) else item
            if isinstance(row, dict):
                rows.append(_slim_row(row))
        return {
            "dataset": repo_id,
            "query": query,
            "source": "live-viewer",
            "num_rows_total": payload.get("num_rows_total"),
            "rows": rows,
        }
    data = preview(repo_id, live=False, length=50)
    hits = []
    for row in data.get("rows") or []:
        blob = " ".join(str(v) for v in row.values() if v is not None).lower()
        if q and q in blob:
            hits.append(row)
        if len(hits) >= length:
            break
    return {
        "dataset": repo_id,
        "query": query,
        "source": "offline-snapshot-substring",
        "note": "Offline search only sees the committed first-row window, not the full corpus.",
        "rows": hits,
    }


def filter_rows(repo_id: str, *, where: str, live: bool = False, length: int = 8) -> dict[str, Any]:
    """Viewer ``/filter`` (live) or a tiny offline equality matcher.

    Offline ``where`` is ``column=value`` (exact string match after strip).
    """
    if live:
        meta = next((d for d in list_datasets(live=False) if d["id"] == repo_id), {})
        splits = meta.get("splits") or [{"config": "default", "split": "train"}]
        cfg, sp = splits[0]["config"], splits[0]["split"]
        payload = _viewer_get(
            "/filter",
            dataset=repo_id,
            config=cfg,
            split=sp,
            where=where,
            offset=0,
            length=min(int(length), 100),
        )
        rows = []
        for item in payload.get("rows") or []:
            row = item.get("row") if isinstance(item, dict) else item
            if isinstance(row, dict):
                rows.append(_slim_row(row))
        return {"dataset": repo_id, "where": where, "source": "live-viewer", "rows": rows}
    if "=" not in (where or ""):
        raise ValueError("offline filter expects column=value")
    col, val = where.split("=", 1)
    col, val = col.strip(), val.strip().strip("\"'")
    data = preview(repo_id, live=False, length=50)
    hits = [r for r in data.get("rows") or [] if str(r.get(col, "")).strip() == val][:length]
    return {
        "dataset": repo_id,
        "where": where,
        "source": "offline-snapshot-equality",
        "note": "Offline filter only sees the committed first-row window.",
        "rows": hits,
    }


def row_to_doc_text(row: dict[str, Any]) -> str:
    """Best-effort document text from a Hub row, for feeding the pipeline."""
    for key in ("doc_text", "text", "input", "body", "content"):
        val = row.get(key)
        if isinstance(val, str) and val.strip() and val.strip() != "{{text}}":
            # mailroom-cuad-contracts-full stores JSON in `input`
            if key == "input" and val.lstrip().startswith("{"):
                try:
                    parsed = json.loads(val)
                    inner = parsed.get("doc_text") if isinstance(parsed, dict) else None
                    if isinstance(inner, str) and inner.strip():
                        return inner
                except json.JSONDecodeError:
                    pass
            return val
    return ""


def show_catalog(rows: list[dict[str, Any]] | None = None) -> None:
    """Plain-text catalog table (no ipywidgets)."""
    rows = rows if rows is not None else list_datasets()
    print(f"{'dataset':48s} {'rows':>8s}  classes                  role")
    print("-" * 120)
    for d in rows:
        classes = ",".join(d.get("mailroom_classes") or ["—"])
        n = d.get("num_rows")
        n_s = f"{n:,}" if isinstance(n, int) else "?"
        role = (d.get("role") or "")[:48]
        slug = d["id"].split("/", 1)[-1]
        print(f"{slug:48s} {n_s:>8s}  {classes:24s} {role}")


def show_rows(payload: dict[str, Any], *, text_chars: int = 160) -> None:
    """Plain-text row dump."""
    print(f"{payload.get('dataset')}  source={payload.get('source')}  n={len(payload.get('rows') or [])}")
    for i, row in enumerate(payload.get("rows") or [], 1):
        print(f"  [{i}]")
        for k, v in row.items():
            s = str(v).replace("\n", " ")
            if len(s) > text_chars:
                s = s[:text_chars] + "…"
            print(f"      {k}: {s}")


# ---------------------------------------------------------------------------
# Live HTTP — imported inside the function so module-scope stays network-free
# (test_notebook_suite AST scan of this file's top-level body).
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or ""
    headers = {"User-Agent": "llm-mailroom-notebooks/huggingface_lab"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _http_get_json(url: str) -> Any:
    import urllib.request

    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _hub_get(path: str) -> Any:
    return _http_get_json(f"{HUB_API}/{path.lstrip('/')}")


def _viewer_get(path: str, **params: Any) -> Any:
    from urllib.parse import urlencode

    qs = urlencode({k: v for k, v in params.items() if v is not None})
    return _http_get_json(f"{VIEWER_BASE}{path}?{qs}")


def _slim_row(row: dict[str, Any], n: int = 500) -> dict[str, Any]:
    slim: dict[str, Any] = {}
    for k, v in row.items():
        if k == "image" and isinstance(v, dict):
            slim[k] = {"src": str(v.get("src", ""))[:180]}
            continue
        if v is None:
            slim[k] = None
            continue
        if isinstance(v, (dict, list)):
            s = json.dumps(v, default=str)
        else:
            s = str(v)
        slim[k] = s if len(s) <= n else s[:n] + "…"
    return slim
