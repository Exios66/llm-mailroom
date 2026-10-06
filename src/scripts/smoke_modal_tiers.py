"""End-to-end smoke: one document per class through the full stack, then
prove which Modal GPU tier served each graph node (Langfuse generation models).

Topology under test (deploy/docker-compose.full.yml):

    upload -> app (watcher + LangGraph) -> llm-gateway (LiteLLM) -> Modal tier
                                                         \\-> OpenRouter (api tier)

Each agent's tier comes from taxonomy.yaml ``agents.<name>.tier`` (+
``MAILROOM_GATEWAY_TIERS`` overrides). A GPU tier serves its weights under the
alias ``mailroom-<tier>`` (deploy/modal_vllm.py ``--served-model-name``), so
the model recorded on every Langfuse generation names the tier that answered.

Modes (combine freely; ``--check`` runs first whenever given):

    --check     network-free routing contract: every LLM agent resolves to a
                known tier, every GPU alias is routed by deploy/litellm/config.yaml,
                and every GPU tier is deployable by deploy/modal_vllm.py.
    --warm      wake every GPU tier in parallel through the gateway (one
                1-token completion each) so cold starts overlap, not stack.
    --run       one corpus document per doc class, end to end.
                Default: through the deployed API (``POST /upload`` into the
                embedded watcher) — run it on the compose host:
                  PYTHONPATH=src python src/scripts/smoke_modal_tiers.py --run \\
                      --api-url http://127.0.0.1:8000
                ``--in-process`` instead calls run_pipeline() here (needs
                DEFAULT_PROVIDER=litellm + a reachable LITELLM_BASE_URL).
    --verify-session ID   only audit an existing Langfuse session.

After ``--run`` the Langfuse session (= the smoke matter id) is fetched fresh
and every generation is attributed to its graph node and checked against the
tier its agent should use. Requires LANGFUSE_* keys (``--skip-langfuse``
keeps only the API/stage checks). A JSON + markdown report is written under
``<MAILROOM_BASE_DIR>/smoke_runs/<stamp>/``.

Documents come from the canonical corpus loader (pipeline/hf_corpus_loader.py,
``--source hf``, the default) or its committed offline snapshot
(``--source snapshot``, partial class coverage).

Exit status: 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.env import load_env  # noqa: E402

load_env()

from pipeline.logging import setup_logging  # noqa: E402

setup_logging()

REPO_ROOT = Path(__file__).resolve().parents[2]
LITELLM_CONFIG = REPO_ROOT / "deploy" / "litellm" / "config.yaml"
MODAL_APP = REPO_ROOT / "deploy" / "modal_vllm.py"

TERMINAL_STAGES = {"archived", "review", "failed", "aborted"}

# Graph node span names (graph/build_graph.py traced_node) -> the LLM agents
# that can call from inside that node. ``{specialist}`` is the doc class's
# taxonomy specialist.
NODE_AGENTS: dict[str, tuple[str, ...]] = {
    "intake-document": ("intake", "pdf_transcriber", "image_extractor"),
    "classify-document": ("sorter", "sorter_reviewer"),
    "extract-fields": ("{specialist}",),
    "judge-verify": ("judge",),
    "arbitrate-verdict": ("arbiter",),
    "adjudicate-conflict": ("boss",),
}

# Generations that are not LLM calls (the evaluator target) are skipped.
NON_LLM_GENERATIONS = {"pipeline-result"}


# ── routing contract ─────────────────────────────────────────────────────


def llm_agents(cfg: dict) -> list[str]:
    """Agents that make LLM calls (procedural agents are excluded)."""
    return [
        name
        for name, spec in (cfg.get("agents") or {}).items()
        if not (spec or {}).get("procedural")
    ]


def expected_routing(cfg: dict) -> dict[str, dict[str, Any]]:
    """{agent: {tier, wire_model, gpu}} for the gateway path, honoring
    ``MAILROOM_GATEWAY_TIERS``. Pure over the taxonomy (no provider/keys)."""
    from llm.client import gateway_alias, gateway_tier

    routing: dict[str, dict[str, Any]] = {}
    for agent in llm_agents(cfg):
        spec = cfg["agents"][agent]
        tier = gateway_tier(agent, spec)
        alias = gateway_alias(tier)
        routing[agent] = {
            "tier": tier,
            "wire_model": alias or spec.get("model"),
            "gpu": bool(alias),
        }
    return routing


def litellm_aliases(config_path: Path = LITELLM_CONFIG) -> set[str]:
    import yaml

    data = yaml.safe_load(config_path.read_text()) or {}
    return {str(m.get("model_name")) for m in data.get("model_list") or [] if m.get("model_name")}


def modal_tiers(app_path: Path = MODAL_APP) -> set[str]:
    """Tier names deploy/modal_vllm.py can serve (parsed, not imported — the
    runtime venv never has the `modal` package)."""
    match = re.search(r"^ALL_TIERS\s*=\s*\(([^)]*)\)", app_path.read_text(), re.M)
    if not match:
        return set()
    return set(re.findall(r"[\"']([a-z_]+)[\"']", match.group(1)))


def check_contract(cfg: dict) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Return (problems, routing). Network-free."""
    problems: list[str] = []
    try:
        routing = expected_routing(cfg)
    except ValueError as exc:
        return [str(exc)], {}
    tiers = (cfg.get("gateway") or {}).get("tiers") or {}
    aliases = litellm_aliases()
    served = modal_tiers()
    for tier, spec in tiers.items():
        alias = (spec or {}).get("alias")
        if not alias:
            continue
        if alias not in aliases:
            problems.append(f"tier '{tier}': alias '{alias}' has no model_list entry in {LITELLM_CONFIG.name}")
        if tier not in served:
            problems.append(f"tier '{tier}': not deployable by {MODAL_APP.name} (ALL_TIERS={sorted(served)})")
        if alias != f"mailroom-{tier}":
            problems.append(f"tier '{tier}': alias '{alias}' != served name 'mailroom-{tier}' (modal_vllm.py)")
    if "*" not in aliases and any(not r["gpu"] for r in routing.values()):
        problems.append("an agent is on the `api` tier but the gateway has no '*' OpenRouter passthrough")
    return problems, routing


