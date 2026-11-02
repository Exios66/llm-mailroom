"""Langfuse sync — pull live experiment traces into the dojo scoring pipeline.

Two producer families land here:

- **llm-entity-extraction** (standalone repo) eval runners (``run_langfuse_*_eval.py``) trace
  per-document classification under ``sessionId = experiment_name`` with
  structured output (``doc_type_ok``, ``subtype_ok``, ``subtype_ok_equiv``,
  ``failure_mode``, …) and per-row scores (``exact_match``,
  ``subtype_accuracy``, ``subtype_accuracy_equiv``, ``confidence``).
- **llm-mailroom** ``document-pipeline`` traces (v0.9.0): filename,
  ``expected_hf_class``, exact/aligned accuracy, ``user_id`` / ``release`` /
  ``environment``, and ``normalize-intake`` span stats. Observation types
  follow :mod:`llm_dojo_scoring.mailroom` (chain / agent / evaluator / …).
  Score names on the wire may be the 35-char alias
  ``extraction_verified_precision``.

This module re-reads those traces, groups them into runs, and reconstructs
the SAME experiment-log records + workbook rows the pipeline exports — so
``dojo-analyze`` can score directly off the live sink with no manual
workbook export.

Synced sources:
- Langfuse (cloud or self-hosted): the primary experiment sink.
- Phoenix / OTLP local sink (``:6006``): read via :mod:`llm_dojo_scoring.phoenix_sync`
  when it is running; this module degrades gracefully when it is not.

Credential resolution (in priority order): shell environment variables, then
``langfuse.env``, then ``.env``. Keys: ``LANGFUSE_PUBLIC_KEY``,
``LANGFUSE_SECRET_KEY``, ``LANGFUSE_HOST``/``LANGFUSE_BASE_URL``,
``LANGFUSE_PROJECT``, ``LANGFUSE_ENVIRONMENT``, ``LANGFUSE_RELEASE``,
``MAILROOM_TRACE_USER_ID``, ``LANGFUSE_FLUSH_AT`` /
``LANGFUSE_FLUSH_INTERVAL``, ``OBSERVABILITY_ENVIRONMENT``.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from . import bootstrap as boot
from . import failure_modes as fm
from .config import PER_SUBTYPE, get_settings

# Trace names the pipeline uses (per task family).
SORTER_TRACE = "subtype_classification"
DOCCLASS_TRACE = "docclass_classification"
EXTRACTION_TRACE = "contract_entity_extraction"
CHAINED_TRACE = "chained_sorter_extractor"
PIPELINE_TRACE = "document-pipeline"


def _intake_fields_from_trace(trace: dict, out: dict, meta: dict) -> dict:
    """Pull ``normalize-intake`` span stats onto a pipeline row when present."""
    payload: dict = {}
    for obs in trace.get("observations") or []:
        if not isinstance(obs, dict):
            continue
        if obs.get("name") == "normalize-intake":
            obs_out = obs.get("output")
            if isinstance(obs_out, dict):
                payload = obs_out
            break

    def _first(*vals):
        """Return the first non-None value, preserving false and zero."""
        for val in vals:
            if val is not None:
                return val
        return None

    messy = _first(out.get("intake_messy"), payload.get("messy"), meta.get("intake_messy"))
    changed = _first(
        out.get("intake_changed"), payload.get("changed"), meta.get("intake_changed")
    )
    method = _first(
        out.get("intake_method"), payload.get("method"), meta.get("intake_method")
    )
    return {
        "intake_messy": None if messy is None else bool(messy),
        "intake_changed": None if changed is None else bool(changed),
        "intake_method": method,
        "intake_chars": _first(out.get("intake_chars"), payload.get("chars")),
        "intake_hyphen_unwraps": payload.get("hyphen_unwraps"),
        "intake_collapsed_blanks": payload.get("collapsed_blank_runs"),
    }


# Fallback file names for credential discovery (also tried as config/*.env).
_ENV_FILES = ("langfuse.env", ".env", "config/environments/langfuse.env")

DEFAULT_BASE_URL = "https://us.cloud.langfuse.com"
DEFAULT_PROJECT = "llm-dojo"
DEFAULT_ENVIRONMENT = "llm-dojo"
_DEFAULT_PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LangfuseConfig:
    """Langfuse endpoint, credentials, and optional project and SDK settings."""
    base_url: str
    public_key: str
    secret_key: str
    project: str = DEFAULT_PROJECT
    environment: str = DEFAULT_ENVIRONMENT
    release: str | None = None
    user_id: str | None = None
    flush_at: int | None = None
    flush_interval: float | None = None


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs. Prefer python-dotenv; fall back to a stdlib
    parser so an explicit env file still works when the extra is absent.
    Never overrides a non-empty variable already in the environment.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(path, override=False)
        return
    except ImportError:
        pass
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and os.environ.get(key) in (None, ""):
            os.environ[key] = value


def _discover_env_file() -> Path | None:
    """Find the first recognized credential file in working, config, or home directories."""
    cwd = Path.cwd()
    for name in _ENV_FILES:
        candidates = [cwd / name, cwd / "config" / name, Path.home() / name]
        for candidate in candidates:
            if candidate.exists():
                return candidate
    return None


def load_langfuse_config(env_file: str | Path | None = None) -> LangfuseConfig:
    """Resolve Langfuse credentials: shell env wins, then langfuse.env/.env."""
    if env_file is not None and str(env_file).strip():
        path = Path(env_file)
        if path.exists():
            _load_dotenv(path)
    else:
        found = _discover_env_file()
        if found is not None:
            _load_dotenv(found)

    def get(name: str, default: str = "") -> str:
        """Read an environment value, using the default for missing or empty strings."""
        value = os.environ.get(name)
        return value if value not in (None, "") else default

    def get_int(name: str) -> int | None:
        """Read an optional integer environment setting, returning None for invalid values."""
        raw = get(name)
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def get_float(name: str) -> float | None:
        """Read an optional float environment setting, returning None for invalid values."""
        raw = get(name)
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    base_url = get("LANGFUSE_BASE_URL") or get("LANGFUSE_HOST", DEFAULT_BASE_URL)
    environment = (
        get("LANGFUSE_ENVIRONMENT")
        or get("OBSERVABILITY_ENVIRONMENT")
        or get("LANGFUSE_TRACING_ENVIRONMENT")
        or DEFAULT_ENVIRONMENT
    )
    return LangfuseConfig(
        base_url=base_url.rstrip("/"),
        public_key=get("LANGFUSE_PUBLIC_KEY"),
        secret_key=get("LANGFUSE_SECRET_KEY"),
        project=get("LANGFUSE_PROJECT", DEFAULT_PROJECT),
        environment=environment,
        release=get("LANGFUSE_RELEASE") or None,
        user_id=get("MAILROOM_TRACE_USER_ID") or None,
        flush_at=get_int("LANGFUSE_FLUSH_AT"),
        flush_interval=get_float("LANGFUSE_FLUSH_INTERVAL"),
    )


# ---------------------------------------------------------------------------
# REST client (stdlib urllib — no extra dependency)
# ---------------------------------------------------------------------------


class LangfuseClient:
    """Thin REST client over the Langfuse public API."""

    def __init__(self, config: LangfuseConfig | None = None,
                 max_retries: int = 8, min_interval: float = 1.0):
        """Resolve credentials and configure Basic authentication, retries, and throttling."""
        self.config = config or load_langfuse_config()
        if not self.config.public_key or not self.config.secret_key:
            raise ValueError(
                "Langfuse credentials missing — set LANGFUSE_PUBLIC_KEY / "
                "LANGFUSE_SECRET_KEY or provide an env file."
            )
        token = f"{self.config.public_key}:{self.config.secret_key}"
        self._auth = "Basic " + base64.b64encode(token.encode()).decode()
        self.max_retries = max_retries
        self.min_interval = min_interval
        self._last_request_ts = 0.0

    # -- low-level ----------------------------------------------------------

    def _throttle(self) -> None:
        """Enforce a minimum interval between requests (Langfuse list
        endpoints rate-limit aggressively — ~15 req/min on /traces)."""
        now = time.monotonic()
        elapsed = now - self._last_request_ts
        wait = self.min_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    def _request(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """Fetch authenticated JSON with throttling and retries for rate limits or network errors."""
        url = self.config.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}, doseq=True
            )
        self._throttle()
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, headers={"Authorization": self._auth})
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:500]
                if exc.code == 429:
                    retry_after = 0
                    try:
                        retry_after = int(float(exc.headers.get("Retry-After", 0)))
                    except (TypeError, ValueError):
                        pass
                    if not retry_after:
                        try:
                            retry_after = int(json.loads(body).get("details", {}).get("retryAfterSeconds", 0))
                        except (json.JSONDecodeError, TypeError, ValueError):
                            retry_after = 2 ** attempt
                    wait = max(retry_after, 1) + (0.25 * attempt)
                    if attempt >= self.max_retries:
                        raise RuntimeError(f"Langfuse rate-limited (429) for {path} after retries") from exc
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"Langfuse API {exc.code} for {path}: {body}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt >= self.max_retries:
                    raise RuntimeError(f"Langfuse network error for {path}: {exc}") from exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Langfuse request failed for {path}")  # pragma: no cover

    # -- paginated accessors -------------------------------------------------

    def fetch_all(self, path: str, params: dict[str, Any] | None = None,
                  max_items: int | None = None, page_size: int = _DEFAULT_PAGE_SIZE) -> list[dict]:
        """Paginate a public API list endpoint until exhausted (or max_items)."""
        out: list[dict] = []
        params = dict(params or {})
        params.setdefault("limit", page_size)
        page = 1
        while True:
            params["page"] = page
            data = self._request(path, params)
            items = data.get("data") or []
            out.extend(items)
            if max_items is not None and len(out) >= max_items:
                return out[:max_items]
            meta = data.get("meta") or {}
            if page >= int(meta.get("totalPages") or page) or not items:
                break
            page += 1
        return out

    def list_traces(self, name: str | None = None, session: str | None = None,
                    max_items: int | None = None, from_ts: str | None = None,
                    to_ts: str | None = None) -> list[dict]:
        """Fetch traces matching optional name, session, and timestamp filters."""
        params = {"name": name, "sessionId": session, "fromTimestamp": from_ts,
                  "toTimestamp": to_ts}
        return self.fetch_all("/api/public/traces", params, max_items=max_items)

    def get_trace(self, trace_id: str) -> dict:
        """Fetch a single trace by its identifier."""
        return self._request(f"/api/public/traces/{trace_id}")

    def list_scores(self, trace_id: str | None = None, name: str | None = None,
                    max_items: int | None = None) -> list[dict]:
        """Fetch scores matching optional trace and score-name filters."""
        params = {"traceId": trace_id, "name": name}
        return self.fetch_all("/api/public/scores", params, max_items=max_items)

    def get_session(self, session_id: str) -> list[dict]:
        """Fetch all traces belonging to a session."""
        return self.list_traces(session=session_id)


# ---------------------------------------------------------------------------
# Trace -> per-row result
# ---------------------------------------------------------------------------

_TASK_ROW_KEYS: dict[str, tuple] = {
    SORTER_TRACE: ("expected_subtype", "subtype_ok", "subtype_ok_equiv",
                   "doc_type_ok", "contract_subtype", "confidence"),
    DOCCLASS_TRACE: ("expected_subclass", "subclass_ok", "subclass_ok_equiv",
                     "doc_type_ok", "doc_subclass", "confidence"),
}


def _row_from_pipeline_trace(trace: dict) -> dict | None:
    """Normalize a live ``document-pipeline`` trace (llm-mailroom / The-Mailroom)."""
    from .mailroom import GROUND_TRUTH_KEYS, align_doc_type, trace_identity

    inp = trace.get("input") or {}
    if not isinstance(inp, dict):
        inp = {}
    out = trace.get("output") or {}
    if not isinstance(out, dict):
        out = {}
    meta = trace.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    gt = inp.get("ground_truth") if isinstance(inp.get("ground_truth"), dict) else {}
    identity = trace_identity(trace)

    expected = None
    for key in GROUND_TRUTH_KEYS:
        expected = gt.get(key) or meta.get(key) or inp.get(key)
        if expected:
            break
    predicted = out.get("doc_type") or (out.get("sorter") or {}).get("doc_type")
    subclass = (
        gt.get("expected_subclass")
        or meta.get("expected_subclass")
        or (out.get("sorter") or {}).get("expected_subclass")
    )
    predicted_subclass = (
        (out.get("sorter") or {}).get("doc_subclass")
        or (out.get("sorter") or {}).get("contract_subtype")
        or out.get("doc_subclass")
    )
    if not expected and not predicted:
        return None
    exact_ok = (
        str(expected).strip().lower() == str(predicted).strip().lower()
        if expected and predicted
        else False
    )
    aligned_ok = (
        align_doc_type(expected) == align_doc_type(predicted)
        if expected and predicted
        else False
    )
    return {
        "filename": inp.get("filename"),
        "trace_id": identity["trace_id"],
        "session_id": identity["session_id"],
        "user_id": identity["user_id"],
        "release": identity["release"],
        "environment": identity["environment"],
        "expected": expected,
        "predicted": predicted,
        "expected_subclass": subclass,
        "predicted_subclass": predicted_subclass,
        "exact_ok": exact_ok,
        "aligned_ok": aligned_ok,
        "stage": out.get("stage"),
        "classification_confidence": out.get("classification_confidence")
        or (out.get("sorter") or {}).get("confidence"),
        "extraction_confidence": out.get("extraction_confidence"),
        "run_aborted": bool(out.get("run_aborted")),
        "tags": identity["tags"],
        **_intake_fields_from_trace(trace, out, meta),
    }


def row_from_trace(trace: dict, task: str = SORTER_TRACE) -> dict | None:
    """One normalized per-document result row from a Langfuse trace.

    Returns None for traces without sorter / pipeline output (skipped rows).
    """
    name = (trace.get("name") or task or "").strip()
    if task == PIPELINE_TRACE or name == PIPELINE_TRACE:
        return _row_from_pipeline_trace(trace)

    output = trace.get("output") or {}
    sorter = output.get("sorter") or output if isinstance(output, dict) else {}
    if not isinstance(sorter, dict) or not (
        sorter.get("doc_type") or sorter.get("expected_subtype")
        or sorter.get("expected_subclass")
    ):
        return None
    inp = trace.get("input") or {}
    identity = None
    try:
        from .mailroom import trace_identity

        identity = trace_identity(trace)
    except Exception:
        identity = {"trace_id": trace.get("id"), "user_id": None, "release": None}
    if task == SORTER_TRACE:
        return {
            "filename": inp.get("filename"),
            "trace_id": identity.get("trace_id") or trace.get("id"),
            "user_id": identity.get("user_id"),
            "release": identity.get("release"),
            "environment": identity.get("environment"),
            "expected_subtype": sorter.get("expected_subtype"),
            "contract_subtype": sorter.get("contract_subtype"),
            "subtype_ok": bool(sorter.get("subtype_ok")),
            "subtype_ok_equiv": bool(sorter.get("subtype_ok_equiv")),
            "doc_type_ok": bool(sorter.get("doc_type_ok")),
            "confidence": sorter.get("confidence"),
            "failure_mode": sorter.get("failure_mode"),
        }
    if task == DOCCLASS_TRACE:
        return {
            "filename": inp.get("filename"),
            "trace_id": identity.get("trace_id") or trace.get("id"),
            "user_id": identity.get("user_id"),
            "release": identity.get("release"),
            "environment": identity.get("environment"),
            "expected_subclass": sorter.get("expected_subclass"),
            "doc_subclass": sorter.get("doc_subclass"),
            "subclass_ok": bool(sorter.get("subclass_ok")),
            "subclass_ok_equiv": bool(sorter.get("subclass_ok_equiv")),
            "doc_type_ok": bool(sorter.get("doc_type_ok")),
            "confidence": sorter.get("confidence"),
            "failure_mode": sorter.get("failure_mode"),
        }
    return None


def group_rows_by_session(rows: list[tuple[str, dict]]) -> dict[str, list[dict]]:
    """Group per-doc rows by session id (experiment name)."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for session, row in rows:
        if session:
            groups[session].append(row)
    return dict(groups)


