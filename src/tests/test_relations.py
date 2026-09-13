"""HUB-040 relations layer tests — hermetic (fake embedder, tmp base dir).

Covers: ledger chain integrity + tamper detection, monotonic ordering under
rapid writes, edge upsert/novelty/vocabulary refusal, deterministic signals
(same-matter, keyword Jaccard, embedding cosine via the injection seam),
embedding compute-once caching, kill-switches + fail-soft, context block
bounding, LLM judgment validation (closed vocabulary + unproposed-pair
refusal), knowledge-graph projections (matter/global/ego + GraphML
well-formedness + intra-matter self-loop exclusion), and ledger render
events.
"""

from __future__ import annotations

import xml.dom.minidom
from datetime import datetime, timezone

import pytest

from pipeline import relations as P
from pipeline import relations_graph as G
from storage import relations as R


@pytest.fixture(autouse=True)
def _relations_env(monkeypatch, temp_base_dir):
    monkeypatch.setenv("MAILROOM_RELATIONS", "1")
    monkeypatch.delenv("MAILROOM_LLM_FREE_ONLY", raising=False)
    P.set_embedder(None)
    yield
    P.set_embedder(None)


@pytest.fixture
def fake_embedder():
    def _embed(texts):
        out = []
        for t in texts:
            low = t.lower()
            if "hail" in low:
                out.append([0.9, 0.1, 0.0])
            elif "merger" in low:
                out.append([0.0, 0.95, 0.1])
            else:
                out.append([0.0, 1.0, 0.0])
        return out

    P.set_embedder(_embed)
    return _embed


async def _seed_docs(rows: list[tuple]) -> None:
    from storage.catalog import DocumentRecord
    from storage.db import async_session, ensure_schema

    ensure_schema()
    async with async_session() as session:
        for did, matter, fname, kw, txt in rows:
            session.add(
                DocumentRecord(
                    doc_id=did,
                    matter_id=matter,
                    original_filename=fname,
                    stage="archived",
                    doc_type="insurance_claim" if matter == "M1" else "contract",
                    extracted_data={"keywords": kw, "subject_matter": txt, "_report": txt},
                    created_at=datetime.now(timezone.utc),
                )
            )
        await session.commit()


HAIL_ROWS = [
    ("docA", "M1", "fnol_a.txt", ["hail damage", "roof"], "Hail damage FNOL roof"),
    ("docB", "M1", "fnol_b.txt", ["hail damage", "siding"], "Hail damage siding"),
    ("docC", "M2", "merger_c.txt", ["indemnification"], "Merger agreement indemnification"),
]


# ---------------------------------------------------------------------------
# Ledger


def test_ledger_chain_verifies_and_detects_tampering(temp_base_dir):
    for i in range(20):  # rapid same-millisecond writes — the ordering hazard
        _write(i)
    ok, count = P.verify_ledger()
    assert ok is True
    assert count == 20

    chain = _chain()
    import asyncio

    from schemas.audit import AuditLogEntry, verify_chain
    from storage.db import async_session
    from sqlalchemy import update

    async def _tamper():
        async with async_session() as session:
            await session.execute(
                update(R.RelationLogRecord).where(R.RelationLogRecord.id == 5).values(detail={"tampered": True})
            )
            await session.commit()

    asyncio.run(_tamper())
    entries = [
        AuditLogEntry(
            entry_id=c["entry_id"], doc_id="__relations__", matter_id="relations",
            event=c["event"], actor=c["actor"], detail=c["detail"],
            prev_hash=c["prev_hash"], entry_hash=c["entry_hash"], timestamp=c["timestamp"],
        )
        for c in _chain()
    ]
    assert verify_chain(entries) is False


def _write(i: int) -> dict:
    import asyncio

    from storage import relations as R

    return asyncio.run(R.write_relation_log_entry("relation_recorded", {"i": i}))


def _chain() -> list[dict]:
    import asyncio

    from storage import relations as R

    return asyncio.run(R.get_relation_chain())


def test_latest_hash_matches_chain_tail(temp_base_dir):
    _write(1)
    _write(2)
    chain = _chain()
    assert chain[-1]["entry_hash"] == _latest_hash()


