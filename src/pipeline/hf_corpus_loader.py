"""Canonical mailroom-dataset HuggingFace dataset loader (HUB-053).

The ONE loading path for the mailroom-corpus family (``Lucius-Morningstar/
mailroom-dataset``, v9 successor of the frozen v8 `mailroom-corpus`, internal
slug ``docclass-merged``) — scripts, notebooks,
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
  the load-time integrity proof (verified 2979/2979 exact match (v9 train)).
- The Dataset Viewer ``/filter`` endpoint has been intermittently broken
  server-side (422/500/502); for an UNPINNED load the robust fetch is
  ``/parquet`` (every shard of the split is downloaded and concatenated)
  with ``/rows`` pagination (``length<=100``) as the fallback ladder.
- The Viewer always serves the dataset's CURRENT revision: a ``revision=``
  query parameter on ``/parquet`` or ``/rows`` is not a pin. A PINNED load
  (``revision`` set) therefore never touches the Viewer: the revision is
  resolved to a commit sha through the Hub API
  (``/api/datasets/{id}/revision/{rev}``), the config's data files are
  selected from that revision's own ``configs`` card and file listing, and
  every shard is fetched from ``/datasets/{id}/resolve/{sha}/...``. If any
  step cannot be enforced the load FAILS; it never falls back to Viewer data
  while labelling it as the pin.
- The Hub single-repo API is ``https://huggingface.co/api/datasets/{id}``
  with a LITERAL slash (URL-encoding the slash is a 400); the
  ``?author=&search=&expand[]=sha`` listing is the fallback.
- Reads are anonymous-friendly (public, ungated); ``HF_TOKEN`` is optional
  headroom for quota. Never log or commit tokens.

``datasets.load_dataset`` is NOT a dependency of this workspace — the
loader is a stdlib + httpx + pandas ladder with an on-disk parquet cache
(``<base>/hf_cache/``); pinned loads are keyed by the resolved commit sha,
so a moved tag can never be served from a stale cache entry.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .env import load_env
from .hf_corpora import FULL_CORPUS_REVISION

_log = logging.getLogger("llm_mailroom.hf_corpus_loader")

HUB_BASE = "https://huggingface.co"
VIEWER_BASE = "https://datasets-server.huggingface.co"
ORG = "Lucius-Morningstar"
FULL_CORPUS_ID = f"{ORG}/mailroom-dataset"
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
    _log.warning(
        "hub tip sha for %s could NOT be resolved (both API routes failed) — "
        "provenance will record hub_sha_tip: None and pinned-vs-tip drift "
        "cannot be checked for this load",
        repo_id,
    )
    return None


# ---------------------------------------------------------------------------
# HTTP (network imports stay INSIDE functions — the notebook guard suite
# bans module-scope network imports; test_notebook_suite.py Duty 4)


def _token_headers() -> dict[str, str]:
    headers = {"User-Agent": "llm-mailroom-loader/1.0"}
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
# Pinned-revision ladder (Hub API + resolve/<sha>; the only revision-honoring path)

_DATA_EXTS = (".parquet", ".jsonl")
_OTHER_SPLITS = ("test", "validation", "val", "dev")


def resolve_revision(repo_id: str, revision: str) -> dict[str, Any]:
    """Resolve a tag / branch / sha to ``{"sha", "siblings", "configs"}``.

    Raises RuntimeError when the Hub cannot resolve the revision: a pin that
    cannot be resolved cannot be enforced, so the caller must fail the load.
    """
    from urllib.parse import quote

    url = f"{HUB_BASE}/api/datasets/{repo_id}/revision/{quote(revision, safe='')}"
    try:
        info = _http_get_json(url)
    except Exception as exc:
        raise RuntimeError(
            f"cannot resolve revision {revision!r} of {repo_id} "
            f"({type(exc).__name__}: {exc}) — refusing to load unpinned data as the pin"
        ) from exc
    sha = info.get("sha") if isinstance(info, dict) else None
    if not sha:
        raise RuntimeError(f"Hub returned no commit sha for {repo_id}@{revision!r}")
    return {
        "sha": str(sha),
        "siblings": [
            str(s.get("rfilename")) for s in (info.get("siblings") or []) if s.get("rfilename")
        ],
        "configs": (info.get("cardData") or {}).get("configs"),
    }


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def revision_data_files(info: dict[str, Any], config: str, split: str) -> list[str]:
    """Data files of one config/split at the resolved revision, in shard order.

    Uses the revision's own ``configs`` card (``data_files`` globs) when it
    declares the config; a repo without a card falls back to the implicit
    ``default`` config (``<split>.*`` / ``<split>-NNNNN-of-NNNNN.*`` files, or
    the lone data file for ``train``). Returns [] when nothing matches.
    """
    siblings: list[str] = info["siblings"]
    data_siblings = [f for f in siblings if f.lower().endswith(_DATA_EXTS)]
    configs = info.get("configs")
    if configs:
        for entry in configs:
            if str(entry.get("config_name") or "default") != config:
                continue
            files = entry.get("data_files")
            if isinstance(files, str):
                files = [{"split": "train", "path": files}]
            matched: list[str] = []
            for item in files or []:
                if isinstance(item, str):
                    item = {"split": "train", "path": item}
                if str(item.get("split")) != split:
                    continue
                paths = item.get("path")
                for glob in [paths] if isinstance(paths, str) else list(paths or []):
                    rx = _glob_regex(str(glob))
                    matched.extend(f for f in data_siblings if rx.match(f))
            return sorted(set(matched))
        return []
    if config != "default":
        return []
    named = [
        f for f in data_siblings
        if re.match(rf"{re.escape(split)}([-_.]|$)", Path(f).name.lower())
        or re.search(rf"(^|/){re.escape(split)}/", f.lower())
    ]
    if named:
        return sorted(named)
    if split == "train":
        rest = [
            f for f in data_siblings
            if not any(re.match(rf"{o}([-_.]|$)", Path(f).name.lower()) for o in _OTHER_SPLITS)
        ]
        return sorted(rest)
    return []


def _read_data_file(raw: bytes, path: str) -> pd.DataFrame:
    if path.lower().endswith(".jsonl"):
        return pd.read_json(io.BytesIO(raw), lines=True)
    return pd.read_parquet(io.BytesIO(raw))


def load_revision_frame(
    repo_id: str,
    config: str,
    *,
    split: str,
    revision: str,
    max_rows: int | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """One config's split at EXACTLY ``revision``: every shard, concatenated.

    Raises RuntimeError when the revision cannot be resolved or the config
    has no data files there. ``max_rows`` stops fetching further shards once
    that many rows are held (the frame is then cut to ``max_rows``).
    """
    info = resolve_revision(repo_id, revision)
    sha = info["sha"]
    files = revision_data_files(info, config, split)
    if not files:
        raise RuntimeError(
            f"no data files for {repo_id}/{config}[{split}] at revision "
            f"{revision!r} ({sha[:12]}): the revision's configs card and file "
            f"listing did not declare it — refusing to fall back to the Viewer"
        )
    from urllib.parse import quote

    frames: list[pd.DataFrame] = []
    urls: list[str] = []
    held = 0
    for path in files:
        url = f"{HUB_BASE}/datasets/{repo_id}/resolve/{sha}/{quote(path)}"
        raw = _cached_get_bytes(url, label=f"{repo_id.replace('/', '__')}_{config}_{split}")
        part = _read_data_file(raw, path)
        frames.append(part)
        urls.append(url)
        held += len(part)
        if max_rows is not None and held >= max_rows:
            break
    frame = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    if max_rows is not None:
        frame = frame.head(max_rows).reset_index(drop=True)
    provenance = {
        "revision_resolved": sha,
        "strategy": "hub_resolve",
        "source_url": urls[0],
        "source_urls": urls,
        "shards": len(urls),
        "shards_total": len(files),
    }
    return frame, provenance


# ---------------------------------------------------------------------------
# Viewer parquet ladder (UNPINNED loads only: the Viewer serves the current
# revision and cannot be pinned)


def parquet_urls(repo_id: str = FULL_CORPUS_ID, *, split: str = "train") -> dict[str, list[str]]:
    """Viewer ``/parquet`` listing: config name -> ALL shard URLs of one split
    (listing order). The Viewer serves the current revision only."""
    listing = _http_get_json(f"{VIEWER_BASE}/parquet?" + _urlencode({"dataset": repo_id}))
    out: dict[str, list[str]] = {}
    for entry in sorted(
        (e for e in listing.get("parquet_files") or [] if e.get("split") == split and e.get("url")),
        key=lambda e: str(e.get("filename") or e.get("url")),
    ):
        out.setdefault(str(entry.get("config")), []).append(str(entry["url"]))
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
    max_rows: int | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """One config's split as a DataFrame, ALL shards concatenated.

    ``revision`` set: the data is fetched from exactly that revision through
    the Hub (``load_revision_frame``); any failure to enforce it raises — the
    Viewer is never used, because it cannot honor a pin. ``revision=None``:
    Viewer ``/parquet`` download (cached) with the ``/rows`` pagination ladder
    as fallback, and the provenance says ``revision: None``. Returns
    (frame, provenance).
    """
    provenance: dict[str, Any] = {
        "dataset": repo_id,
        "config": config,
        "split": split,
        "revision": revision,
        "revision_resolved": None,
        "strategy": None,
        "fetched_at": _now(),
    }
    if revision:
        frame, extra = load_revision_frame(
            repo_id, config, split=split, revision=revision, max_rows=max_rows
        )
        provenance.update(extra)
        provenance["num_rows"] = int(len(frame))
        return frame, provenance
    try:
        urls = parquet_urls(repo_id, split=split)
        shard_urls = urls.get(config)
        if not shard_urls:
            raise RuntimeError(f"config {config!r} not in /parquet listing: {sorted(urls)}")
        parts: list[pd.DataFrame] = []
        held = 0
        for url in shard_urls:
            raw = _cached_get_bytes(url, label=f"{repo_id.replace('/', '__')}_{config}_{split}")
            parts.append(pd.read_parquet(io.BytesIO(raw)))
            held += len(parts[-1])
            if max_rows is not None and held >= max_rows:
                break
        frame = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
        if max_rows is not None:
            frame = frame.head(max_rows).reset_index(drop=True)
        provenance["strategy"] = "parquet"
        provenance["source_url"] = shard_urls[0]
        provenance["source_urls"] = shard_urls[: len(parts)]
        provenance["shards"] = len(parts)
        provenance["shards_total"] = len(shard_urls)
    except Exception as parquet_error:
        frame = _rows_ladder(repo_id, config, split=split, revision=None)
        if frame.empty:
            raise RuntimeError(
                f"cannot load {repo_id}/{config}[{split}]: parquet failed "
                f"({type(parquet_error).__name__}: {parquet_error}) and /rows returned nothing"
            ) from parquet_error
        if max_rows is not None:
            frame = frame.head(max_rows).reset_index(drop=True)
        provenance["strategy"] = "rows_pagination"
    provenance["num_rows"] = int(len(frame))
    return frame, provenance


def _rows_ladder(
    repo_id: str, config: str, *, split: str, revision: str | None, page: int = 100
) -> pd.DataFrame:
    """Viewer ``/rows`` pagination fallback for UNPINNED loads (transient-safe:
    3 attempts per page; stops on a short page or num_rows_total). Raises
    ValueError for a pinned ``revision``: the Viewer cannot enforce one."""
    if revision:
        # The Viewer cannot honor a pin (it serves the current revision):
        # refuse instead of returning unpinned rows under a pinned label.
        raise ValueError("the Viewer /rows ladder cannot enforce a pinned revision")
    params: dict[str, Any] = {"dataset": repo_id, "config": config, "split": split}
    rows: list[dict] = []
    offset = 0
    known_total: int | None = None
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
            # A short page / num_rows_total is a CLEAN stop; a fetch failure
            # mid-pagination is data loss — silently returning the partial
            # frame collects w/o error (DMR-061).
            if rows and (known_total is None or offset < int(known_total)):
                raise RuntimeError(
                    f"rows ladder for {repo_id}/{config}[{split}] stopped at "
                    f"offset={offset} ({len(rows)} rows collected, "
                    f"total={known_total}) because a page fetch failed after 3 "
                    f"attempts — refusing to return a TRUNCATED frame; re-run "
                    f"or fall back to the /parquet ladder"
                )
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
        if total is not None:
            known_total = int(total)
            if offset >= known_total:
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
    # DMR-052 (G25): the loader used to float on the Hub tip while the rest
    # of the family pins the publisher's canonical sha — default to the same
    # pin the ground-truth publisher re-pins on every publish.
    revision: str | None = FULL_CORPUS_REVISION,
    join: bool = True,
    verify: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """The mailroom-corpus split, labels joined to text on ``filename``.

    ``join=True`` merges ``ground_truth`` (labels) with ``default``
    (``doc_text``); ``verify=True`` checks ``content_sha256`` against the
    sha256 of every joined ``doc_text`` (the 2979/2979 exact proof (v9 train) — a
    mismatch counts are RECORDED, not silent). Returns (frame, provenance).
    """
    gt, gt_prov = load_config_frame(repo_id, GT_CONFIG, split=split, revision=revision)
    provenance: dict[str, Any] = {
        "dataset": repo_id,
        "split": split,
        "revision_requested": revision,
        "revision_resolved": gt_prov.get("revision_resolved"),
        "hub_sha_tip": dataset_sha(repo_id),
        "ground_truth": gt_prov,
        "fetched_at": _now(),
    }
    hub_tip = provenance["hub_sha_tip"]
    if hub_tip is None:
        provenance["hub_sha_tip_unavailable"] = True
    elif revision and hub_tip != (gt_prov.get("revision_resolved") or revision):
        # Compare against the RESOLVED commit sha: a tag pin ("v9.3") never
        # equals a sha, so comparing the raw pin would always report drift.
        pinned_sha = gt_prov.get("revision_resolved") or revision
        _log.warning(
            "hub tip %s differs from the requested pinned revision %s (%s) — "
            "the dataset moved upstream; this load stays on the pin, but the "
            "next publish roll will re-pin (provenance records both)",
            hub_tip,
            revision,
            pinned_sha,
        )
        provenance["hub_tip_drift"] = {"tip": hub_tip, "pinned": pinned_sha}
    if not join:
        return gt, provenance
    blind, blind_prov = load_config_frame(repo_id, BLIND_CONFIG, split=split, revision=revision)
    provenance["default"] = blind_prov
    if gt_prov.get("revision_resolved") != blind_prov.get("revision_resolved"):
        # A moving tag/branch resolved to two commits between the two config
        # loads: joining them would mix revisions under one pin.
        raise RuntimeError(
            f"revision {revision!r} of {repo_id} resolved to different commits for "
            f"{GT_CONFIG} ({gt_prov.get('revision_resolved')}) and {BLIND_CONFIG} "
            f"({blind_prov.get('revision_resolved')}) — refusing to join mixed revisions; "
            f"pin a commit sha"
        )
    merged = gt.merge(
        blind[[JOIN_KEY, "doc_text"]], on=JOIN_KEY, how="left", validate="one_to_one"
    )
    unmatched = int((~gt[JOIN_KEY].isin(blind[JOIN_KEY])).sum())
    provenance["unmatched_text"] = unmatched
    if unmatched:
        # The left join leaves doc_text null for these rows and the integrity
        # check below only counts rows WITH text, so without this they vanish
        # from every report (#195).
        _log.warning(
            "%d of %d %s rows in %s[%s@%s] have no %s row with a matching "
            "%s — their doc_text is null and they are excluded from the "
            "content_sha256 integrity check",
            unmatched,
            len(gt),
            GT_CONFIG,
            repo_id,
            split,
            revision,
            BLIND_CONFIG,
            JOIN_KEY,
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
            if provenance["integrity"]["mismatched"]:
                # A decode-corrupted row must never flow into GT-joined scoring
                # silently (#195): say it loudly, name the count.
                _log.warning(
                    "content_sha256 mismatch for %s/%s[%s@%s]: %d of %d rows "
                    "failed the integrity check — scores computed on mutated "
                    "text are suspect; re-pin or re-download",
                    repo_id,
                    GT_CONFIG,
                    split,
                    revision,
                    provenance["integrity"]["mismatched"],
                    provenance["integrity"]["checked"],
                )
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