# ---------------------------------------------------------------------------
# Run-level aggregation -> experiment-log records
# ---------------------------------------------------------------------------


def aggregate_run(session: str, rows: list[dict], task: str = SORTER_TRACE,
                  model: str | None = None, prompt_version: str | None = None,
                  trace_ts: str | None = None) -> dict:
    """Aggregate one run's per-doc rows into an experiment-log record using
    the dojo failure-mode + bootstrap machinery (matches the pipeline schema).

    The record follows ``scores.sorter.*`` so :mod:`llm_dojo_scoring.export`
    column specs can render it into the reference workbook directly.
    """
    n = len(rows)
    if task == PIPELINE_TRACE:
        from .mailroom import score_aligned_classification

        expected = [r.get("expected") for r in rows]
        predicted = [r.get("predicted") for r in rows]
        aligned = score_aligned_classification(expected, predicted)
        n_sub = sum(
            1
            for r in rows
            if r.get("expected_subclass") not in (None, "")
            and r.get("predicted_subclass") not in (None, "")
        )
        n_sub_ok = sum(
            1
            for r in rows
            if r.get("expected_subclass") not in (None, "")
            and str(r.get("expected_subclass")).strip().lower()
            == str(r.get("predicted_subclass") or "").strip().lower()
        )
        subclass_acc = round(n_sub_ok / n_sub, 4) if n_sub else None
        return {
            "type": "experiment",
            "task": task,
            "experiment_name": session,
            "model": model,
            "prompt_versions": {"pipeline": prompt_version} if prompt_version else {},
            "timestamp": trace_ts or datetime.now(timezone.utc).isoformat(),
            "n_rows": n,
            "n_ok": aligned["n_aligned"],
            "user_id": next((r.get("user_id") for r in rows if r.get("user_id")), None),
            "release": next((r.get("release") for r in rows if r.get("release")), None),
            "environment": next((r.get("environment") for r in rows if r.get("environment")), None),
            "scores": {
                "pipeline": {
                    "exact_accuracy": aligned["exact_accuracy"],
                    "aligned_accuracy": aligned["aligned_accuracy"],
                    "subclass_accuracy": subclass_acc,
                    "n": n,
                    "n_subclass_scored": n_sub,
                }
            },
            "tokens": {"pipeline": {"rows_with_usage": n}},
            "data_source": {"project": f"langfuse:{DEFAULT_PROJECT}"},
        }
    if task == SORTER_TRACE:
        per_doc_acc = [1.0 if r["subtype_ok"] else 0.0 for r in rows]
        per_doc_equiv = [1.0 if r["subtype_ok_equiv"] else 0.0 for r in rows]
        per_doc_doc_type = [1.0 if r["doc_type_ok"] else 0.0 for r in rows]
        per_doc_conf = [float(r["confidence"]) if r.get("confidence") is not None else None
                        for r in rows]
        per_sub = fm.per_subtype_accuracy(rows)
        failures = fm.summarize_failures(rows)
        confusion = fm.confusion_from_rows(rows)
    else:  # docclass
        per_doc_acc = [1.0 if r["subclass_ok"] else 0.0 for r in rows]
        per_doc_equiv = [1.0 if r["subclass_ok_equiv"] else 0.0 for r in rows]
        per_doc_doc_type = [1.0 if r["doc_type_ok"] else 0.0 for r in rows]
        per_doc_conf = [float(r["confidence"]) if r.get("confidence") is not None else None
                        for r in rows]
        per_sub = {}
        for r in rows:
            key = r.get("expected_subclass")
            per_sub.setdefault(key, {"n": 0, "correct": 0, "correct_equiv": 0})
            per_sub[key]["n"] += 1
            per_sub[key]["correct"] += int(r["subclass_ok"])
            per_sub[key]["correct_equiv"] += int(r["subclass_ok_equiv"])
        per_sub = {k: {"n": v["n"], "correct": v["correct"],
                       "accuracy": round(v["correct"] / v["n"], 4) if v["n"] else 0.0,
                       "correct_equiv": v["correct_equiv"],
                       "accuracy_equiv": round(v["correct_equiv"] / v["n"], 4) if v["n"] else 0.0}
                   for k, v in per_sub.items()}
        failures = {
            "n_total": n, "n_failed": sum(1 for r in rows if not r["subclass_ok"]),
            "n_ok": sum(1 for r in rows if r["subclass_ok"]),
            "mode_counts": {},
            "failures": [],
        }
        confusion = [], []

    acc = sum(per_doc_acc) / n if n else 0.0
    acc_equiv = sum(per_doc_equiv) / n if n else 0.0
    doc_type = sum(per_doc_doc_type) / n if n else 0.0
    conf_vals = [c for c in per_doc_conf if c is not None]
    confidence = sum(conf_vals) / len(conf_vals) if conf_vals else None

    return {
        "type": "experiment",
        "task": task,
        "experiment_name": session,
        "model": model,
        "prompt_versions": {"sorter": prompt_version} if prompt_version else {},
        "timestamp": trace_ts or datetime.now(timezone.utc).isoformat(),
        "n_rows": n,
        "n_ok": n,
        "scores": {
            "sorter": {
                "exact_match": round(doc_type, 4),
                "exact_match_ci": boot.bootstrap_ci(per_doc_doc_type),
                "subtype_accuracy": round(acc, 4),
                "subtype_accuracy_ci": boot.bootstrap_ci(per_doc_acc),
                "subtype_accuracy_equiv": round(acc_equiv, 4),
                "confidence": round(confidence, 4) if confidence is not None else None,
                "failure_insights": {
                    "n_failed": failures["n_failed"],
                    "mode_counts": failures["mode_counts"],
                },
                "per_subtype": {
                    k: {
                        "accuracy": v["accuracy"],
                        "accuracy_equiv": v["accuracy_equiv"],
                        "correct": v["correct"],
                        "equiv": v["correct_equiv"] - v["correct"],
                        "total": v["n"],
                    }
                    for k, v in per_sub.items()
                },
                "confusion_matrix": confusion,
            }
        },
        "tokens": {"sorter": {"rows_with_usage": n}},
        "data_source": {"project": f"langfuse:{DEFAULT_PROJECT}"},
    }


