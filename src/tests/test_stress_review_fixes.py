"""Regression tests for the stress harness review fixes (src/scripts/stress_test.py)."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "src/scripts/stress_test.py"


@pytest.fixture(scope="module")
def st():
    spec = importlib.util.spec_from_file_location("stress_test_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _args(st):
    return SimpleNamespace(target_dpm=600.0)


def _data(scenarios, do_integrity):
    return {"scenarios": scenarios, "do_integrity": do_integrity}


def test_docstring_names_five_classes(st):
    assert len(st.DOC_CLASSES) == 5
    assert "(5 classes," in st.__doc__


def test_worker_env_pins_database_url_to_scratch_sqlite(st, monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db.invalid/prod")
    env = st._worker_env(str(tmp_path), "CRITICAL")
    assert env["DATABASE_URL"] == f"sqlite+aiosqlite:///{tmp_path / 'mailroom.db'}"


def test_blank_documents_have_distinct_bytes(st):
    corpus = st.make_corpus(200, 0)
    blanks = [text for _n, text, meta in corpus if meta["kind"] == "blank"]
    assert len(blanks) >= 2
    assert all(not t.strip() for t in blanks)
    assert len(set(blanks)) == len(blanks)


def test_every_sorter_window_carries_the_marker(st):
    """The mock keys faults on the marker; a window without it would share the `anon` counter."""
    import yaml
    from agents.intake import sliding_windows

    cfg = yaml.safe_load((ROOT / "src/config/taxonomy.yaml").read_text())
    budget = cfg["agents"]["sorter"]["max_input_chars"] - 600  # sorter._PRIOR_ALLOWANCE
    longs = [t for _n, t, m in st.make_corpus(100, 0) if m["kind"] == "long"]
    assert longs
    for text in longs:
        windows = sliding_windows(text, budget, max(1, int(budget * 0.15)))
        assert len(windows) > 1
        assert all(st.MARKER_RE.search(w) for w, _base in windows)


def test_matter_index_is_stable_across_processes(st):
    code = (
        "import importlib.util,sys;"
        f"s=importlib.util.spec_from_file_location('m',{str(SCRIPT)!r});m=importlib.util.module_from_spec(s);"
        "sys.modules['m']=m;s.loader.exec_module(m);"
        "print(m._stable_index('s0d00001_contract_ok.txt',4))"
    )
    outs = {
        subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": seed}
        ).stdout.strip()
        for seed in ("1", "2", "3", "4")
    }
    assert outs == {str(st._stable_index("s0d00001_contract_ok.txt", 4))}


@pytest.mark.parametrize("bad", ["0", "-4", "8,0", "x", ""])
def test_db_threads_rejects_non_positive(st, bad, capsys):
    with pytest.raises(SystemExit) as exc:
        st.build_parser().parse_args(["--db-threads", bad])
    assert exc.value.code == 2
    assert st.build_parser().parse_args(["--db-threads", "2,8"]).db_threads == [2, 8]
    assert st.build_parser().parse_args([]).db_threads == [32, 128]


def test_workdir_scratch_root_is_absolute(st, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_run(cmd, env, **kw):
        seen["base"] = env["MAILROOM_BASE_DIR"]
        seen["db"] = env["DATABASE_URL"]
        raise st.subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(st.subprocess, "run", fake_run)
    args = SimpleNamespace(workdir="rel-scratch", log_level="CRITICAL", cap=4, timeout=1, keep=False)
    (tmp_path / "rel-scratch").mkdir()
    st.run_worker("throughput", {}, args, "t")
    assert Path(seen["base"]).is_absolute()
    assert seen["db"].startswith("sqlite+aiosqlite:////")


def test_integrity_failures_gate_without_the_integrity_scenario(st):
    thr = {"ok": True, "metrics": {"docs_per_min": 600.0, "p50_s": 0, "p95_s": 0, "max_s": 0, "elapsed_s": 1,
                                   "raised": 0, "returned_stage_counts": {}, "llm_calls": 0},
           "integrity": {"lost_docs": 2, **{k: 0 for k in (
               "duplicate_catalog_rows", "duplicate_sha_rows", "duplicate_chains",
               "orphan_processing_rows", "broken_chains", "lost_audit_events")}}}
    sc = st.score(_args(st), _data({"throughput": {"1": thr}}, do_integrity=False))
    assert sc["gates"]["lost_docs"]["pass"] is False
    assert sc["SCORE"] == 0.0


def test_integrity_only_run_with_no_measurement_fails(st):
    sc = st.score(_args(st), _data({}, do_integrity=True))
    assert sc["gates"]["integrity_measured"]["pass"] is False
    assert sc["SCORE"] == 0.0


def test_missed_crash_injection_fails_the_scenario(st):
    inter = {k: 0 for k in ("lost_docs", "duplicate_catalog_rows", "duplicate_sha_rows", "duplicate_chains",
                            "orphan_processing_rows", "broken_chains", "lost_audit_events")}
    def cr(all_crashed):
        return {"ok": True, "integrity": inter,
                "metrics": {"all_crashed": all_crashed, "duplicate_manifests": 0, "by_point": {}}}
    assert st.score(_args(st), _data({"crash_recovery": cr(True)}, False))["gates"]["crash_injected"]["pass"] is True
    sc = st.score(_args(st), _data({"crash_recovery": cr(False)}, False))
    assert sc["gates"]["crash_injected"]["pass"] is False
    assert sc["SCORE"] == 0.0


def _run(*argv, tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "MAILROOM_BASE_DIR": str(tmp_path)}
    return subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True, env=env, timeout=240)


def test_stale_claim_scenario_ages_live_claims_and_exercises_exclusion(tmp_path):
    out = _run("--scenarios", "stale_claim", "--stale-files", "2", tmp_path=tmp_path)
    assert "GATE inflight_hijacked PASS" in out.stdout, out.stdout[-1500:] + out.stderr[-1500:]
    assert "GATE orphans_unrecovered PASS" in out.stdout
    assert "stale_claim.inflight_aged_stale" in out.stdout
    line = next(l for l in out.stdout.splitlines() if l.startswith("stale_claim.inflight_aged_stale"))
    assert line.split()[-1] == "2"


_CHAIN_PROBE = r"""
import asyncio, importlib.util, json, sys
spec = importlib.util.spec_from_file_location("st", sys.argv[1]); st = importlib.util.module_from_spec(spec)
sys.modules["st"] = st; spec.loader.exec_module(st)
from pipeline import bins
from schemas.audit import build_audit_entry
from storage.audit_log import write_audit_entry
from storage.catalog import write_document_record
from storage.db import ensure_schema
ensure_schema(); st._ensure_bins()
NAME = "probe.txt"
async def go():
    # attempt 1: catalog row carries the filename; attempt 2: catalog row lost its filename
    for doc_id, fname in (("doc-a", NAME), ("doc-b", "")):
        await write_document_record({"doc_id": doc_id, "matter_id": "M", "original_filename": fname, "stage": "archived"})
        await write_audit_entry(build_audit_entry(doc_id, "M", "ingested", "intake", {}))
        (bins.manifests_dir() / f"{doc_id}.json").write_text(json.dumps({"doc_id": doc_id, "original_filename": NAME, "stage": "archived"}))
