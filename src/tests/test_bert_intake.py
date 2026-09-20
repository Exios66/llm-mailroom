"""ModernBERT intake fast-path tests (mailroom-issues #85 M6a / #98).

The sibling ``mailroom_ml`` package is NOT a declared dependency — every
test here either exercises the fail-open paths (flag off / package absent /
model error / missing bundle) or injects a fake package via ``sys.modules``
(precedent: ``test_vllm_modal_capability.py``). No network, no models, no
Docker — hermetic by construction (conftest forces ``MAILROOM_BERT_INTAKE=0``
and pops ``ML_MODEL_DIR``/``BERT_*`` envs).
"""
import sys
import types

import pytest


def _fake_mailroom_ml(*, result=None, raises=None):
    """Build a minimal mailroom_ml package with inference/routing submodules."""
    inference = types.ModuleType("mailroom_ml.inference")

    if raises is not None:

        def classify_document(doc_text, filename=None):
            raise raises

    else:

        def classify_document(doc_text, filename=None):
            # Preserve non-dict results verbatim (None stays None so the
            # fail-open path sees a non-dict return).
            return None if result is None else dict(result)

    inference.classify_document = classify_document
    routing = types.ModuleType("mailroom_ml.routing")
    routing.build_handoff = lambda **kw: kw
    pkg = types.ModuleType("mailroom_ml")
    pkg.inference = inference
    pkg.routing = routing
    return pkg


@pytest.fixture
def fake_mailroom_ml(monkeypatch):
    """Install (or remove) a fake mailroom_ml package via sys.modules."""

    def _install(result=None, raises=None):
        mod = _fake_mailroom_ml(result=result, raises=raises)
        monkeypatch.setitem(sys.modules, "mailroom_ml", mod)
        monkeypatch.setitem(sys.modules, "mailroom_ml.inference", mod.inference)
        monkeypatch.setitem(sys.modules, "mailroom_ml.routing", mod.routing)
        return mod

    yield _install
    for name in ("mailroom_ml.routing", "mailroom_ml.inference", "mailroom_ml"):
        monkeypatch.delitem(sys.modules, name, raising=False)


def _enable_bert(monkeypatch):
    monkeypatch.setenv("MAILROOM_BERT_INTAKE", "1")


FAST_PATH_RESULT = {
    "status": "ok",
    "route": "fast_path",
    "doc_type": "contract",
    "subclass": "term_length",
    "score": 0.91,
    "quality": "good",
    "calibrated_confidence": 0.98,
    "subclass_confidence": 0.71,
    "n_windows": 1,
    "artifact_sha": "583f1d83e3009d54c306926d415720e50bcdff23",
    "agreement": 1.0,
    "margin": 0.62,
}