# ---------------------------------------------------------------------------
# Top-level sync helpers
# ---------------------------------------------------------------------------


def fetch_run_records(client: LangfuseClient, task: str = SORTER_TRACE,
                      max_items: int | None = None,
                      session_filter: str | None = None) -> list[dict]:
    """Fetch traces for a task family and aggregate into run records.

    Uses the trace name as the filter (traces carry ``name`` = task family).
    Optionally restrict to one session (experiment) with ``session_filter``.
    """
    traces = client.list_traces(name=task, session=session_filter, max_items=max_items)
    rows = []
    ts_by_session: dict[str, str] = {}
    model_by_session: dict[str, str] = {}
    prompt_by_session: dict[str, str] = {}
    for trace in traces:
        session = trace.get("sessionId")
        if not session:
            continue
        row = row_from_trace(trace, task=task)
        if row is None:
            continue
        rows.append((session, row))
        if not ts_by_session.get(session) or trace.get("timestamp", "") < ts_by_session[session]:
            ts_by_session[session] = trace.get("timestamp", "")
        inp = trace.get("input") or {}
        model_by_session.setdefault(session, inp.get("model"))
        prompt_by_session.setdefault(session, inp.get("prompt_version"))
    grouped = group_rows_by_session(rows)
    records = []
    for session in sorted(grouped):
        records.append(aggregate_run(
            session, grouped[session], task=task,
            model=model_by_session.get(session),
            prompt_version=prompt_by_session.get(session),
            trace_ts=ts_by_session.get(session),
        ))
    return records