def _latest_hash() -> str:
    import asyncio

    from storage import relations as R

    return asyncio.run(R.get_latest_relation_hash())


# ---------------------------------------------------------------------------
# Edges


def test_edge_upsert_normalization_and_vocabulary_refusal(temp_base_dir):
    first = asyncio_run(
        R.record_edges(
            [
                {"source_doc_id": "B", "target_doc_id": "A", "relation_type": "same_matter", "score": 1.0},
                {"source_doc_id": "A", "target_doc_id": "C", "relation_type": "made_up", "score": 0.9},
                {"source_doc_id": "A", "target_doc_id": "A", "relation_type": "same_matter", "score": 1.0},
            ]
        )
    )
    assert first["inserted"] == 1 and first["refused"] == 2
    assert first["inserted_keys"] == [("A", "B", "same_matter")]  # canonical order
    again = asyncio_run(R.record_edges([{"source_doc_id": "A", "target_doc_id": "B", "relation_type": "same_matter", "score": 0.5}]))
    assert again["inserted"] == 0 and again["updated"] == 1
    edges = asyncio_run(R.list_edges(doc_id="A"))
    assert len(edges) == 1 and edges[0]["score"] == 0.5


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Deterministic scanning


def test_scan_document_signals_and_idempotence(temp_base_dir, fake_embedder):
    _seed(HAIL_ROWS)
    report = P.scan_document("docA")
    assert report.get("ok") is True
    assert report["edges_new"] == 2  # docB: same_matter + topic_overlap; docC unlinked
    # Scanning docB completes the pair: its cosine signal sees docA's cached
    # embedding and records the semantic edge (compute-once, compare-cached).
    P.scan_document("docB")
    edges = asyncio_run(R.all_edges())
    types = sorted((e["source_doc_id"], e["target_doc_id"], e["relation_type"]) for e in edges)
    assert ("docA", "docB", "same_matter") in types
    assert ("docA", "docB", "topic_overlap") in types
    assert ("docA", "docB", "semantic_similarity") in types
    assert all("docC" not in (s, t) for s, t, _ in types)  # unrelated doc stays unlinked
    # Re-scan: upserts, no new edges (novelty from the ledger's perspective)
    rescan = P.scan_document("docA")
    assert rescan["edges_new"] == 0 and rescan["edges_updated"] >= 1


def _seed(rows):
    import asyncio

    asyncio.run(_seed_docs(rows))


def test_embedding_cached_across_scans(temp_base_dir, fake_embedder):
    _seed(HAIL_ROWS)
    P.scan_document("docA")
    first = asyncio_run(R.get_embedding("docA", P.embedding_model_name()))
    assert first == pytest.approx([0.9, 0.1, 0.0], abs=1e-5)  # float32 storage
    calls = {"n": 0}

    def counting(texts):
        calls["n"] += 1
        return fake_embedder(texts)

    P.set_embedder(counting)
    P.scan_document("docB")  # docA's embedding must come from cache
    assert calls["n"] == 1  # only docB itself was embedded


def test_kill_switch_disables_layer(temp_base_dir, monkeypatch, fake_embedder):
    _seed(HAIL_ROWS)
    monkeypatch.setenv("MAILROOM_RELATIONS", "0")
    assert P.scan_document("docA") == {"skipped": "disabled"}
    assert P.sweep() == {"skipped": "disabled"}
    assert asyncio_run(R.count_edges()) == 0


def test_sweep_is_incremental(temp_base_dir, fake_embedder):
    _seed(HAIL_ROWS)
    first = P.sweep(limit=10)
    assert first["scanned"] == 3
    second = P.sweep(limit=10)
    assert second["scanned"] == 0  # ledger novelty is the gate
    assert second["pending_remaining"] == 0


# ---------------------------------------------------------------------------
# Context injection


def test_context_block_bounded_and_advisory(temp_base_dir, fake_embedder):
    _seed(HAIL_ROWS)
    P.scan_document("docA")
    block = P.context_block(matter_id="M1")
    assert block.startswith("RELATED (advisory")
    assert "same_matter" in block
    # Empty ledger ⇒ empty string (no noise)
    assert P.context_block(matter_id="NO-SUCH") == ""


