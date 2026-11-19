"""The tool and the display-contract exporter must read the same compiled wiring.

Parity is structural, not a snapshot of today's 14 nodes: both callers go
through ``graph.contract``, so a future revision (PR 203/204 or a new class) is
described without touching this tool.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from graph import contract as graph_contract  # noqa: E402

from mailroom_diagnostics.snapshot import build_workspace_snapshot, load_artifact  # noqa: E402


@pytest.fixture(scope="module")
def exporter():
    from scripts import export_contract

    return export_contract


def test_exporter_delegates_to_the_shared_helper(exporter):
    assert exporter.node_row is graph_contract.node_row
    assert exporter._graph_section() == graph_contract.graph_section()


def test_workspace_snapshot_matches_the_compiled_graph():
    snapshot = build_workspace_snapshot()
    assert snapshot.graph.model_dump(mode="json") == graph_contract.graph_section()


def test_workspace_snapshot_matches_the_committed_artifact(contract_path):
    snapshot = build_workspace_snapshot()
    committed = json.loads(contract_path.read_text(encoding="utf-8"))
    assert snapshot.graph.model_dump(mode="json") == committed["graph"]
    assert snapshot.provenance.pipeline_version == committed["pipeline_version"]


def test_artifact_and_workspace_agree_on_the_topology(contract_path):
    assert load_artifact(contract_path).graph_hash == build_workspace_snapshot().graph_hash


def test_node_rows_keep_the_as_type_override():
    """A canonical binding is what the row records.

    ``as_type`` cannot rewrite a bound observation: ``classify-document`` is
    an agent, and a disagreeing override is rejected before a row is built.
    """
    from observability.tracing import NODE_OBSERVATION_TYPES, traced_node

    span = "classify-document"
    default = NODE_OBSERVATION_TYPES[span]
    plain = traced_node(span)(lambda state: state)
    assert graph_contract.node_row("classify", plain) == {
        "id": "classify",
        "span": span,
        "observation_type": default,
    }
    with pytest.raises(
        ValueError,
        match="observation 'classify-document' must be 'agent', got 'evaluator'",
    ):
        traced_node(span, as_type="evaluator")
