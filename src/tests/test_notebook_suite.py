"""Guard suite for the notebooks/ walkthroughs (KANBAN-095, mailroom #15).

Enforces the four duties promised in ``notebooks/PLAN.md`` (§Guards):

1. every planned notebook exists with title + honesty-label cells, and the
   shared bench module imports network-free;
2. headless re-execution of every notebook via ``nbclient`` from the repo
   root, plus ``CWD_NOTEBOOKS`` (00 and 14) from notebooks/, regenerates
   cleanly — cell error status must match the committed stored outputs
   (zero errors) and a notebooks/ run must leave no new files there. That
   is sound because a static guard pins every notebook cell and lab module
   to one cwd bootstrap with no other cwd read or cwd-relative path;
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
    "14_gmail_pilot.ipynb": "Gmail corpus pilot",
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


def test_every_graph_node_has_a_span_name() -> None:
    """A node added to the graph needs its traced span name in NODE_SPAN_NAMES, or
    ``trace_contract`` raises KeyError in notebooks 07 and 08."""
    nodes = set(lab.graph_map()["nodes"])
    assert nodes <= set(lab.NODE_SPAN_NAMES), sorted(nodes - set(lab.NODE_SPAN_NAMES))


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
    for module in ("pipeline_lab.py", "huggingface_lab.py", "legalbench_lab.py", "gmail_pilot_lab.py"):
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
# Duty 2 — headless re-execution from the PLAN cwds (the heavy one, last)
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


# Every notebook locates the repo through ONE shared bootstrap: ``ROOT = Path.cwd()`` then walk
# up to the directory holding ``pyproject.toml``. A static guard (``_cwd_findings``) enforces that
# nothing else in a notebook cell or a ``notebooks/*.py`` lab module reads the working directory
# or names a cwd-relative path (``Path("data/x")``, ``open("x")``, ``os.listdir("x")`` ...). So the
# second PLAN cwd exercises the same code in every notebook, and re-executing all 15 from
# ``notebooks/`` (~80 s) would only repeat it. Each notebook runs from the repo root. The
# ``notebooks/`` start runs for two cheap notebooks:
#   - 00, whose bootstrap is the canonical form, to prove the walk-up works;
#   - 14, the one notebook that writes files (a pilot attachment under ``data/pilot_runs``).
# Execution alone cannot catch a misplaced write (14 ran green from ``notebooks/`` while creating
# ``notebooks/data/``), so those runs also assert that nothing new appeared under ``notebooks/``.
CWD_NOTEBOOKS = ("00_pipeline_anatomy.ipynb", "14_gmail_pilot.ipynb")

_REEXEC_CASES = [(fname, "repo_root") for fname in sorted(PLANNED)] + [
    (fname, "notebooks_dir") for fname in CWD_NOTEBOOKS
]

_ROOT_WALK_UP = (
    "ROOT = Path.cwd()",
    'while ROOT != ROOT.parent and not (ROOT / "pyproject.toml").exists():',
    "ROOT = ROOT.parent",
)
_CWD_CALLS = {"cwd", "getcwd", "getcwdb", "chdir", "fchdir"}
_PATH_CTORS = {"Path", "PurePath", "PosixPath", "WindowsPath"}
# Functions whose first argument is a filesystem path resolved against the working directory.
# Methods that take a pattern or content (``Path.glob("*.json")``, ``Path.write_text("x")``) are not
# paths; their receiver is a ``Path(...)`` and is covered by the constructor rule.
_FS_CALLS = {"open", "listdir", "scandir", "mkdir", "makedirs", "isfile", "isdir", "exists",
             "remove", "unlink", "rmtree"}
_GLOB_MODULE_CALLS = {"glob", "iglob"}
_CWD_MAGIC = re.compile(r"^\s*[%!].*\b(cd|pushd|popd|chdir)\b")


def _call_name(node: ast.Call) -> str:
    """Bare name of the callable (``Path.cwd`` -> ``cwd``, ``open`` -> ``open``)."""
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _is_relative_literal(node: ast.expr) -> bool:
    """True for a string literal that is neither absolute nor a ``~`` home path."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return not node.value.startswith(("/", "~"))
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return not str(node.values[0].value).startswith(("/", "~"))
    return False