def test_context_kill_switch(temp_base_dir, monkeypatch, fake_embedder):
    _seed(HAIL_ROWS)
    P.scan_document("docA")
    monkeypatch.setenv("MAILROOM_RELATIONS_CONTEXT", "0")
    assert P.context_block(matter_id="M1") == ""


# ---------------------------------------------------------------------------
# LLM judgment pass (validator only — the lane is OFF in the pilot)


def test_llm_pass_disabled_in_pilot(temp_base_dir):
    assert P.llm_pass_enabled() is False  # taxonomy relations.llm: false


def test_validate_judgments_refuses_unproposed_pairs_and_invented_types():
    judgments = P_module_validate(
        {
            "judgments": [
                {"source_doc_id": "A", "target_doc_id": "B", "relation_type": "party_overlap", "confidence": 1.7},
                {"source_doc_id": "A", "target_doc_id": "Z", "relation_type": "party_overlap", "confidence": 0.9},
                {"source_doc_id": "A", "target_doc_id": "B", "relation_type": "made_up", "confidence": 0.9},
                {"source_doc_id": "A", "target_doc_id": "A", "relation_type": "party_overlap", "confidence": 0.9},
            ]
        },
        allowed_pairs={("A", "B")},
    )
    assert judgments == [
        {"source_doc_id": "A", "target_doc_id": "B", "relation_type": "party_overlap", "confidence": 1.0, "rationale": ""}
    ]


def P_module_validate(raw, allowed_pairs):
    from agents.relations import validate_judgments

    return validate_judgments(raw, allowed_pairs)


# ---------------------------------------------------------------------------
# HUB-051: the LLM judgment pass WIRING (scan_document -> judge -> ledger)


LLM_ROWS = [
    ("docA", "M1", "fnol_a.txt", ["hail damage", "roof"], "Hail damage FNOL roof"),
    # docD: shares a keyword at jaccard 1/6 < 0.25 and embeds far from docA —
    # the ambiguous band (sub-threshold on every signal), never a
    # deterministic edge, exactly what the judge exists for.
    ("docD", "M3", "wind_d.txt", ["hail damage", "wind", "window", "deck", "porch"], "Wind and window damage"),
]


class _FakeRelationsAgent:
    """Stand-in for agents.relations.RelationsAgent (already-validated
    judgments — the validator itself is covered above)."""

    def __init__(self, judgments):
        self.judgments = judgments
        self.seen_candidates = []

    def judge(self, candidates):
        self.seen_candidates.append(candidates)
        return self.judgments


def _enable_llm_pass(monkeypatch):
    monkeypatch.setattr(P, "llm_pass_enabled", lambda: True)


def test_llm_judgment_pass_records_gated_llm_asserted_edges(temp_base_dir, monkeypatch, fake_embedder):
    _seed(LLM_ROWS)
    _enable_llm_pass(monkeypatch)
    fake = _FakeRelationsAgent(
        [
            {
                "source_doc_id": "docA",
                "target_doc_id": "docD",
                "relation_type": "llm_asserted",
                "confidence": 0.8,
                "rationale": "shared hail-damage context across matters",
            }
        ]
    )
    monkeypatch.setattr("agents.relations.RelationsAgent", lambda: fake)

    report = P.scan_document("docA")
    assert report.get("ok") is True
    assert report["edges_llm"] == 1

    edges = asyncio_run(R.all_edges())
    llm_edges = [e for e in edges if e["relation_type"] == "llm_asserted"]
    assert len(llm_edges) == 1
    assert llm_edges[0]["method"] == "llm"
    assert llm_edges[0]["score"] == 0.8
    assert llm_edges[0]["source_matter_id"] == "M1"
    assert llm_edges[0]["target_matter_id"] == "M3"
    assert llm_edges[0]["evidence"]["rationale"] == "shared hail-damage context across matters"

    # The judge only saw the ambiguous-band pair, bounded by top_k.
    assert len(fake.seen_candidates) == 1
    seen_pairs = {(c["source_doc_id"], c["target_doc_id"]) for c in fake.seen_candidates[0]}
    assert ("docA", "docD") in seen_pairs

    # Ledger stays intact with the new edge recorded.
    ok, _ = P.verify_ledger()
    assert ok is True


