import json
from pathlib import Path
import pytest

def test_dbg(temp_base_dir, mock_openai_client, mock_langchain_llm, monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_PROVIDER", "none")
    monkeypatch.setenv("MAILROOM_HF_PILOT_DIR", str(temp_base_dir / "hf_pilot"))
    monkeypatch.setenv("MAILROOM_VISION_ENABLED", "0")
    from scripts import run_hf_pilot as mod
    monkeypatch.setattr(mod, "_mock_samples", lambda per_class, per_subclass=0: [{
        "filename": "hf_contract.txt",
        "text": "SERVICES AGREEMENT between Acme and Beta. " * 20,
        "expected_hf_class": "contract",
        "expected_subclass": "fixture",
        "chars": 400,
    }])
    monkeypatch.setattr("sys.argv", ["run_hf_pilot.py", "--mock", "--per-class", "1"])
    with pytest.raises(AssertionError):
        assert mod.main() == 0
    reports = list((temp_base_dir / "hf_pilot").glob("*/report.json"))
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    print("ROW:", json.dumps(payload["samples"][0])[:500])
