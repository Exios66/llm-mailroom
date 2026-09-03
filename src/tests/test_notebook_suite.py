"""Guard suite for the notebooks/ walkthroughs (KANBAN-095, mailroom #15).

Enforces the four duties promised in ``notebooks/PLAN.md`` (§Guards):

1. every planned notebook exists with title + honesty-label cells, and the
   shared bench module imports network-free;
2. headless re-execution of every notebook via ``nbclient`` from BOTH
   PLAN cwds (repo root and notebooks/) regenerates cleanly — cell error
   status must match the committed stored outputs (zero errors both times);
3. ``pipeline_lab`` unit pins: band math matches the ``graph.routing``
   literals, sandbox teardown restores env, step-log capture reports node
   names in graph order;
4. no notebook cell text carries an API-key pattern or a network call at
   exec time (AST scan of the lab module + source grep of the notebooks,
   the marker-gated opt-in cells of notebook 08 excepted).

Network-free by construction: the only network-shaped text lives behind the
``NB-OPT-IN-NETWORK`` marker in 08 (Langfuse) and 11 (Hugging Face Dataset
Viewer) and is skipped unless keys / ``MAILROOM_HF_LIVE`` are present.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

import pytest

from notebooks import pipeline_lab as lab

REPO_ROOT = Path(__file__).resolve().parents[2]
NB_DIR = REPO_ROOT / "notebooks"

# The suite roster from notebooks/PLAN.md (§The suite). Titles are matched
# as substrings of each notebook's H1; honesty cells must self-declare.
PLANNED = {
    "00_pipeline_anatomy.ipynb": "Pipeline anatomy",
    "01_happy_path_run.ipynb": "A happy-path run through the agents",
    "02_routing_dynamics.ipynb": "Routing dynamics",
    "03_review_lanes.ipynb": "Review lanes",
    "04_human_in_the_loop.ipynb": "Human-in-the-loop",
    "05_failure_recovery.ipynb": "Failure recovery",
    "06_outputs_and_audit.ipynb": "Outputs & audit",
    "07_multi_document_matters.ipynb": "Multi-document matters",
    "08_observability_traces.ipynb": "Observability traces",
    "09_all_specialists.ipynb": "All specialists",
    "10_edge_cases.ipynb": "Edge cases",
    "11_huggingface_corpora.ipynb": "Hugging Face corpora",
    "12_legalbench.ipynb": "LegalBench",
    "13_vision_ingestion.ipynb": "Vision ingestion",
}

HONESTY_MARKERS = ("Honesty label", "honest", "OFFLINE")

OPT_IN_MARKER = "NB-OPT-IN-NETWORK"

KEY_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),           # OpenAI-style keys
    re.compile(r"pk_live_[A-Za-z0-9]{10,}"),      # Stripe-style live keys
    re.compile(r"AKIA[0-9A-Z]{16}"),              # AWS access key ids
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
]

NETWORK_MODULES = {"requests", "httpx", "urllib.request", "urllib"}


# ---------------------------------------------------------------------------
# Duty 1 — existence, shape, importability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fname,title", sorted(PLANNED.items()))
def test_notebook_exists_with_title_and_honesty_cells(fname: str, title: str) -> None:
    nb = json.loads((NB_DIR / fname).read_text())
    assert nb["nbformat"] == 4, f"{fname}: not nbformat 4"
    md_text = "\n".join(
        "".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "markdown"
    )
    assert title.lower() in md_text.lower(), f"{fname}: expected title '{title}'"
    assert any(m in md_text for m in HONESTY_MARKERS), (
        f"{fname}: honesty label missing"
    )


def test_step_capture_reports_nodes_in_graph_order() -> None:
    with lab.lab_sandbox() as env:
        r = lab.run_document(
            env,
            lab.DOC_CONTRACT,
            classification=lab.CLASSIFY_CONTRACT_HIGH,
            extraction=lab.EXTRACT_HIGH,
            filename="guard_order.txt",
        )
        nodes = [s["node"] for s in r["steps"]]
        assert nodes[:2] == ["intake-document", "classify-document"], nodes
        assert all(nodes), "empty node names in capture"


# ---------------------------------------------------------------------------
# Duty 3 — pipeline_lab unit pins
# ---------------------------------------------------------------------------


def test_band_report_matches_routing_literals() -> None:
    from graph.routing import get_confidence_thresholds

    thresholds = get_confidence_thresholds()
    report = lab.band_report()
    for band in ("high", "low"):
        assert report[band] == thresholds.get(band), (
            f"{band} threshold drifted between lab and routing config"
        )
    # The judge gate is [low, judge_band_high): pin both ends of the band.
    assert report["judge_band_high"] == float(
        thresholds.get("judge_band_high", 0.85)
    )


def test_sandbox_restores_environment() -> None:
    before = dict(os.environ)
    with lab.open_sandbox():
        pass
    assert os.environ == before, "sandbox leaked environment changes"


# ---------------------------------------------------------------------------
# Duty 4 — no secrets, no exec-time network
# ---------------------------------------------------------------------------


def _opt_in_cell_sources(nb: dict) -> set[str]:
    """Source text of code cells that sit AFTER the opt-in marker."""
    src: set[str] = set()
    seen_marker = False
    for c in nb["cells"]:
        text = "".join(c.get("source", []))
        if OPT_IN_MARKER in text:
            seen_marker = True
        elif seen_marker and c["cell_type"] == "code":
            src.add(text)
    return src


@pytest.mark.parametrize("fname", sorted(PLANNED))
def test_no_key_patterns_in_notebook_sources(fname: str) -> None:
    nb = json.loads((NB_DIR / fname).read_text())
    text = "\n".join("".join(c["source"]) for c in nb["cells"])
    for pat in KEY_PATTERNS:
        assert not pat.search(text), f"{fname}: credential-like literal found"


@pytest.mark.parametrize("fname", sorted(PLANNED))
def test_notebooks_make_no_exec_time_network_calls(fname: str) -> None:
    nb = json.loads((NB_DIR / fname).read_text())
    allowed_tail = _opt_in_cell_sources(nb)
    for c in nb["cells"]:
        if c["cell_type"] != "code":
            continue
        src = "".join(c["source"])
        if src in allowed_tail:
            continue  # marker-gated opt-in cell (notebooks 08, 11)
        tree = ast.parse(src, mode="exec")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {a.name.split(".")[0] for a in node.names}
                leaks = NETWORK_MODULES & roots
                assert not leaks, f"{fname}: imports network module {leaks}"
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                leaks = NETWORK_MODULES & {root}
                assert not leaks, f"{fname}: imports network module {root}"


def test_lab_module_has_no_module_scope_network_imports() -> None:
    for module in ("pipeline_lab.py", "huggingface_lab.py", "legalbench_lab.py"):
        tree = ast.parse((NB_DIR / module).read_text())
        for node in tree.body:
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            roots = {n.split(".")[0] for n in names}
            leaks = NETWORK_MODULES & roots
            assert not leaks, f"{module} imports network modules at scope: {leaks}"


# ---------------------------------------------------------------------------
# Duty 2 — headless re-execution from BOTH PLAN cwds (the heavy one, last)
# ---------------------------------------------------------------------------


def _execute_nb(path: Path, cwd: Path) -> int:
    """Execute a fresh copy of the notebook in-process; count cell errors."""
    import nbformat
    from nbclient import NotebookClient

    nb = nbformat.read(path, as_version=4)
    client = NotebookClient(
        nb,
        timeout=300,
        kernel_name="python3",
        resources={"metadata": {"path": str(cwd)}},
    )
    try:
        client.execute()
    except Exception:
        # CellExecutionError means a cell failed mid-execution.
        return 1
    return sum(
        1
        for c in nb.cells
        for out in c.get("outputs", [])
        if out.get("output_type") == "error"
    )


def _stored_error_count(fname: str) -> int:
    nb = json.loads((NB_DIR / fname).read_text())
    return sum(
        1
        for c in nb["cells"]
        for out in c.get("outputs", [])
        if out.get("output_type") == "error"
    )


@pytest.mark.parametrize("fname", sorted(PLANNED))
@pytest.mark.parametrize("cwd_name", ["repo_root", "notebooks_dir"])
def test_headless_reexecution_matches_stored_outputs(
    fname: str, cwd_name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PLAN duty 2 verbatim: headless execution of every notebook via
    ``nbclient`` from repo root AND notebooks/ itself, with stored outputs
    regenerating cleanly (cell error status compared, not pixel output)."""
    monkeypatch.chdir(REPO_ROOT if cwd_name == "repo_root" else NB_DIR)

    fresh_errors = _execute_nb(NB_DIR / fname, Path.cwd())
    assert fresh_errors == 0, f"{fname} @{cwd_name}: regenerated {fresh_errors} errors"
    assert _stored_error_count(fname) == 0, f"{fname}: COMMITTED outputs contain errors"