def test_llm_judgment_pass_gates_low_confidence_and_refuses_novel_pairs(temp_base_dir, monkeypatch, fake_embedder):
    _seed(LLM_ROWS)
    _enable_llm_pass(monkeypatch)
    # 0.4 < the 0.55 gate → refused; the docZ pair was never proposed by the
    # scanner → refused regardless of confidence.
    fake = _FakeRelationsAgent(
        [
            {
                "source_doc_id": "docA",
                "target_doc_id": "docD",
                "relation_type": "llm_asserted",
                "confidence": 0.4,
                "rationale": "weak signal",
            },
            {
                "source_doc_id": "docA",
                "target_doc_id": "docZ",
                "relation_type": "llm_asserted",
                "confidence": 0.9,
                "rationale": "invented",
            },
        ]
    )
    monkeypatch.setattr("agents.relations.RelationsAgent", lambda: fake)

    report = P.scan_document("docA")
    assert report["edges_llm"] == 0
    edges = asyncio_run(R.all_edges())
    assert all(e["relation_type"] != "llm_asserted" for e in edges)


def test_llm_judgment_pass_off_by_default_skips_agent(temp_base_dir, monkeypatch, fake_embedder):
    _seed(LLM_ROWS)
    fake = _FakeRelationsAgent([])
    monkeypatch.setattr("agents.relations.RelationsAgent", lambda: fake)
    report = P.scan_document("docA")
    assert report.get("edges_llm") == 0
    assert fake.seen_candidates == []  # the agent is never constructed


