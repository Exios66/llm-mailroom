"""Canonical mailroom-corpus HuggingFace dataset loader (HUB-053).

The ONE loading path for the mailroom-corpus family (``Lucius-Morningstar/
mailroom-corpus``, internal slug ``docclass-merged``) — scripts, notebooks,
and eval runners all import from here; no ad-hoc fetch code anywhere else.

Verified against the live services 2026-09-04 (HF-SME subagent evidence on
HUB-053; re-verify before trusting on schema changes):

- Schema v8+ is split across TWO parquet configs on split ``train``:
  ``ground_truth`` (labels + provenance; ~60 columns incl. ``expected``,
  ``expected_subclass``, ``expected_stage``, ``document_id``,
  ``content_sha256``, insurance fields) and ``default`` (blind:
  ``doc_text``/``filename``/``metadata``/``prompt``). The join key is
  ``filename`` (zero duplicates per config; positional alignment is an
  export artifact, never a contract — always join, never zip).
- ``content_sha256`` is sha256 of the canonical ``doc_text`` UTF-8 bytes —
  the load-time integrity proof (verified 1792/1792 exact match).
- The Dataset Viewer ``/filter`` endpoint has been intermittently broken
  server-side (422/500/502); the robust fetch is ``/parquet`` (2 requests
  for a whole split) with ``/rows`` pagination (``length<=100``) as the
  fallback ladder.
- The Hub single-repo API is ``https://huggingface.co/api/datasets/{id}``
  with a LITERAL slash (URL-encoding the slash is a 400); the
  ``?author=&search=&expand[]=sha`` listing is the fallback.
- Reads are anonymous-friendly (public, ungated); ``HF_TOKEN`` is optional
  headroom for quota. Never log or commit tokens.

``datasets.load_dataset`` is NOT a dependency of this workspace — the
loader is a stdlib + httpx + pandas ladder with an on-disk parquet cache
(``<base>/hf_cache/``); when the ``datasets`` library IS installed it is
preferred for pinned-revision loads (``revision=`` is only expressible
there and on the ``?revision=`` viewer params).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .env import load_env

HUB_BASE = "https://huggingface.co"
VIEWER_BASE = "https://datasets-server.huggingface.co"
ORG = "Lucius-Morningstar"
FULL_CORPUS_ID = f"{ORG}/mailroom-corpus"
GT_CONFIG = "ground_truth"
BLIND_CONFIG = "default"
JOIN_KEY = "filename"
DEFAULT_TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# Provenance


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dataset_sha(repo_id: str = FULL_CORPUS_ID) -> str | None:
    """Current git sha (tip) of a Hub dataset — literal-slash path first,
    author+search listing as the fallback. None when unreachable."""
    try:
        payload = _http_get_json(f"{HUB_BASE}/api/datasets/{repo_id}")
        sha = payload.get("sha")
        if sha:
            return str(sha)
    except Exception:
        pass
    try:
        ns, name = repo_id.split("/", 1)
        rows = _http_get_json(
            f"{HUB_BASE}/api/datasets?author={ns}&search={name}&expand[]=sha"
        )
        for row in rows or []:
            if row.get("id") == repo_id and row.get("sha"):
                return str(row["sha"])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# HTTP (network imports stay INSIDE functions — the notebook guard suite
# bans module-scope network imports; test_notebook_suite.py Duty 4)


def _token_headers() -> dict[str, str]:
    headers = {"User-Agent": "llm-mailroom-corpus-loader/1.0"}
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _http_get_bytes(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, headers=_token_headers())
        resp.raise_for_status()
        return resp.content


def _http_get_json(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> Any:
    return json.loads(_http_get_bytes(url, timeout=timeout))


# ---------------------------------------------------------------------------
# Cache


def cache_dir() -> Path:
    """Parquet cache root (``<base>/hf_cache/corpus`` — data/ is gitignored)."""
    from .bins import get_base_dir

    root = Path(
        os.environ.get("MAILROOM_HF_CACHE_DIR", str(get_base_dir() / "hf_cache" / "corpus"))
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cached_get_bytes(url: str, *, label: str) -> bytes:
    """GET with an on-disk cache keyed by URL sha256 (repeat loads are
    network-free; a stale cache is just re-fetched by changing the URL —
    viewer parquet URLs carry the revision)."""
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    path = cache_dir() / f"{label}_{key}.bin"
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    data = _http_get_bytes(url)
    path.write_bytes(data)
    return data


# ---------------------------------------------------------------------------
# Parquet ladder


def parquet_urls(
    repo_id: str = FULL_CORPUS_ID, *, split: str = "train", revision: str | None = None
) -> dict[str, str]:
    """Viewer ``/parquet`` listing: config name -> parquet URL for one split.
    ``revision`` is passed through when the viewer supports it."""
    params: dict[str, Any] = {"dataset": repo_id}
    if revision:
        params["revision"] = revision
    listing = _http_get_json(f"{VIEWER_BASE}/parquet?" + _urlencode(params))
    out: dict[str, str] = {}
    for entry in listing.get("parquet_files") or []:
        if entry.get("split") == split and entry.get("url"):
            out[str(entry.get("config"))] = str(entry["url"])
    if not out:
        raise RuntimeError(f"/parquet listing empty for {repo_id} split={split}")
    return out


def _urlencode(params: dict[str, Any]) -> str:
    from urllib.parse import urlencode

    return urlencode({k: v for k, v in params.items() if v is not None})


def load_config_frame(
    repo_id: str = FULL_CORPUS_ID,
    config: str = GT_CONFIG,
    *,
    split: str = "train",
    revision: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """One config's split as a DataFrame: /parquet download (cached) with the
    /rows pagination ladder as fallback. Returns (frame, provenance)."""
    provenance: dict[str, Any] = {
        "dataset": repo_id,
        "config": config,
        "split": split,
        "revision": revision,
        "strategy": None,
        "fetched_at": _now(),
    }
    try:
        urls = parquet_urls(repo_id, split=split, revision=revision)
        url = urls.get(config)
        if url is None:
            raise RuntimeError(f"config {config!r} not in /parquet listing: {sorted(urls)}")
        raw = _cached_get_bytes(url, label=f"{repo_id.replace('/', '__')}_{config}_{split}")
        frame = pd.read_parquet(io.BytesIO(raw))
        provenance["strategy"] = "parquet"
        provenance["source_url"] = url
    except Exception as parquet_error:
        frame = _rows_ladder(repo_id, config, split=split, revision=revision)
        if frame.empty:
            raise RuntimeError(
                f"cannot load {repo_id}/{config}[{split}]: parquet failed "
                f"({type(parquet_error).__name__}: {parquet_error}) and /rows returned nothing"
            ) from parquet_error
        provenance["strategy"] = "rows_pagination"
    provenance["num_rows"] = int(len(frame))
    return frame, provenance


def _rows_ladder(
    repo_id: str, config: str, *, split: str, revision: str | None, page: int = 100
) -> pd.DataFrame:
    """Viewer ``/rows`` pagination fallback (transient-safe: 3 attempts per
    page; stops on a short page or num_rows_total)."""
    params: dict[str, Any] = {"dataset": repo_id, "config": config, "split": split}
    if revision:
        params["revision"] = revision
    rows: list[dict] = []
    offset = 0
    while True:
        page_params = dict(params, offset=offset, length=page)
        payload = None
        for attempt in range(3):
            try:
                payload = _http_get_json(f"{VIEWER_BASE}/rows?" + _urlencode(page_params))
                break
            except Exception:
                if attempt == 2:
                    payload = None
        if not payload:
            break
        batch = payload.get("rows") or []
        if not batch:
            break
        for item in batch:
            row = item.get("row") if isinstance(item, dict) else item
            if isinstance(row, dict):
                rows.append(row)
        offset += len(batch)
        total = payload.get("num_rows_total")
        if total is not None and offset >= int(total):
            break
        if len(batch) < page:
            break
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Corpus join + integrity


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_corpus(
    *,
    repo_id: str = FULL_CORPUS_ID,
    split: str = "train",
    revision: str | None = None,
    join: bool = True,
    verify: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """The mailroom-corpus split, labels joined to text on ``filename``.

    ``join=True`` merges ``ground_truth`` (labels) with ``default``
    (``doc_text``); ``verify=True`` checks ``content_sha256`` against the
    sha256 of every joined ``doc_text`` (the 1792/1792 exact proof — a
    mismatch counts are RECORDED, not silent). Returns (frame, provenance).
    """
    gt, gt_prov = load_config_frame(repo_id, GT_CONFIG, split=split, revision=revision)
    provenance: dict[str, Any] = {
        "dataset": repo_id,
        "split": split,
        "revision_requested": revision,
        "hub_sha_tip": dataset_sha(repo_id),
        "ground_truth": gt_prov,
        "fetched_at": _now(),
    }
    if not join:
        return gt, provenance
    blind, blind_prov = load_config_frame(repo_id, BLIND_CONFIG, split=split, revision=revision)
    provenance["default"] = blind_prov
    merged = gt.merge(
        blind[[JOIN_KEY, "doc_text"]], on=JOIN_KEY, how="left", validate="one_to_one"
    )
    if verify and "doc_text" in merged.columns:
        texts = merged["doc_text"].fillna("").astype(str)
        expected = merged.get("content_sha256")
        if expected is not None:
            computed = texts.map(_sha256_text)
            verified = (computed == expected.fillna("").astype(str)) & (texts.str.len() > 0)
            provenance["integrity"] = {
                "checked": int((texts.str.len() > 0).sum()),
                "verified": int(verified.sum()),
                "mismatched": int(((~verified) & (texts.str.len() > 0)).sum()),
            }
    provenance["num_rows"] = int(len(merged))
    return merged, provenance


def pick_document(
    frame: pd.DataFrame,
    *,
    doc_class: str | None = None,
    index: int | None = None,
    seed: int | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Deterministic document selection from a loaded corpus frame.

    ``index`` pins an absolute row; ``seed`` drives a stable sample among the
    (optionally class- and length-filtered) candidates. Returns the row dict
    plus its selection provenance.
    """
    import random

    candidates = frame
    if doc_class:
        mask = candidates.get("expected", pd.Series(dtype=str)).astype(str) == doc_class
        candidates = candidates[mask]
    if max_chars is not None and "doc_text" in candidates.columns:
        candidates = candidates[candidates["doc_text"].fillna("").str.len() <= max_chars]
    candidates = candidates.reset_index(drop=True)
    if candidates.empty:
        raise RuntimeError("no candidate documents after filters")
    if index is not None:
        pos = int(index)
        if pos >= len(candidates):
            raise RuntimeError(f"index {pos} out of range ({len(candidates)} candidates)")
    elif seed is not None:
        pos = random.Random(seed).randrange(len(candidates))
    else:
        pos = 0
    row = candidates.iloc[pos].to_dict()
    row["_selection"] = {
        "position": pos,
        "of": int(len(candidates)),
        "doc_class": doc_class,
        "index": index,
        "seed": seed,
        "max_chars": max_chars,
    }
    return row


# ---------------------------------------------------------------------------
# Committed offline snapshot (notebook default; refreshed via live loads)


def snapshot_path() -> Path:
    from pathlib import Path as _P

    return _P(__file__).resolve().parents[2] / "notebooks" / "fixtures" / "gmail_pilot_corpus_snapshot.json"


def load_snapshot() -> tuple[list[dict], dict[str, Any]]:
    """The committed offline snapshot rows + its provenance block."""
    path = snapshot_path()
    data = json.loads(path.read_text())
    rows = list(data.get("rows") or [])
    return rows, {k: v for k, v in data.items() if k != "rows"}