class TestFailOpenPaths:
    def test_flag_off_handoff_and_no_import(self, monkeypatch):
        from agents.bert_intake import run_bert_intake

        monkeypatch.delitem(sys.modules, "mailroom_ml", raising=False)
        monkeypatch.setenv("MAILROOM_BERT_INTAKE", "0")
        handoff = run_bert_intake("some clean text", filename="a.txt")
        assert handoff == {
            "available": False,
            "reason": "flag_off",
            "method": "deterministic",
            "routing_path": "clerk_only",
            "elapsed_ms": 0.0,
        }
        assert "mailroom_ml" not in sys.modules  # no import attempted when off

    def test_package_absent_fail_open(self, monkeypatch):
        from agents.bert_intake import run_bert_intake

        monkeypatch.delitem(sys.modules, "mailroom_ml", raising=False)
        _enable_bert(monkeypatch)
        handoff = run_bert_intake("text", filename="a.txt")
        assert handoff["available"] is False
        assert handoff["reason"] == "no_package"
        assert handoff["routing_path"] == "clerk_only"
        assert isinstance(handoff["elapsed_ms"], float)

    def test_bundle_missing_fail_open(self, monkeypatch, fake_mailroom_ml):
        from agents.bert_intake import run_bert_intake

        fake_mailroom_ml(result={"status": "no_model", "reason": "bundle_missing"})
        _enable_bert(monkeypatch)
        handoff = run_bert_intake("text", filename="a.txt")
        assert handoff["available"] is False
        assert handoff["reason"] == "no_model"
        assert handoff["routing_path"] == "clerk_only"

    def test_classify_error_fail_open(self, monkeypatch, fake_mailroom_ml):
        from agents.bert_intake import run_bert_intake

        fake_mailroom_ml(raises=RuntimeError("boom"))
        _enable_bert(monkeypatch)
        handoff = run_bert_intake("text", filename="a.txt")  # never raises
        assert handoff["available"] is False
        assert handoff["reason"] == "error"
        assert handoff["routing_path"] == "clerk_only"

    def test_non_dict_result_fail_open(self, monkeypatch, fake_mailroom_ml):
        from agents.bert_intake import run_bert_intake

        fake_mailroom_ml(result=None)  # classify_document returns None
        _enable_bert(monkeypatch)
        handoff = run_bert_intake("text", filename="a.txt")
        assert handoff["available"] is False
        assert handoff["reason"] == "error"

    def test_classify_typeerror_falls_back_without_filename(
        self, monkeypatch, fake_mailroom_ml
    ):
        from agents.bert_intake import run_bert_intake

        capture = {}

        def strict_classify(doc_text, *, required_kw="x"):
            # No `filename` parameter: a first call with filename= must raise
            # TypeError and the lane must retry without it.
            capture["called_with"] = doc_text
            return dict(FAST_PATH_RESULT)

        fake_mailroom_ml()
        module = sys.modules["mailroom_ml"]
        module.inference.classify_document = strict_classify
        _enable_bert(monkeypatch)
        handoff = run_bert_intake("text", filename="a.txt")
        assert handoff["available"] is True
        assert capture["called_with"] == "text"  # TypeError-fallback path used


class TestHappyPath:
    def test_fast_path_handoff_shape(self, monkeypatch, fake_mailroom_ml):
        from agents.bert_intake import run_bert_intake

        fake_mailroom_ml(result=FAST_PATH_RESULT)
        _enable_bert(monkeypatch)
        handoff = run_bert_intake("a short clean letter", filename="letter.txt")
        assert handoff["available"] is True
        assert handoff["reason"] == "ok"
        assert handoff["method"] == "bert"
        assert handoff["routing_path"] == "fast_path"
        assert handoff["doc_type"] == "contract"
        assert handoff["subclass"] == "term_length"
        assert handoff["calibrated_confidence"] == 0.98
        assert handoff["n_windows"] == 1
        assert handoff["artifact_sha"].endswith("bcdff23")
        assert isinstance(handoff["elapsed_ms"], float) and handoff["elapsed_ms"] >= 0

    def test_verify_route_handoff_not_fast_path(self, monkeypatch, fake_mailroom_ml):
        from agents.bert_intake import run_bert_intake

        result = dict(FAST_PATH_RESULT, route="verify")
        fake_mailroom_ml(result=result)
        _enable_bert(monkeypatch)
        handoff = run_bert_intake("text", filename="b.txt")
        assert handoff["available"] is True
        assert handoff["routing_path"] == "clerk_only"  # not a sorter-skip route

    def test_debug_capture_written(self, monkeypatch, fake_mailroom_ml, temp_base_dir):
        from agents.bert_intake import run_bert_intake

        fake_mailroom_ml(result=FAST_PATH_RESULT)
        _enable_bert(monkeypatch)
        handoff = run_bert_intake("debug me", filename="capture-me.txt")
        from pathlib import Path

        debug_root = Path(temp_base_dir) / "debug" / "bert_intake"
        assert debug_root.exists()
        entries = list(debug_root.iterdir())
        assert len(entries) == 1
        assert (entries[0] / "result.json").exists()
        assert (entries[0] / "input.txt").read_text() == "debug me"
        assert (entries[0] / "meta.json").exists()
        assert handoff["available"] is True

    def test_debug_kill_switch(self, monkeypatch, fake_mailroom_ml, temp_base_dir):
        from agents.bert_intake import run_bert_intake

        fake_mailroom_ml(result=FAST_PATH_RESULT)
        _enable_bert(monkeypatch)
        monkeypatch.setenv("MAILROOM_BERT_DEBUG", "0")
        run_bert_intake("no debug", filename="quiet.txt")
        from pathlib import Path

        debug_root = Path(temp_base_dir) / "debug" / "bert_intake"
        assert not debug_root.exists()