def _cwd_findings(source: str, *, allow_bootstrap: bool) -> list[str]:
    """Every way ``source`` depends on the working directory, except the shared bootstrap.

    Flags cwd reads (``Path.cwd``, ``os.getcwd``, ``chdir``, ``os.curdir``, ``%cd``), ``Path()`` with
    no argument, and a relative string literal handed to ``Path(...)`` or a filesystem call. With
    ``allow_bootstrap`` the one ``ROOT = Path.cwd()`` assignment, and cwd reads inside an
    ``assert`` message, are permitted.
    """
    findings: list[str] = []
    lines = []
    for line in source.splitlines():
        if _CWD_MAGIC.match(line):
            findings.append(f"magic changes or reads cwd: {line.strip()!r}")
        lines.append("pass" if line.lstrip().startswith(("%", "!")) else line)
    tree = ast.parse("\n".join(lines))
    allowed: set[int] = set()
    if allow_bootstrap:
        # Only the FIRST ``ROOT = Path.cwd()`` is the bootstrap: a later one would reset ROOT to
        # the starting directory after the walk-up.
        boots = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                 and [t.id for t in node.targets if isinstance(t, ast.Name)] == ["ROOT"]
                 and _call_name(node.value) == "cwd"]
        if boots:
            allowed.add(id(min(boots, key=lambda n: (n.lineno, n.col_offset)).value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert) and node.msg is not None:
                allowed.update(id(n) for n in ast.walk(node.msg))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "curdir":
            findings.append(f"line {node.lineno}: os.curdir")
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in _CWD_CALLS and id(node) not in allowed:
            findings.append(f"line {node.lineno}: {name}() reads or changes the working directory")
        elif name in _PATH_CTORS and not node.args:
            findings.append(f"line {node.lineno}: {name}() with no argument is the working directory")
        elif (node.args and _is_relative_literal(node.args[0]) and (
                name in _PATH_CTORS | _FS_CALLS
                or (name in _GLOB_MODULE_CALLS and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "glob"))):
            findings.append(f"line {node.lineno}: {name}() on a cwd-relative literal path")
    return findings


def _notebook_code_cells(fname: str) -> list[str]:
    """Source of every code cell of a planned notebook."""
    nb = json.loads((NB_DIR / fname).read_text())
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


@pytest.mark.parametrize("source,expected", [
    ('ROOT = Path.cwd()\nassert ROOT, f"not found above {Path.cwd()}"\n', 0),
    ('import os\nx = os.getcwd()\n', 1),
    ('ROOT = Path.cwd()\nelsewhere = Path.cwd()\n', 1),
    ('ROOT = Path.cwd()\nROOT = ROOT.parent\nROOT = Path.cwd()\n', 1),
    ('os.chdir("/tmp")\n', 1),
    ('d = Path("data/pilot_runs") / "x"\n', 1),
    ('d = Path(f"data/{x}")\n', 1),
    ('open("notes.txt")\n', 1),
    ('os.listdir("out")\n', 1),
    ('import glob\nglob.glob("out/*.json")\n', 1),
    ('list(d.glob("*.json"))\np.write_text("x")\n', 0),
    ('p = Path()\n', 1),
    ('p = os.curdir\n', 1),
    ('%cd ..\nx = 1\n', 1),
    ('d = ROOT / "data" / "pilot_runs"\nq = Path("/tmp/x")\nopen(path)\n', 0),
])
def test_cwd_findings_detects_each_way_of_depending_on_the_working_directory(source: str, expected: int) -> None:
    """The static guard flags cwd reads and cwd-relative paths, and passes root-anchored code."""
    assert len(_cwd_findings(source, allow_bootstrap=True)) == expected, source


def test_the_bootstrap_is_only_allowed_when_asked_for() -> None:
    """Lab modules get no bootstrap allowance: the same assignment is a finding there."""
    assert _cwd_findings("ROOT = Path.cwd()\n", allow_bootstrap=False)


@pytest.mark.parametrize("fname", sorted(PLANNED))
def test_cwd_dependence_is_confined_to_the_root_bootstrap(fname: str) -> None:
    """The justification for running the ``notebooks/`` cwd only for ``CWD_NOTEBOOKS``: each
    notebook finds the repo root by walking up from the cwd once, and nothing else in any code
    cell reads the working directory or uses a cwd-relative path."""
    cells = _notebook_code_cells(fname)
    bootstrap = [src for src in cells if "ROOT = Path.cwd()" in src]
    assert bootstrap, f"{fname}: no cell finds the repo root; cwd handling changed, re-audit"
    # Each cell may hold one bootstrap, so a second one in a later cell would also pass per
    # cell: allow exactly one per notebook.
    assert sum(src.count("ROOT = Path.cwd()") for src in cells) == 1, (
        f"{fname}: more than one ROOT = Path.cwd(); a later one resets ROOT to the start cwd")
    for src in bootstrap:
        for line in _ROOT_WALK_UP:
            assert line in src, f"{fname}: root bootstrap incomplete ({line!r} missing)"
    for n, src in enumerate(cells):
        findings = _cwd_findings(src, allow_bootstrap=True)
        assert not findings, f"{fname} code cell {n}: {findings}"


def test_lab_modules_never_depend_on_the_working_directory() -> None:
    """``notebooks/*.py`` helpers are imported by every notebook, so they get no cwd allowance."""
    for module in sorted(NB_DIR.glob("*.py")):
        if module.name == "_build_extension_notebooks.py":
            continue  # generator script, never imported or executed by a notebook
        findings = _cwd_findings(module.read_text(), allow_bootstrap=False)
        assert not findings, f"{module.name}: {findings}"


def _notebooks_dir_entries() -> set[Path]:
    """Every path under ``notebooks/`` except bytecode and checkpoint caches."""
    return {
        p for p in NB_DIR.rglob("*")
        if "__pycache__" not in p.parts and ".ipynb_checkpoints" not in p.parts
    }


@pytest.mark.parametrize("fname,cwd_name", _REEXEC_CASES)
def test_headless_reexecution_matches_stored_outputs(
    fname: str, cwd_name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PLAN duty 2: headless execution of every notebook via ``nbclient`` from the repo root,
    and from ``notebooks/`` itself for the cwd representative (see ``CWD_REPRESENTATIVE``),
    with stored outputs regenerating cleanly (cell error status compared, not pixel output)."""
    monkeypatch.chdir(REPO_ROOT if cwd_name == "repo_root" else NB_DIR)
    before = _notebooks_dir_entries()

    fresh_errors = _execute_nb(NB_DIR / fname, Path.cwd())
    assert fresh_errors == 0, f"{fname} @{cwd_name}: regenerated {fresh_errors} errors"
    if cwd_name == "notebooks_dir":
        stray = sorted(str(p.relative_to(NB_DIR)) for p in _notebooks_dir_entries() - before)
        assert not stray, f"{fname} wrote into notebooks/ instead of the repo root: {stray}"
    assert _stored_error_count(fname) == 0, f"{fname}: COMMITTED outputs contain errors"