def records_to_sorter_frame(records: list[dict]) -> pd.DataFrame:
    """Render run records into the reference sorter workbook schema (the same
    columns ``dojo-export`` produces) as a DataFrame — usable directly by
    :func:`llm_dojo_scoring.io.normalize_results_frame` and ``dojo-analyze``."""
    from .export import sorter_columns

    cols = sorter_columns()
    rows = []
    for rec in records:
        row = {}
        for col in cols:
            value = col["get"](rec)
            if isinstance(value, (list, tuple)) and value:
                value = str(value)
            row[col["header"]] = value
        rows.append(row)
    return pd.DataFrame(rows)


def sync_sorter_results(outdir: str = ".", task: str = SORTER_TRACE,
                        max_items: int | None = None,
                        env_file: str | Path | None = None,
                        workbook: str | None = None,
                        session: str | None = None) -> tuple[pd.DataFrame, str | None]:
    """Full sync: Langfuse -> run records -> reference workbook.

    Returns ``(frame, workbook_path_or_None)``. ``workbook`` defaults to
    ``Sorter_Experiment_Results.xlsx`` in ``outdir`` when the task is the
    sorter family. Set ``workbook=False`` to skip writing the xlsx.
    ``session`` restricts the sync to one experiment (sessionId).
    """
    client = LangfuseClient(load_langfuse_config(env_file))
    records = fetch_run_records(client, task=task, max_items=max_items,
                                session_filter=session)
    frame = records_to_sorter_frame(records)
    path = None
    if workbook is not False:
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        path = str(out / (workbook or "Sorter_Experiment_Results.xlsx"))
        from .export import sorter_columns, write_codebook, write_workbook

        write_workbook(path, "Eval Results", sorter_columns(), records,
                       codebook_sheet=True)
        write_codebook(str(out / "Sorter_Experiment_Codebook.csv"), sorter_columns())
    return frame, path


__all__ = [
    "LangfuseConfig", "LangfuseClient", "load_langfuse_config",
    "row_from_trace", "group_rows_by_session", "aggregate_run",
    "fetch_run_records", "records_to_sorter_frame", "sync_sorter_results",
    "SORTER_TRACE", "DOCCLASS_TRACE", "EXTRACTION_TRACE", "CHAINED_TRACE",
]