class TestPriorFormatting:
    def test_empty_without_fast_path_triage(self):
        from agents.bert_intake import format_bert_type_prior

        assert format_bert_type_prior(None) == ""
        assert format_bert_type_prior({}) == ""
        assert format_bert_type_prior({"route": "verify", "doc_type": "contract"}) == ""
        assert format_bert_type_prior({"route": "fast_path"}) == ""  # no class

    def test_fast_path_prior_labels_subclass_unverified(self):
        from agents.bert_intake import format_bert_type_prior

        prior = format_bert_type_prior(dict(FAST_PATH_RESULT))
        assert "[bert prior" in prior
        assert "primary class: contract" in prior
        assert "type confidence: 0.98" in prior
        assert "candidate subclass: term_length (UNVERIFIED — decide independently)" in prior

    def test_call_site_composition_matches_classify_node(self, monkeypatch):
        """The exact expression used in classify_node composes BERT on top."""
        from agents.bert_intake import format_bert_type_prior
        from agents.intake import format_intake_prior

        intake_prior = format_intake_prior(None)  # no LLM intake prep
        bert_prior = format_bert_type_prior(dict(FAST_PATH_RESULT))
        composed = "\n\n".join(p for p in (intake_prior, bert_prior) if p)
        assert "[bert prior" in composed
        assert composed.count("primary class: contract") == 1


class TestIntakeNodeIntegration:
    def _write_doc(self, tmp_path):
        f = tmp_path / "letter.txt"
        f.write_text("This is a short clean notice letter.", encoding="utf-8")
        return f

    def test_flag_off_node_emits_fail_open_handoff(
        self, monkeypatch, temp_base_dir, tmp_path
    ):
        from graph.build_graph import intake_node

        f = self._write_doc(tmp_path)
        monkeypatch.setenv("MAILROOM_BERT_INTAKE", "0")
        updates = intake_node({"file_path": str(f), "intake_meta": {"source": "upload"}})
        assert updates["intake_handoff"]["available"] is False
        assert updates["intake_handoff"]["reason"] == "flag_off"
        assert updates["bert_triage"] is not None
        assert updates["doc_id"]
        assert updates["stage"] == "processing"

    def test_node_merges_handoff_into_manifest(
        self, monkeypatch, fake_mailroom_ml, temp_base_dir, tmp_path
    ):
        from graph.build_graph import intake_node

        fake_mailroom_ml(result=FAST_PATH_RESULT)
        _enable_bert(monkeypatch)
        f = self._write_doc(tmp_path)
        updates = intake_node({"file_path": str(f), "intake_meta": {"source": "upload"}})
        assert updates["intake_handoff"]["available"] is True
        # The manifest on disk carries the merged intake.bert block
        # (the old `intake_prep`-gated merge would have DROPPED it).
        from pipeline.bins import manifests_dir

        manifests = [p for p in manifests_dir().glob("*.json")]
        assert manifests, "expected a written manifest"
        merged = None
        import json

        for p in manifests:
            m = json.loads(p.read_text())
            if m.get("intake", {}).get("bert", {}).get("available") is True:
                merged = m
        assert merged is not None, "manifest missing intake.bert handoff"
        assert merged["intake"]["bert"]["doc_type"] == "contract"

    def test_node_error_never_breaks_run(
        self, monkeypatch, fake_mailroom_ml, temp_base_dir, tmp_path
    ):
        from graph.build_graph import intake_node

        fake_mailroom_ml(raises=RuntimeError("boom"))
        _enable_bert(monkeypatch)
        f = self._write_doc(tmp_path)
        updates = intake_node({"file_path": str(f)})
        assert updates["intake_handoff"]["available"] is False
        assert updates["intake_handoff"]["reason"] == "error"
        assert updates["doc_id"] is not None  # run continued past the lane