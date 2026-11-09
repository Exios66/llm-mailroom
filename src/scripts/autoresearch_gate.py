#!/usr/bin/env python3
"""Keep-or-discard gate for one autoresearch hill-climb iteration.

Reads the objective registry (``experiments/autoresearch/objectives.yaml``),
runs the CORE guards first (compileall, affected tests, the hermetic
experiment gate, the display contract, the changelog check) so a change that
breaks core functionality is discarded whatever its score, then runs the
objective's own eval and decides:

- ``crash``       the eval did not run (exit code other than 0 or 2, an eval command that
                  cannot be formatted, or no SCORE line for a scored objective; with
                  ``require_delta_ci``, no ΔU point estimate for the first candidate)
- ``guard_fail``  a core guard failed, the eval printed a ``GUARD … FAIL`` /
                  ``GATE … FAIL`` line or exited 2, or the required ΔU interval
                  did not exclude 0
- ``baseline``    first scored result for the objective
- ``keep``        every guard passed and the score beats the best kept score by
                  ``min_delta`` in the objective's direction
- ``discard``     every guard passed but the score did not clear ``min_delta``
- ``harness``     the eval reported non-real records (``records: … modes=[…]``), or, for an
                  objective that reads ``{records}``, no modes at all; the result is recorded
                  but is never evidence and never seeds ``best_kept``
- ``pass``        guards-only objective with every guard passing

Each result is appended to ``<state-dir>/<objective>.jsonl`` (line 0 is the
config header) and ``<objective>_dashboard.md`` is regenerated, so a resumed
loop can recover its state.

Usage:
    PYTHONPATH=src .venv/bin/python src/scripts/autoresearch_gate.py \\
        --objective thresholds --candidate candidate.yaml --records test.jsonl \\
        --baseline current.yaml --description "raise contract high to 0.985"

Exit codes: 0 keep / baseline / pass, 3 discard, 2 guard_fail,
4 harness (records are not evidence — mock/synthetic), 1 crash.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_REGISTRY = REPO_ROOT / "experiments" / "autoresearch" / "objectives.yaml"
DEFAULT_STATE_DIR = REPO_ROOT / "experiments" / "autoresearch" / "state"

_SCORE = re.compile(r"^SCORE:\s*([+-]?\d+(?:\.\d+)?)\s*$")
_FAIL = re.compile(r"^(GUARD|GATE)\s+(\S+)\s+FAIL\b")
_CI = re.compile(r"^DELTA U .*95% CI \[\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*\]")
_DELTA_U = re.compile(r"^DELTA U .*?=\s*([+-]?\d+(?:\.\d+)?)")   # lazy: not the "n=300" tail
_MODES = re.compile(r"^records:.*modes=\[([^\]]*)\]")
_EXIT = {"keep": 0, "baseline": 0, "pass": 0, "discard": 3, "guard_fail": 2, "harness": 4, "crash": 1}


def _run(cmd: str, timeout: int) -> tuple[int, str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    try:
        proc = subprocess.run(
            shlex.split(cmd), cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, proc.stdout + proc.stderr


def run_core_guards(guards: dict[str, str], timeout: int) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for name, cmd in guards.items():
        code, out = _run(cmd, timeout)
        # affected_tests exits 5 when the diff selects no test: nothing can regress.
        ok = code == 0 or (name == "affected_tests" and code == 5)
        results[name] = {"ok": ok, "exit": code, "tail": out.strip().splitlines()[-1:] if out.strip() else []}
    return results


def parse_eval(output: str) -> dict:
    score = None
    fails: list[str] = []
    ci = None
    delta = None
    modes = None
    for line in output.splitlines():
        line = line.strip()
        if m := _SCORE.match(line):
            score = float(m[1])
        elif m := _FAIL.match(line):
            fails.append(m[2])
        elif m := _CI.match(line):
            ci = (float(m[1]), float(m[2]))
        if m := _DELTA_U.match(line):
            delta = float(m[1])
        if m := _MODES.match(line):
            # hillclimb_eval prints a Python list repr: modes=['real'] or modes=['mock', 'real'].
            modes = [t.strip().strip("'\"") for t in m[1].split(",") if t.strip()]
    return {"score": score, "fails": fails, "delta_ci": ci, "delta": delta, "modes": modes}


def load_state(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def best_kept(rows: list[dict], direction: str) -> float | None:
    scores = [r["score"] for r in rows if r.get("type") == "result"
              and r.get("status") in ("baseline", "keep") and r.get("score") is not None]
    if not scores:
        return None
    return max(scores) if direction == "higher" else min(scores)


def _records_not_evidence(objective: dict, parsed: dict) -> list[str]:
    """Reasons the records behind this result are not evidence; empty when they are.

    An objective that reads no records (``scale``: its eval has no ``{records}`` placeholder)
    has no provenance to report, so a missing modes line is fine there.
    """
    modes = parsed.get("modes")
    if modes and any(m != "real" for m in modes):
        return [f"records modes {','.join(modes)} are not evidence (not real)"]
    if not modes and "{records}" in str(objective.get("eval", "")):
        # A missing or empty modes list leaves the provenance unknown, which is not "real".
        return ["records modes not reported, so provenance is unknown (not evidence)"]
    return []


def decide(objective: dict, core: dict, eval_code: int, parsed: dict, best: float | None) -> tuple[str, list[str]]:
    reasons = [f"core:{n}" for n, r in core.items() if not r["ok"]]
    if reasons:
        return "guard_fail", reasons
    direction = objective.get("direction", "higher")
    if eval_code not in (0, 2):
        return "crash", [f"eval exit {eval_code}"]
    if parsed["fails"] or eval_code == 2:
        return "guard_fail", [f"eval:{f}" for f in parsed["fails"]] or ["eval exit 2"]
    if direction == "guards_only":
        # A guards-only pass is still a result the operator keeps, so mock/synthetic records must
        # not reach it (the review objective's eval reads no records and is unaffected).
        if harness := _records_not_evidence(objective, parsed):
            return "harness", harness
        return "pass", []
    if parsed["score"] is None:
        return "crash", ["no SCORE line"]
    min_delta = float(objective.get("min_delta", 0.0))
    if objective.get("require_delta_ci"):
        ci = parsed["delta_ci"]
        if ci is None or ci[0] <= 0:
            return "guard_fail", [f"delta U CI {ci} does not exclude 0"]
    if harness := _records_not_evidence(objective, parsed):
        # Realer-than-real check first: mock/synthetic records are never evidence,
        # so they must not baseline, keep, or seed best_kept (objectives.yaml not_evidence).
        return "harness", harness
    if best is None:
        delta = parsed.get("delta")
        if objective.get("require_delta_ci") and delta is None:
            # Without a parseable point estimate min_delta cannot be applied, and an unchecked
            # first candidate would become the bar every later iteration must beat.
            return "crash", ["no DELTA U point estimate to check against min_delta"]
        if objective.get("require_delta_ci") and delta < min_delta:
            # The first candidate becomes the bar later iterations must beat, so a
            # negligible-but-significant first gain must not be accepted as a baseline.
            return "discard", [f"first candidate delta {delta:+.4f} < min_delta {min_delta:.4f}"]
        return "baseline", []
    gain = parsed["score"] - best if direction == "higher" else best - parsed["score"]
    if gain >= min_delta and gain > 0:
        return "keep", []
    return "discard", [f"gain {gain:+.4f} < min_delta {min_delta}"]


def _git_commit() -> str:
    code, out = _run("git rev-parse --short HEAD", 30)
    sha = out.strip() if code == 0 else "unknown"
    code, dirty = _run("git status --short", 30)
    return sha + ("-dirty" if code == 0 and dirty.strip() else "")


def write_dashboard(path: Path, name: str, objective: dict, rows: list[dict]) -> None:
    results = [r for r in rows if r.get("type") == "result"]
    best = best_kept(rows, objective.get("direction", "higher"))
    counts = {s: sum(1 for r in results if r["status"] == s) for s in _EXIT}
    lines = [
        f"# Autoresearch dashboard: {name}",
        "",
        f"**Metric:** {objective.get('metric', '')} ({objective.get('direction', 'higher')} is better, "
        f"min delta {objective.get('min_delta', 0)})",
        f"**Best kept:** {best if best is not None else '—'} | **Iterations:** {len(results)}",
        "",
        "| # | Commit | Score | Status | Reasons | Description |",
        "|--:|:--|--:|:--|:--|:--|",
    ]
    for r in results:
        score = "—" if r.get("score") is None else f"{r['score']:.4f}"
        lines.append(f"| {r['iteration']} | `{r['commit']}` | {score} | {r['status']} | "
                     f"{'; '.join(r.get('reasons') or [])} | {r.get('description', '')} |")
    lines += ["", " | ".join(f"**{k}:** {v}" for k, v in counts.items() if v), ""]
    path.write_text("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--objective", required=True)
    ap.add_argument("--description", required=True, help="what this iteration changed")
    ap.add_argument("--candidate", default="")
    ap.add_argument("--records", default="")
    ap.add_argument("--baseline", default="")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    ap.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    ap.add_argument("--timeout", type=int, default=3600, help="per-command timeout (s)")
    args = ap.parse_args(argv)

    registry = yaml.safe_load(Path(args.registry).read_text())
    objective = (registry.get("objectives") or {}).get(args.objective)
    if objective is None:
        print(f"unknown objective {args.objective!r}; known: {sorted(registry.get('objectives') or {})}",
              file=sys.stderr)
        return 1
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / f"{args.objective}.jsonl"
    rows = load_state(state_path)
    if not rows:
        header = {"type": "config", "objective": args.objective, "metric": objective.get("metric"),
                  "direction": objective.get("direction", "higher"), "min_delta": objective.get("min_delta", 0),
                  "guards": objective.get("guards", []), "core_guards": sorted(registry.get("core_guards") or {}),
                  "eval": objective.get("eval")}
        rows.append(header)
        state_path.write_text(json.dumps(header) + "\n")

    print(f"[core guards] {', '.join(registry.get('core_guards') or {})}")
    core = run_core_guards(registry.get("core_guards") or {}, args.timeout)
    for name, r in core.items():
        print(f"CORE {name} {'PASS' if r['ok'] else 'FAIL'} (exit {r['exit']})")

    eval_code, eval_out = 0, ""
    if all(r["ok"] for r in core.values()):
        # Format only when the eval will run: a failed core guard is the result whatever the command is.
        try:
            eval_cmd = objective["eval"].format(
                candidate=shlex.quote(args.candidate),
                records=shlex.quote(args.records),
                baseline=shlex.quote(args.baseline),
            )
        except (KeyError, IndexError, ValueError) as exc:
            eval_code, eval_out = 1, f"eval command could not be formatted: {exc!r}"
            print(f"[eval] {eval_out}")
        else:
            eval_code, eval_out = _run(eval_cmd, args.timeout)
            print(f"[eval] {eval_cmd}\n" + "\n".join(eval_out.strip().splitlines()[-15:]))
    parsed = parse_eval(eval_out)
    best = best_kept(rows, objective.get("direction", "higher"))
    status, reasons = decide(objective, core, eval_code, parsed, best)

    iteration = sum(1 for r in rows if r.get("type") == "result") + 1
    direction = objective.get("direction", "higher")
    if parsed["score"] is None or best is None:
        delta = None
    else:
        # Direction-aware: "keep" means the candidate improved in the objective's
        # direction, so the persisted delta must agree with what decide computed.
        delta = round(parsed["score"] - best if direction == "higher" else best - parsed["score"], 6)
    result = {
        "type": "result", "iteration": iteration, "commit": _git_commit(), "score": parsed["score"],
        "best_before": best, "delta": delta, "delta_vs_policy": parsed.get("delta"),
        "status": status, "reasons": reasons,
        "eval_fails": parsed["fails"], "delta_ci": parsed["delta_ci"],
        "modes": parsed.get("modes"), "records": args.records, "baseline": args.baseline,
        "core": {n: r["ok"] for n, r in core.items()}, "candidate": args.candidate,
        "description": args.description, "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with state_path.open("a") as fh:
        fh.write(json.dumps(result) + "\n")
    rows.append(result)
    write_dashboard(state_dir / f"{args.objective}_dashboard.md", args.objective, objective, rows)
    print(f"DECISION {status.upper()} iteration={iteration} score={parsed['score']} best_before={best} "
          f"{'; '.join(reasons)}")
    return _EXIT[status]


if __name__ == "__main__":
    raise SystemExit(main())
