#!/usr/bin/env python3
"""Mirror Langfuse run logs into the local repository for offline analysis.

Fetches traces (with nested observations and scores) from Langfuse and writes
them as one JSON file per trace under `data/langfuse_logs/<run>/`, where `<run>`
is a timestamped directory. Each sync also writes `index.json` mapping trace id
-> file, latency, stage, and doc id, so subagents and dashboards can navigate
the mirror without re-querying Langfuse.

The mirrored files contain the full trace detail (inputs, outputs, latency,
token/cost usage, scores, and the linked prompt versions) and are safe to hand
to analysis subagents.

Usage:
    python scripts/sync_langfuse_logs.py                    # last 24h, latest first
    python scripts/sync_langfuse_logs.py --since 7d         # last 7 days
    python scripts/sync_langfuse_logs.py --since 2026-08-01
    python scripts/sync_langfuse_logs.py --limit 50
    python scripts/sync_langfuse_logs.py --trace-id <id>    # one specific trace
    python scripts/sync_langfuse_logs.py --output /tmp/lf_logs
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from pipeline.env import load_env  # noqa: E402

from pipeline.env import default_environment, load_env  # noqa: E402

load_env()
default_environment("misc")

from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

DEFAULT_OUTPUT = Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / "langfuse_logs"


def _client():
    from observability.langfuse_setup import _NoopLangfuse, get_langfuse_client

    client = get_langfuse_client()
    if isinstance(client, _NoopLangfuse):
        print("Langfuse is not configured (LANGFUSE_SECRET_KEY missing) — cannot fetch logs.")
        return None
    return client


def _parse_since(raw: str | None) -> datetime | None:
    if not raw:
        return datetime.now(timezone.utc) - timedelta(hours=24)
    raw = raw.strip()
    if raw.endswith("h"):
        return datetime.now(timezone.utc) - timedelta(hours=int(raw[:-1]))
    if raw.endswith("d"):
        return datetime.now(timezone.utc) - timedelta(days=int(raw[:-1]))
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"Cannot parse --since '{raw}' (use e.g. 24h, 7d, or an ISO date).")
        raise SystemExit(2)


def _trace_basics(trace_id: str, trace: dict) -> dict:
    latency = trace.get("latency")
    total_cost = trace.get("total_cost")
    return {
        "trace_id": trace_id,
        "name": trace.get("name"),
        "timestamp": str(trace.get("timestamp", "")),
        "latency_s": round(latency, 3) if isinstance(latency, (int, float)) else None,
        "total_cost": total_cost,
        "session_id": trace.get("session_id"),
        "tags": trace.get("tags") or [],
        "environment": trace.get("environment"),
        "input": trace.get("input"),
        "output": trace.get("output"),
    }


def _trace_stage(trace: dict) -> str | None:
    output = trace.get("output")
    if isinstance(output, dict):
        return output.get("stage")
    return None


def _wait_for_scores(client, trace_id: str, timeout_s: float) -> None:
    """Poll a trace's scores until they arrive or the timeout elapses.

    LLM-as-a-judge evaluators run asynchronously in Langfuse; a sync run that
    starts right after a pilot run may otherwise mirror traces with empty
    score arrays (pilot audit issue #9). Polls the scores API and prints a
    note when it gives up.
    """
    import time as _time

    deadline = _time.time() + timeout_s
    seen = 0
    while _time.time() < deadline:
        try:
            page = client.api.scores.get_many(trace_id=trace_id, limit=100)
            seen = len(page.data or [])
            if seen > 0:
                return
        except Exception:
            pass
        _time.sleep(5)
    logger.warning("scores_not_ready", trace_id=trace_id, waited_s=timeout_s, scores=seen)


def sync_logs(
    client,
    output_dir: Path,
    *,
    since: datetime,
    limit: int,
    only_trace: str | None,
    wait_scores_s: float = 0.0,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = output_dir / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    traces = []
    if only_trace:
        try:
            trace = client.api.trace.get(only_trace)
            traces = [trace]
        except Exception:
            logger.warning("trace_fetch_failed", trace_id=only_trace, exc_info=True)
    else:
        # Note: `order_by` is omitted — the API rejects most formats; we sort
        # locally instead.
        page = client.api.trace.list(
            limit=limit,
            from_timestamp=since,
        )
        traces = sorted(list(page.data or []), key=lambda t: t.timestamp, reverse=True)

    if not traces:
        print(f"No traces found (since {since.isoformat()}).")
        return 0

    index = []
    for trace in traces:
        trace_id = trace.id
        if wait_scores_s > 0:
            _wait_for_scores(client, trace_id, wait_scores_s)
        dump = trace.model_dump(mode="json")
        # `trace.get` only returns observation *ids*; expand the details and
        # scores so the mirrored logs are directly analyzable.
        try:
            obs_page = client.api.observations.get_many(trace_id=trace_id, limit=100)
            dump["observations_detail"] = [o.model_dump(mode="json") for o in (obs_page.data or [])]
        except Exception:
            logger.warning("observations_fetch_failed", trace_id=trace_id, exc_info=True)
        try:
            scores_page = client.api.scores.get_many(trace_id=trace_id, limit=100)
            dump["scores_detail"] = [s.model_dump(mode="json") for s in (scores_page.data or [])]
        except Exception:
            logger.warning("scores_fetch_failed", trace_id=trace_id, exc_info=True)
        file_path = run_dir / f"{trace_id}.json"
        file_path.write_text(json.dumps(dump, indent=2))
        basics = _trace_basics(trace_id, dump)
        basics["file"] = str(file_path)
        basics["stage"] = _trace_stage(dump)
        index.append(basics)

    index.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    index_path = run_dir / "index.json"
    index_path.write_text(json.dumps({"run": str(run_dir), "count": len(index), "traces": index}, indent=2))
    print(f"Wrote {len(index)} trace log(s) to {run_dir}/")
    print(f"Index: {index_path}")

    stages: dict[str, int] = {}
    for r in index:
        stages[r.get("stage") or "unknown"] = stages.get(r.get("stage") or "unknown", 0) + 1
    if stages:
        print("Stage breakdown:", ", ".join(f"{k}={v}" for k, v in sorted(stages.items())))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror Langfuse traces to a local directory.")
    parser.add_argument("--since", default=None, help="Backfill window: 24h, 7d, or an ISO date (default 24h).")
    parser.add_argument("--limit", type=int, default=100, help="Max traces to fetch (default 100).")
    parser.add_argument("--trace-id", default=None, help="Fetch a single trace by id.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output directory.")
    parser.add_argument(
        "--wait-scores",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Before writing each trace, poll for its scores for up to SECONDS "
        "(LLM-as-a-judge scores arrive asynchronously; 0 disables waiting).",
    )
    args = parser.parse_args()

    since = _parse_since(args.since)
    client = _client()
    if client is None:
        return 1
    return sync_logs(
        client,
        args.output,
        since=since,
        limit=args.limit,
        only_trace=args.trace_id,
        wait_scores_s=args.wait_scores,
    )


if __name__ == "__main__":
    raise SystemExit(main())