def print_routing(routing: dict[str, dict[str, Any]]) -> None:
    print(f"{'agent':30s} {'tier':8s} wire model")
    for agent, r in sorted(routing.items(), key=lambda kv: (kv[1]["tier"], kv[0])):
        print(f"{agent:30s} {r['tier']:8s} {r['wire_model']}")


# ── gateway warm-up ──────────────────────────────────────────────────────


def _gateway_client(timeout: float):
    from openai import OpenAI

    base = os.environ.get("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1")
    key = os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY") or "not-needed"
    return OpenAI(base_url=base, api_key=key, timeout=timeout, max_retries=0)


def warm_tiers(cfg: dict, timeout: float = 900.0) -> dict[str, str]:
    """Wake every GPU tier concurrently; returns {alias: 'ok (Ns)' | error}."""
    aliases = sorted(
        {spec.get("alias") for spec in ((cfg.get("gateway") or {}).get("tiers") or {}).values() if spec and spec.get("alias")}
    )
    client = _gateway_client(timeout)

    def _wake(alias: str) -> str:
        started = time.perf_counter()
        deadline = started + timeout
        last = ""
        while time.perf_counter() < deadline:
            try:
                resp = client.chat.completions.create(
                    model=alias,
                    messages=[{"role": "user", "content": "Reply with the word ok."}],
                    max_tokens=1,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                return f"ok ({time.perf_counter() - started:.0f}s, served as {resp.model})"
            except Exception as exc:  # cold start: 503 / timeout -> keep waiting
                last = f"{type(exc).__name__}: {str(exc)[:160]}"
                time.sleep(15)
        return f"FAILED after {timeout:.0f}s — {last}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(aliases))) as pool:
        results = dict(zip(aliases, pool.map(_wake, aliases)))
    return results


# ── document selection ───────────────────────────────────────────────────


def doc_classes(cfg: dict) -> list[str]:
    return [c["key"] for c in cfg.get("doc_classes") or [] if c.get("key")]


def specialist_for(cfg: dict, doc_class: str) -> str | None:
    for c in cfg.get("doc_classes") or []:
        if c.get("key") == doc_class:
            return c.get("specialist")
    return None


def pick_documents(rows: list[dict], classes: list[str], target_chars: int) -> dict[str, dict]:
    """One row per class: the doc whose length is closest to ``target_chars``
    (deterministic; ties break on filename). Rows: {class, filename, doc_text}."""
    picked: dict[str, dict] = {}
    for cls in classes:
        candidates = [r for r in rows if r.get("class") == cls and (r.get("doc_text") or "").strip()]
        if not candidates:
            continue
        candidates.sort(key=lambda r: (abs(len(r["doc_text"]) - target_chars), str(r.get("filename"))))
        picked[cls] = candidates[0]
    return picked


def load_rows(source: str) -> list[dict]:
    from pipeline import hf_corpus_loader as loader

    if source == "snapshot":
        rows, _ = loader.load_snapshot()
        return [
            {"class": r["labels"].get("expected"), "filename": r["labels"].get("filename"), "doc_text": r.get("doc_text")}
            for r in rows
        ]
    frame, _ = loader.load_corpus()
    return [
        {"class": rec.get("expected"), "filename": rec.get("filename"), "doc_text": rec.get("doc_text")}
        for rec in frame.to_dict("records")
    ]


def write_document(out_dir: Path, doc_class: str, row: dict, stamp: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(str(row.get("filename") or "doc")).stem)[:60]
    path = out_dir / f"smoke_{stamp}_{doc_class}_{safe}.txt"
    path.write_text(row["doc_text"], encoding="utf-8")
    return path


# ── running ──────────────────────────────────────────────────────────────


def run_via_api(api_url: str, token: str, files: dict[str, Path], matter_id: str, timeout_s: float) -> dict[str, dict]:
    """Upload every file under one matter, then poll until each reaches a
    terminal stage. Returns {doc_class: {stage, doc_type, doc_id, filename}}."""
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    by_name: dict[str, str] = {}
    with httpx.Client(base_url=api_url.rstrip("/"), headers=headers, timeout=60.0) as http:
        for cls, path in files.items():
            with path.open("rb") as fh:
                resp = http.post(
                    "/upload",
                    files={"file": (path.name, fh, "text/plain")},
                    data={"matter_id": matter_id},
                )
            resp.raise_for_status()
            by_name[resp.json()["file"]] = cls
            print(f"  uploaded {cls:18s} -> {resp.json()['file']}")
        deadline = time.time() + timeout_s
        results: dict[str, dict] = {}
        while time.time() < deadline:
            try:
                resp = http.get(f"/matters/{matter_id}")
                resp.raise_for_status()
                docs = resp.json().get("documents") or []
            except (httpx.HTTPError, ValueError):
                time.sleep(10)
                continue
            for d in docs:
                cls = by_name.get(d.get("original_filename"))
                if cls and d.get("stage") in TERMINAL_STAGES:
                    results[cls] = {
                        "stage": d.get("stage"),
                        "doc_type": d.get("doc_type"),
                        "doc_id": d.get("doc_id"),
                        "filename": d.get("original_filename"),
                    }
            if len(results) == len(files):
                break
            time.sleep(10)
    for cls in files:
        results.setdefault(cls, {"stage": "timeout", "doc_type": None, "doc_id": None, "filename": files[cls].name})
    return results


def run_in_process(files: dict[str, Path], matter_id: str, run_id: str) -> dict[str, dict]:
    import shutil

    from graph.build_graph import run_pipeline
    from pipeline.bins import inbox_dir

    results: dict[str, dict] = {}
    for cls, path in files.items():
        inbox = inbox_dir()
        inbox.mkdir(parents=True, exist_ok=True)
        queued = inbox / path.name
        shutil.copyfile(path, queued)
        out = run_pipeline(
            queued,
            matter_id,
            source="smoke-modal",
            ground_truth={"expected_doc_class": cls, "expected_stage": "archived"},
            session_id=matter_id,
            run_id=run_id,
        )
        results[cls] = {
            "stage": out.get("stage"),
            "doc_type": out.get("doc_type"),
            "doc_id": out.get("doc_id"),
            "filename": path.name,
        }
        print(f"  {cls:18s} -> stage={out.get('stage')} doc_type={out.get('doc_type')}")
    from observability.tracing import flush

    flush()
    return results


# ── Langfuse verification ────────────────────────────────────────────────


def _obs_field(obs: dict, *names: str):
    for name in names:
        if obs.get(name) is not None:
            return obs[name]
    return None


def node_of(obs: dict, by_id: dict[str, dict]) -> str | None:
    """Walk parent observations up to the nearest graph-node span name."""
    seen = 0
    parent = _obs_field(obs, "parent_observation_id", "parentObservationId")
    while parent and seen < 50:
        node = by_id.get(parent)
        if node is None:
            return None
        name = node.get("name")
        if name in NODE_AGENTS:
            return name
        parent = _obs_field(node, "parent_observation_id", "parentObservationId")
        seen += 1
    return None


def allowed_models(agent: str, routing: dict[str, dict[str, Any]]) -> set[str]:
    r = routing.get(agent)
    return {r["wire_model"]} if r else set()


def _model_ok(model: str | None, allowed: set[str]) -> bool:
    if not model:
        return False
    # OpenRouter may answer with a dated variant of the champion slug.
    return any(model == a or model.startswith(f"{a}-") for a in allowed)


def verify_generations(
    observations: list[dict], routing: dict[str, dict[str, Any]], specialist: str | None
) -> tuple[list[dict], list[str]]:
    """Attribute each LLM generation to its node and check its model.

    A generation named after an agent (native path: ``name=<agent>``) is
    checked against that agent's tier exactly; otherwise (LangChain path) the
    node's possible agents bound the allowed tiers. Returns (rows, failures).
    """
    by_id = {o.get("id"): o for o in observations if o.get("id")}
    rows: list[dict] = []
    failures: list[str] = []
    for obs in observations:
        if str(obs.get("type", "")).upper() != "GENERATION":
            continue
        name = obs.get("name") or ""
        if name in NON_LLM_GENERATIONS:
            continue
        model = obs.get("model")
        node = node_of(obs, by_id)
        if name in routing:
            agents = (name,)
        elif node:
            agents = tuple(specialist if a == "{specialist}" else a for a in NODE_AGENTS[node] if a)
        else:
            agents = tuple(routing)
        allowed = set().union(*(allowed_models(a, routing) for a in agents if a)) if agents else set()
        tiers = sorted({routing[a]["tier"] for a in agents if a in routing})
        ok = _model_ok(model, allowed)
        rows.append({"node": node or "(unattributed)", "generation": name, "model": model, "expected": sorted(allowed), "tiers": tiers, "ok": ok})
        if not ok:
            failures.append(f"{node or '(unattributed)'} / {name or 'generation'}: model {model!r} not in {sorted(allowed)}")
    return rows, failures


def fetch_session_observations(session_id: str, expect_traces: int, wait_s: float = 180.0) -> dict[str, list[dict]]:
    """{trace_id: [observation dicts]} for a session, polling until the
    expected trace count has landed (ingestion is asynchronous)."""
    from langfuse import Langfuse

    client = Langfuse()
    deadline = time.time() + wait_s
    traces: list = []
    while time.time() < deadline:
        page = client.api.trace.list(session_id=session_id, limit=50)
        traces = list(page.data or [])
        if len(traces) >= expect_traces:
            break
        time.sleep(10)
    out: dict[str, list[dict]] = {}
    for trace in traces:
        obs_page = client.api.observations.get_many(trace_id=trace.id, limit=100)
        out[trace.id] = [o.model_dump(mode="json") for o in (obs_page.data or [])]
        out[trace.id].append({"id": None, "type": "TRACE_META", "name": trace.name, "metadata": trace.metadata})
    return out


def doc_class_of_trace(observations: list[dict], results: dict[str, dict]) -> str | None:
    """Match a trace to a run document via its curated input filename."""
    names = {r.get("filename"): cls for cls, r in results.items() if r.get("filename")}
    blob = json.dumps([o.get("input") for o in observations if o.get("input")] + [o.get("metadata") for o in observations])
    for filename, cls in names.items():
        if filename and filename in blob:
            return cls
    return None


# ── report ───────────────────────────────────────────────────────────────


def write_report(out_dir: Path, report: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    lines = [
        f"# Modal tier smoke — {report['stamp']}",
        "",
        f"- matter / Langfuse session: `{report['session_id']}`",
        f"- verdict: **{'PASS' if report['passed'] else 'FAIL'}**",
        "",
        "## Documents",
        "",
        "| class | stage | predicted | filename |",
        "|---|---|---|---|",
    ]
    for cls, r in sorted((report.get("documents") or {}).items()):
        lines.append(f"| {cls} | {r.get('stage')} | {r.get('doc_type')} | {r.get('filename')} |")
    lines += ["", "## Generations by node", "", "| class | node | generation | model | expected tier(s) | ok |", "|---|---|---|---|---|---|"]
    for row in report.get("generations") or []:
        lines.append(
            f"| {row.get('class')} | {row['node']} | {row['generation']} | {row['model']} | {', '.join(row['tiers'])} | {'✅' if row['ok'] else '❌'} |"
        )
    if report.get("failures"):
        lines += ["", "## Failures", ""] + [f"- {f}" for f in report["failures"]]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n")
    return path


# ── main ─────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="network-free routing contract")
    ap.add_argument("--warm", action="store_true", help="wake every GPU tier through the gateway")
    ap.add_argument("--run", action="store_true", help="one document per class end to end")
    ap.add_argument("--verify-session", default=None, help="only audit an existing Langfuse session")
    ap.add_argument("--in-process", action="store_true", help="call run_pipeline() here instead of the API")
    ap.add_argument("--api-url", default=os.environ.get("MAILROOM_SMOKE_API_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--source", choices=["hf", "snapshot"], default="hf")
    ap.add_argument("--classes", default="", help="comma list (default: every taxonomy doc class)")
    ap.add_argument("--target-chars", type=int, default=8000, help="pick docs closest to this length")
    ap.add_argument("--timeout", type=float, default=3600.0, help="seconds to wait for terminal stages")
    ap.add_argument("--skip-langfuse", action="store_true")
    args = ap.parse_args(argv)
    if not (args.check or args.warm or args.run or args.verify_session):
        args.check = True

    from pipeline.config import load_config

    cfg = load_config()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report: dict[str, Any] = {"stamp": stamp, "failures": [], "generations": [], "documents": {}}
    failures: list[str] = report["failures"]

    problems, routing = check_contract(cfg)
    report["routing"] = routing
    if args.check:
        print("== routing contract (gateway path) ==")
        print_routing(routing)
        for p in problems:
            print(f"  PROBLEM: {p}")
        print("contract:", "OK" if not problems else f"{len(problems)} problem(s)")
    failures.extend(problems)

    if args.warm:
        print("== warming GPU tiers (parallel) ==")
        for alias, status in warm_tiers(cfg).items():
            print(f"  {alias:18s} {status}")
            if not status.startswith("ok"):
                failures.append(f"warm {alias}: {status}")

    session_id = args.verify_session
    results: dict[str, dict] = {}
    if args.run:
        session_id = f"SMOKE-MODAL-{stamp}"
        report["session_id"] = session_id
        classes = [c for c in args.classes.split(",") if c] or doc_classes(cfg)
        rows = load_rows(args.source)
        picked = pick_documents(rows, classes, args.target_chars)
        for cls in classes:
            if cls not in picked:
                failures.append(f"no {args.source} document for class '{cls}'")
        base = Path(os.environ.get("MAILROOM_BASE_DIR", "./data"))
        doc_dir = base / "smoke_runs" / stamp / "documents"
        doc_dir.mkdir(parents=True, exist_ok=True)
        files = {cls: write_document(doc_dir, cls, row, stamp) for cls, row in picked.items()}
        print(f"== running {len(files)} document(s) as matter {session_id} ==")
        if args.in_process:
            results = run_in_process(files, session_id, run_id=stamp)
        else:
            results = run_via_api(args.api_url, os.environ.get("MAILROOM_API_TOKEN", ""), files, session_id, args.timeout)
        report["documents"] = results
        for cls, r in results.items():
            if r.get("stage") not in TERMINAL_STAGES:
                failures.append(f"{cls}: did not finish ({r.get('stage')})")
            elif r.get("stage") == "failed":
                failures.append(f"{cls}: pipeline failed")
            print(f"  {cls:18s} stage={r.get('stage')} predicted={r.get('doc_type')}")

    if session_id and not args.skip_langfuse:
        report["session_id"] = session_id
        print(f"== verifying Langfuse session {session_id} ==")
        try:
            traces = fetch_session_observations(session_id, expect_traces=max(1, len(results)))
        except Exception as exc:
            traces = {}
            failures.append(f"Langfuse fetch failed: {type(exc).__name__}: {exc}")
        if not traces:
            failures.append(f"no Langfuse traces in session {session_id}")
        for trace_id, observations in traces.items():
            cls = doc_class_of_trace(observations, results) if results else None
            specialist = specialist_for(cfg, cls) if cls else None
            rows, fails = verify_generations(observations, routing, specialist)
            if not rows:
                failures.append(f"trace {trace_id}: no LLM generations recorded")
            for row in rows:
                row["class"] = cls or "?"
                row["trace_id"] = trace_id
                print(f"  {row['class']:16s} {row['node']:20s} {row['generation'][:26]:26s} {str(row['model']):22s} {'ok' if row['ok'] else 'MISMATCH'}")
            report["generations"].extend(rows)
            failures.extend(f"{cls or trace_id}: {f}" for f in fails)

    report["passed"] = not failures
    if args.run or args.verify_session:
        out = write_report(Path(os.environ.get("MAILROOM_BASE_DIR", "./data")) / "smoke_runs" / stamp, report)
        print(f"report: {out}")
    for f in failures:
        print(f"FAIL: {f}")
    print("SMOKE", "PASS" if not failures else "FAIL")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
