"""Relations clerk mode toggle (HUB-052) — live vs pilot, simply.

The relations judge (``relations.llm``) is the live/pilot knob: OFF keeps the
research clerk deterministic-only (pilot posture — zero LLM spend), ON
activates the LLM judgment pass over the ambiguous band. This module makes
the flip a one-command operation instead of a manual taxonomy edit + watcher
restart:

    python -m pipeline.relations_mode status
    python -m pipeline.relations_mode pilot
    python -m pipeline.relations_mode live [--model deepseek/deepseek-v4-flash]
    python -m pipeline.relations_mode live --restart-watcher

``status`` prints the effective mode plus every knob that can block or shape
it (taxonomy values, env kill-switches, the free-only guardrail, ledger
health). ``pilot``/``live`` edit taxonomy.yaml SURGICALLY (comments and every
other line preserved byte-for-byte), clear the in-process config caches so
the current process honors the flip immediately, and remove a stale
``MAILROOM_RELATIONS_LLM`` kill-switch from ``.env`` that would contradict
the requested mode. The standalone watcher needs ``--restart-watcher`` (the
graceful relaunch sequence) — its caches live in another process. The API
mirrors the same apply via ``GET/POST /api/relations/mode``: the POST is the
"even smoother" path — the embedded watcher shares the API process, whose
caches the apply clears, so the flip is effective with NO restart.

The global ``MAILROOM_LLM_FREE_ONLY`` pilot gate is NEVER flipped here — it
is a pipeline-wide .env decision; ``status`` reports it so the operator sees
the full posture, and a paid judge model under the guardrail is refused with
an actionable message.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_SRC_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_ROOT = _SRC_ROOT.parent
TAXONOMY_PATH = _SRC_ROOT / "config" / "taxonomy.yaml"
ENV_PATH = _PACKAGE_ROOT / ".env"

_OFF_VALUES = ("0", "false", "no", "off", "")


def _env_off(name: str, default: str = "1") -> bool:
    """Env kill-switch semantics shared with the relations gates."""
    return str(os.environ.get(name, default)).strip().lower() in _OFF_VALUES


def _taxonomy_config() -> dict:
    """Read the taxonomy this module edits (the ``TAXONOMY_PATH`` seam), with
    the pipeline loader as a fallback — the readout must always see exactly
    what the toggle wrote."""
    try:
        import yaml

        return yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        try:
            from pipeline.config import load_config

            return load_config() or {}
        except Exception:
            return {}


# ---------------------------------------------------------------------------
# Readout
# ---------------------------------------------------------------------------


def _model_is_free(model: str) -> bool:
    """Free = taxonomy ``cost_models`` both 0.0 OR OpenRouter ``:free``
    suffix — the same predicate ``llm.client.assert_free_model`` uses."""
    cost = (_taxonomy_config().get("cost_models") or {}).get(model) or {}
    try:
        free = float(cost.get("input_per_million", -1)) == 0.0 and float(
            cost.get("output_per_million", -1)
        ) == 0.0
    except (TypeError, ValueError):
        free = False
    return free or str(model).endswith(":free")


def mode_status() -> dict:
    """Effective relations-clerk posture — every knob, plus ledger health.
    Never raises (best-effort reads; failures render as None/[])."""
    from llm.client import free_only_enabled

    cfg = dict((_taxonomy_config().get("relations") or {}))
    try:
        from pipeline.relations import DEFAULTS

        merged = {**DEFAULTS, **cfg}
    except Exception:
        merged = cfg
    enabled = bool(cfg.get("enabled", True)) and not _env_off("MAILROOM_RELATIONS")
    env_blocked = _env_off("MAILROOM_RELATIONS_LLM")
    llm_on = bool(cfg.get("llm", False))
    effective_llm = enabled and llm_on and not env_blocked
    if not enabled:
        mode = "off"
    elif effective_llm:
        mode = "live"
    else:
        mode = "pilot"

    model = None
    try:
        model = str(
            ((_taxonomy_config().get("agents") or {}).get("relations") or {}).get("model") or ""
        )
    except Exception:
        pass

    ledger = None
    last_sweep = None
    edges = None
    try:
        from pipeline.relations import verify_ledger

        ok, count = verify_ledger()
        ledger = {"ok": bool(ok), "entries": count}
    except Exception:
        pass
    try:
        import asyncio

        from storage import relations as R

        last_sweep = asyncio.run(R.get_scan_state("last_sweep_at"))
        edges = asyncio.run(R.count_edges())
    except Exception:
        pass

    return {
        "mode": mode,
        "llm": llm_on,
        "llm_effective": effective_llm,
        "llm_env_blocked": env_blocked,
        "enabled": enabled,
        "context_injection": bool(cfg.get("context_injection", True)),
        "context_injection_effective": bool(cfg.get("context_injection", True))
        and not _env_off("MAILROOM_RELATIONS_CONTEXT"),
        "graphs": bool(cfg.get("graphs", True)),
        "model": model,
        "model_is_free": _model_is_free(model) if model else None,
        "free_only_guardrail": free_only_enabled(),
        "kill_switches": {
            "MAILROOM_RELATIONS": str(os.environ.get("MAILROOM_RELATIONS", "1")),
            "MAILROOM_RELATIONS_LLM": str(os.environ.get("MAILROOM_RELATIONS_LLM", "1")),
            "MAILROOM_RELATIONS_CONTEXT": str(os.environ.get("MAILROOM_RELATIONS_CONTEXT", "1")),
            "MAILROOM_RELATIONS_EMBEDDINGS": str(os.environ.get("MAILROOM_RELATIONS_EMBEDDINGS", "1")),
        },
        "embeddings_enabled": not _env_off("MAILROOM_RELATIONS_EMBEDDINGS"),
        "similarity_threshold": merged.get("similarity_threshold"),
        "keyword_jaccard_threshold": merged.get("keyword_jaccard_threshold"),
        "llm_confidence_gate": merged.get("llm_confidence_gate"),
        "top_k_llm_candidates": merged.get("top_k_llm_candidates"),
        "last_sweep_at": last_sweep,
        "edges": edges,
        "ledger": ledger,
    }


# ---------------------------------------------------------------------------
# Surgical taxonomy + .env editing (comments and all other lines preserved)
# ---------------------------------------------------------------------------


def _edit_taxonomy(*, llm: bool | None = None, model: str | None = None) -> dict:
    """Flip ``relations.llm`` and/or ``agents.relations.model`` in
    taxonomy.yaml in place — byte-preserving everywhere else. Returns
    ``{"llm": bool, "model": bool}`` for what was actually changed."""
    path = TAXONOMY_PATH
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    top_section: str | None = None
    agent_sub: str | None = None
    changed = {"llm": False, "model": False}
    llm_seen = False
    model_seen = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            top_section = stripped.split(":", 1)[0].strip()
            agent_sub = None
            continue
        if top_section == "agents" and indent == 2:
            agent_sub = stripped.split(":", 1)[0].strip()
            continue
        if top_section == "relations" and indent == 2 and llm is not None:
            if re.fullmatch(r"llm: (true|false)", stripped):
                lines[i] = f"  llm: {'true' if llm else 'false'}"
                changed["llm"] = True
                llm_seen = True
        if top_section == "agents" and agent_sub == "relations" and indent == 4 and model is not None:
            if re.fullmatch(r"model: .*", stripped):
                lines[i] = f"    model: {model}"
                changed["model"] = True
                model_seen = True

    # Insert missing keys right after their section header (never invented
    # elsewhere): ``llm`` after ``relations:``, ``model`` after the agent
    # sub-block's first line.
    if llm is not None and not llm_seen:
        for i, line in enumerate(lines):
            if re.fullmatch(r"relations:", line.strip()):
                lines.insert(i + 1, f"  llm: {'true' if llm else 'false'}")
                changed["llm"] = True
                break
    if model is not None and not model_seen:
        for i, line in enumerate(lines):
            if re.fullmatch(r"  relations:", line.strip()) and i + 1 < len(lines):
                # only under the agents: section — the top-level relations:
                # block is 0-indent, so 2-indent means the agents sub-block
                lines.insert(i + 1, "    model: " + str(model))
                changed["model"] = True
                break

    if any(changed.values()):
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return changed


def _drop_env_kill_switch() -> bool:
    """Remove a stale ``MAILROOM_RELATIONS_LLM`` kill line from ``.env`` so
    it cannot contradict the requested mode. Returns True when removed."""
    if not ENV_PATH.exists():
        return False
    text = ENV_PATH.read_text(encoding="utf-8")
    kept = [
        line
        for line in text.split("\n")
        if not re.match(r"^\s*MAILROOM_RELATIONS_LLM\s*=", line)
    ]
    if len(kept) == len(text.split("\n")):
        return False
    fd, tmp = tempfile.mkstemp(dir=str(ENV_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept))
        os.replace(tmp, ENV_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


def set_mode(mode: str, model: str | None = None) -> dict:
    """Apply the requested mode to the persistent knobs (taxonomy + .env)
    and clear the in-process caches so the CURRENT process honors it at
    once. Raises ValueError for invalid modes / models.

    ``mode``: "pilot" (deterministic-only) or "live" (LLM judgment on).
    ``model``: optional judge model — must be a taxonomy ``cost_models``
    entry or carry the ``:free`` suffix; a paid model under the
    ``MAILROOM_LLM_FREE_ONLY`` guardrail is refused with an actionable
    message (the guardrail is a pipeline-wide .env decision, never flipped
    here).
    """
    mode = (mode or "").strip().lower()
    if mode not in ("pilot", "live"):
        raise ValueError(f"mode must be 'pilot' or 'live', got {mode!r}")
    llm = mode == "live"

    if model is not None:
        model = str(model).strip()
        if not model:
            model = None
    if model is not None:
        if not _model_is_free(model):
            known = model in (_taxonomy_config().get("cost_models") or {})
            if not known:
                raise ValueError(
                    f"unknown model {model!r} — register it under cost_models "
                    "in taxonomy.yaml first (or use an OpenRouter ':free' model)"
                )
        from llm.client import free_only_enabled

        if free_only_enabled() and not _model_is_free(model):
            raise ValueError(
                f"model {model!r} is paid but MAILROOM_LLM_FREE_ONLY is on — "
                "the free-only pilot gate blocks paid agents. Unset "
                "MAILROOM_LLM_FREE_ONLY in .env for full production, or pick "
                "a free model."
            )

    env_switch_removed = False
    if mode == "live" and _env_off("MAILROOM_RELATIONS_LLM"):
        # A stale kill-switch contradicts the requested mode — drop it.
        env_switch_removed = _drop_env_kill_switch()

    changed = _edit_taxonomy(llm=llm, model=model)

    try:
        from pipeline.config import clear_config_cache

        clear_config_cache()
    except Exception:
        logger.debug("relations_mode_cache_clear_failed")

    return {
        "mode": mode,
        "llm": llm,
        "model": model,
        "taxonomy_changed": changed,
        "env_switch_removed": env_switch_removed,
        "note": (
            "applied in-process — the current process honors it immediately; "
            "standalone watchers read the new taxonomy on their next restart "
            "(python -m pipeline.relations_mode <mode> --restart-watcher)"
            if not env_switch_removed
            else (
                "applied in-process; a stale MAILROOM_RELATIONS_LLM kill-switch "
                "was removed from .env; standalone watchers read the new "
                "taxonomy on their next restart"
            )
        ),
    }


# ---------------------------------------------------------------------------
# Standalone watcher restart (graceful — the watchdog first)
# ---------------------------------------------------------------------------


def _running_pids(module: str) -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"python -m {module}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return [int(p) for p in out.stdout.split() if p.strip()]
    except Exception:
        return []


def _tail_matches(log_path: Path, pattern: str, timeout_s: float = 25.0) -> bool:
    import time

    deadline = time.time() + timeout_s
    last = 0
    while time.time() < deadline:
        try:
            with open(log_path, "rb") as fh:
                fh.seek(max(0, last))
                chunk = fh.read()
                last = fh.tell()
            if re.search(pattern, chunk.decode("utf-8", "replace")):
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def restart_watcher() -> dict:
    """Gracefully restart the standalone watcher + watchdog (HUB-052):
    watchdog first (so a dead watcher never triggers a false 🔴 down email),
    watcher second, then relaunch both exactly as the operational pattern
    does (same log files, ``PYTHONPATH=src``, detached session)."""
    data_dir = _PACKAGE_ROOT / "data"
    watcher_log = data_dir / "watcher.out"
    watchdog_log = data_dir / "watchdog.out"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_PACKAGE_ROOT / "src")

    killed = {"watchdog": _running_pids("pipeline.watchdog"), "watcher": _running_pids("pipeline.watcher")}
    for pid in killed["watchdog"]:
        try:
            os.kill(pid, 15)
        except Exception:
            pass
    for pid in killed["watcher"]:
        try:
            os.kill(pid, 15)
        except Exception:
            pass

    wl = open(watcher_log, "ab")
    subprocess.Popen(
        [sys.executable, "-m", "pipeline.watcher"],
        cwd=str(_PACKAGE_ROOT),
        env=env,
        stdout=wl,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    started = _tail_matches(watcher_log, r"watcher_starting.*worker_id=([0-9a-f]{8})")
    worker = None
    if started:
        m = re.search(r"watcher_starting.*worker_id=([0-9a-f]{8})", watcher_log.read_text(encoding="utf-8", errors="replace")[-20000:])
        worker = m.group(1) if m else None
    if not started:
        wl.close()
        return {"ok": False, "killed": killed, "note": "watcher did not confirm startup in 25s — check data/watcher.out"}

    wd = open(watchdog_log, "ab")
    subprocess.Popen(
        [sys.executable, "-m", "pipeline.watchdog"],
        cwd=str(_PACKAGE_ROOT),
        env=env,
        stdout=wd,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    wl.close()
    wd.close()
    sweeper = _tail_matches(watcher_log, r"relations_sweeper_started")
    return {
        "ok": True,
        "worker_id": worker,
        "killed": killed,
        "sweeper_started": sweeper,
        "note": "watchdog stopped first (no false 🔴), relaunched last",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_status(status: dict) -> None:
    mode = status["mode"]
    guard = "ON (free-only pilot gate)" if status["free_only_guardrail"] else "off (full production)"
    print(f"relations clerk mode : {mode.upper()}   ({'LLM judgment pass on' if status['llm_effective'] else 'deterministic-only'})")
    print(f"  relations.enabled  : {status['enabled']}")
    print(f"  relations.llm      : {status['llm']}  (effective: {status['llm_effective']})")
    print(f"  judge model        : {status['model'] or '?'}  ({'free' if status['model_is_free'] else 'paid'})")
    print(f"  free-only guardrail: {guard}")
    print(f"  context injection  : {status['context_injection_effective']}   graphs: {status['graphs']}")
    print(f"  embeddings signal  : {status['embeddings_enabled']}")
    print(f"  thresholds         : cosine>={status['similarity_threshold']} jaccard>={status['keyword_jaccard_threshold']} "
          f"llm_gate>={status['llm_confidence_gate']} top_k={status['top_k_llm_candidates']}")
    if status["llm_env_blocked"]:
        print("  !! MAILROOM_RELATIONS_LLM kill-switch BLOCKS the LLM pass —")
        print("     remove it from .env (the toggle does this for you)")
    if mode != "live":
        print("  hint: python -m pipeline.relations_mode live [--model <name>] --restart-watcher")
    ledger = status.get("ledger")
    if ledger is not None:
        print(f"  relations ledger    : {'OK' if ledger['ok'] else 'BROKEN'} ({ledger['entries']} entries)"
              f"{'' if ledger['ok'] else ' — investigate immediately'}")
    if status.get("edges") is not None:
        print(f"  relation edges      : {status['edges']}   last sweep: {status.get('last_sweep_at') or 'never'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.relations_mode",
        description="Relations clerk mode toggle (HUB-052) — live vs pilot, simply.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="print the effective mode + every knob")
    for name in ("pilot", "live"):
        p = sub.add_parser(name, help="deterministic-only" if name == "pilot" else "LLM judgment pass on")
        p.add_argument("--model", help="judge model (taxonomy cost_models entry or ':free' suffix)")
        p.add_argument("--restart-watcher", action="store_true", help="gracefully restart the standalone watcher")
    args = parser.parse_args(argv)

    if args.command == "status":
        _print_status(mode_status())
        return 0

    try:
        result = set_mode(args.command, getattr(args, "model", None))
    except ValueError as exc:
        print(f"refused: {exc}")
        return 1

    print(f"relations clerk mode set to {result['mode'].upper()}"
          f" (taxonomy llm: {'true' if result['llm'] else 'false'}"
          + (f", judge model: {result['model']}" if result["model"] else "")
          + ")")
    if result.get("env_switch_removed"):
        print("removed the stale MAILROOM_RELATIONS_LLM kill-switch from .env")
    print(result["note"])

    if getattr(args, "restart_watcher", False):
        print("restarting the standalone watcher + watchdog …")
        rc = restart_watcher()
        if rc.get("ok"):
            print(f"watcher up — worker {rc.get('worker_id')}, "
                  f"sweeper {'started' if rc.get('sweeper_started') else 'not yet confirmed'}")
        else:
            print(rc.get("note", "watcher restart failed"))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())