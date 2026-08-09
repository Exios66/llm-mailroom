"""Real (non-mock) pilot runs must only process actual committed legal
documents — the Atticus/CUAD contract & agreement PDFs plus the other legal-DB
samples (LegalBench MAUD, Pile of Law). Repo-written synthetic .txt samples
(render-to-PDF stand-ins under examples/sources/) are mock-only and must be
blocked from every --real run so no real LLM/eval tokens or live traces are
spent on fake documents.
"""

import csv
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "examples" / "samples" / "manifest.csv"


def _rows():
    with MANIFEST.open() as fh:
        return list(csv.DictReader(fh))


def test_manifest_split_real_vs_synthetic():
    from scripts.prepare_samples import is_real_sample

    rows = _rows()
    real = [r for r in rows if is_real_sample(r)]
    synthetic = [r for r in rows if not is_real_sample(r)]

    # All 9 committed CUAD/Atticus PDFs + 6 LegalBench + 6 Pile of Law are real.
    assert {r["id"] for r in real} == {
        "contract_01", "contract_02", "contract_03",
        "atticus_01", "atticus_02", "atticus_03", "atticus_04", "atticus_05", "atticus_06",
        "legalbench_01", "legalbench_02", "legalbench_03", "legalbench_04",
        "legalbench_05", "legalbench_06",
        "pileoflaw_01", "pileoflaw_02", "pileoflaw_03", "pileoflaw_04",
        "pileoflaw_05", "pileoflaw_06",
    }, [r["id"] for r in real]
    # The 9 repo-written synthetic samples are mock-only.
    assert {r["id"] for r in synthetic} == {
        "compliance_01", "compliance_02",
        "corporate_01", "corporate_02",
        "correspondence_01", "correspondence_02",
        "due_diligence_01", "due_diligence_02",
        "ambiguous_01",
    }, [r["id"] for r in synthetic]


def test_filter_real_samples_keeps_all_for_mock():
    from scripts.run_pilot import filter_real_samples

    rows = _rows()
    assert filter_real_samples(rows, mock_mode=True) == rows
    assert len(filter_real_samples(rows, mock_mode=True)) == 30


def test_filter_real_samples_blocks_synthetic_for_real():
    from scripts.run_pilot import filter_real_samples

    filtered = filter_real_samples(_rows(), mock_mode=False)
    ids = {r["id"] for r in filtered}
    assert "atticus_01" in ids  # real CUAD/Atticus PDF kept
    assert "contract_01" in ids  # real CUAD PDF kept
    assert "legalbench_01" in ids  # external LegalBench kept
    assert "pileoflaw_01" in ids  # external Pile of Law kept
    assert "due_diligence_01" not in ids  # synthetic blocked
    assert "compliance_01" not in ids
    assert "ambiguous_01" not in ids


def _env_no_dotenv() -> dict:
    env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["OBSERVABILITY_PROVIDER"] = "none"
    env["OPENROUTER_API_KEY"] = "sk-or-v1-real-test"
    return env


def test_real_run_refuses_synthetic_only_selection():
    # A --real run that only selects synthetic samples must refuse to start
    # before any document is processed — never spend real LLM tokens on fake
    # documents. The refusal happens before any pipeline work (no LLM calls).
    proc = subprocess.run(
        [sys.executable, "scripts/run_pilot.py", "--real", "--include", "due_diligence"],
        capture_output=True, text=True, env=_env_no_dotenv(), cwd=REPO_ROOT,
    )
    assert proc.returncode != 0
    assert "No real samples selected" in proc.stderr
    assert "mock-only" in proc.stderr