def test_llm_judgment_failure_fails_soft(temp_base_dir, monkeypatch, fake_embedder):
    _seed(LLM_ROWS)
    _enable_llm_pass(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("model unreachable")

    monkeypatch.setattr("agents.relations.RelationsAgent", _boom)
    report = P.scan_document("docA")
    assert report.get("ok") is True  # the deterministic layer still lands
    assert report.get("edges_llm") == 0


def test_relations_scan_cli_module_exists_and_verifies_ledger(temp_base_dir, capsys):
    """HUB-051: the documented ``python -m pipeline.relations_scan``
    entrypoint exists (it crashed with ModuleNotFoundError before the fix)
    and the verify-ledger verb works on an empty chain."""
    import pipeline.relations_scan as cli

    rc = cli.main(["--verify-ledger"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "relations ledger: OK" in out


def test_embed_uses_dojo_matcher_singleton(temp_base_dir, monkeypatch):
    """HUB-051: the REAL embedding path drives the dojo's shared matcher.

    The dojo's public ``get_embedding_model()`` returns the model NAME string
    — the old ``model.encode(vals, show_progress_bar=False)`` call was a
    TypeError on every real run (``relations_embed_failed``; the cosine
    signal only ever worked through the injected test seam). The fix routes
    through the ``_EmbeddingMatcher`` singleton with its local-model load.
    """
    import numpy as np

    monkeypatch.setenv("MAILROOM_RELATIONS_EMBEDDINGS", "1")
    P.set_embedder(None)

    class FakeMatcher:
        def __init__(self):
            self.local_loaded = False
            self.calls = []

        def _load_local(self):
            self.local_loaded = True

        def _embed(self, text):
            self.calls.append(text)
            return np.array([0.1, 0.2, 0.3])

    fake = FakeMatcher()
    monkeypatch.setattr("llm_dojo_scoring.field_scoring._get_embedding", lambda: fake)
    vectors = P._embed(["some document text"])
    assert vectors == [[0.1, 0.2, 0.3]]
    assert fake.local_loaded is True
    assert fake.calls == ["some document text"]


def test_embed_degrades_when_dojo_matcher_missing(temp_base_dir, monkeypatch):
    """HUB-051: no matcher ⇒ None (the cosine signal skips; other signals
    flow) — never a crash."""
    monkeypatch.setenv("MAILROOM_RELATIONS_EMBEDDINGS", "1")
    P.set_embedder(None)
    monkeypatch.setattr("llm_dojo_scoring.field_scoring._get_embedding", lambda: None)
    assert P._embed(["text"]) is None


# ---------------------------------------------------------------------------
# Knowledge graphs


def test_graph_projections(temp_base_dir, fake_embedder):
    _seed(HAIL_ROWS)
    P.sweep(limit=10)
    # Bridge edge M1 -> M2 (party overlap)
    asyncio_run(
        R.record_edges(
            [
                {
                    "source_doc_id": "docA",
                    "target_doc_id": "docC",
                    "relation_type": "party_overlap",
                    "score": 0.7,
                    "source_matter_id": "M1",
                    "target_matter_id": "M2",
                    "evidence": {"shared": ["acme corp"]},
                }
            ]
        )
    )
    matter = G.graph_data(matter_id="M1")
    node_ids = {n["id"] for n in matter["nodes"]}
    assert {"docA", "docB", "matter::M2"} <= node_ids
    link_pairs = {(l["source"], l["target"], l["relation_type"]) for l in matter["links"]}
    assert ("docA", "docB", "same_matter") in link_pairs
    assert ("docA", "matter::M2", "cross_matter_link") in link_pairs

    global_view = G.graph_data(global_view=True)
    g_links = {(l["source"], l["target"]) for l in global_view["links"]}
    assert ("M1", "M2") in g_links
    assert all(a != b for a, b in g_links)  # no intra-matter self-loops

    ego = G.graph_data(ego_doc_id="docA")
    assert {n["id"] for n in ego["nodes"]} == {"docA", "docB", "docC"}


def test_graph_renderers_and_ledger_events(temp_base_dir, fake_embedder):
    _seed(HAIL_ROWS)
    P.sweep(limit=10)
    result = G.refresh_graphs()
    assert "global.json" in result["rendered"]
    out = G.graphs_dir()
    assert (out / "matter-M1.json").exists()
    assert (out / "matter-M1.graphml").exists()
    xml.dom.minidom.parse(str(out / "global.graphml"))  # well-formed
    data = (out / "matter-M1.json")
    assert data.exists()
    ok, count = P.verify_ledger()
    assert ok is True
    chain = _chain()
    renders = [c for c in chain if c["event"] == "relations_graph_rendered"]
    assert len(renders) >= 1


# ---------------------------------------------------------------------------
# HUB-051: full judgment I/O debug capture (the triage-lane pattern)


def test_judge_writes_full_io_debug_capture(temp_base_dir, mocker):
    """Every judgment call leaves the complete system/user/response/judgments
    under debug/relations/ — a refused or empty verdict is explainable."""
    from pathlib import Path

    from agents import relations as A

    agent = A.RelationsAgent()
    mocker.patch.object(
        agent,
        "_call_structured",
        return_value={
            "judgments": [
                {
                    "source_doc_id": "A",
                    "target_doc_id": "B",
                    "relation_type": "party_overlap",
                    "confidence": 0.9,
                    "rationale": "shared insured party",
                }
            ]
        },
    )
    out = agent.judge([{"source_doc_id": "A", "target_doc_id": "B", "signals": [], "evidence": {}}])
    assert len(out) == 1

    roots = list((Path(temp_base_dir) / "debug" / "relations").iterdir())
    assert len(roots) == 1
    assert (roots[0] / "system.txt").exists()
    assert (roots[0] / "user.txt").exists()
    assert "CANDIDATE RELATIONS" in (roots[0] / "user.txt").read_text()
    assert (roots[0] / "judgments.json").exists()
    assert "party_overlap" in (roots[0] / "judgments.json").read_text()


def test_judge_debug_capture_survives_unparseable_response(temp_base_dir, mocker):
    from pathlib import Path

    from agents import relations as A

    agent = A.RelationsAgent()
    mocker.patch.object(
        agent, "_call_structured", return_value={"_raw": "this is not json", "_parse_error": True}
    )
    out = agent.judge([{"source_doc_id": "A", "target_doc_id": "B", "signals": [], "evidence": {}}])
    assert out == []  # refused — nothing unvalidated reaches the ledger

    roots = list((Path(temp_base_dir) / "debug" / "relations").iterdir())
    assert len(roots) == 1
    assert "this is not json" in (roots[0] / "response.txt").read_text()
    assert (roots[0] / "judgments.json").read_text() == "[]"