asyncio.run(go())
print("DUP_CHAINS", st.check_integrity({NAME: {}})["duplicate_chains"])
(bins.manifests_dir() / "bad.json").write_text("{not json")
try:
    st.check_integrity({NAME: {}})
except Exception as exc:
    print("RAISED", type(exc).__name__)
"""


def test_duplicate_chain_found_via_manifest_and_bad_manifest_raises(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "MAILROOM_BASE_DIR": str(tmp_path)}
    env.pop("DATABASE_URL", None)
    out = subprocess.run([sys.executable, "-c", _CHAIN_PROBE, str(SCRIPT)], capture_output=True, text=True, env=env,
                         timeout=120, cwd=str(ROOT))
    assert "DUP_CHAINS 1" in out.stdout, out.stdout[-800:] + out.stderr[-1500:]
    assert "RAISED JSONDecodeError" in out.stdout


def test_crash_recovery_injects_a_crash_at_every_point(tmp_path):
    """The after_extract point used to patch judge_verify_node, which confident documents skip."""
    out = _run("--scenarios", "crash_recovery", "--crash-docs-per-point", "1", tmp_path=tmp_path)
    assert "GATE crash_injected PASS" in out.stdout, out.stdout[-1500:] + out.stderr[-1500:]
    assert "GATE scenario_error PASS" in out.stdout
