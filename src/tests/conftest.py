import os
import sys
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # src/


@pytest.fixture(autouse=True)
def _set_test_env():
    # Freeze .env loading FIRST (module-global `_loaded` latch): production
    # code paths that call load_env() mid-test must never re-inject gitignored
    # .env values after the pops below (deterministic hermeticity — otherwise
    # tests pass/fail depending on which module happened to import first).
    from pipeline.env import load_env as _load_env

    _load_env()
    os.environ.setdefault("OPENROUTER_API_KEY", "test-key-not-real")
    os.environ.setdefault("MAILROOM_BASE_DIR", os.environ.get("MAILROOM_BASE_DIR", "/tmp/mailroom-test"))
    # Keep tests hermetic: never pick up the real .env Langfuse/Braintrust keys
    # (llm/client.py now loads .env at import time).
    os.environ["OBSERVABILITY_PROVIDER"] = "none"
    # Production .env may enable the docclass arm, force vision off, or pin
    # DEFAULT_PROVIDER; tests must stay hermetic unless a case opts in.
    os.environ["MAILROOM_DOCCLASS_PROMPTS"] = "0"
    # Gmail intake (HUB-037) is opt-in in production (.env); tests must never
    # pick the real credentials up and start network polls.
    os.environ["MAILROOM_GMAIL_ENABLED"] = "0"
    # LLM-assisted intake (HUB-038) is also opt-in-able; tests stay on the
    # deterministic clerk unless a case opts in (patched gate + mock client).
    os.environ["MAILROOM_LLM_INTAKE"] = "0"
    for k in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
        os.environ.pop(k, None)
    os.environ.pop("MAILROOM_VISION_ENABLED", None)
    os.environ.pop("DEFAULT_PROVIDER", None)
    # HUB-039 free-only guardrail is opt-in via .env; tests must stay hermetic
    # unless a case opts in explicitly (test_llm_free_only.py sets it itself).
    os.environ.pop("MAILROOM_LLM_FREE_ONLY", None)
    for k in ("LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_HOST",
              "LANGFUSE_BASE_URL", "BRAINTRUST_API_KEY"):
        os.environ.pop(k, None)


@pytest.fixture
def temp_base_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["MAILROOM_BASE_DIR"] = tmpdir
        from pipeline.bins import (
            inbox_dir, processing_dir, classified_dir,
            review_dir, failed_dir, archive_dir, manifests_dir, ensure_dirs,
        )
        ensure_dirs(
            inbox_dir(),
            processing_dir(),
            classified_dir(),
            review_dir(),
            failed_dir(),
            archive_dir(),
            manifests_dir(),
        )
        try:
            from graph.build_graph import reset_compiled_graph

            reset_compiled_graph()
        except Exception:
            pass
        yield Path(tmpdir)
        os.environ.pop("MAILROOM_BASE_DIR", None)


@pytest.fixture(autouse=True)
def mock_langchain_llm(mocker):
    """Patch the vendored LangChain agents' ChatOpenAI path with a
    deterministic fake (no network). The LangChain sorter/contracts
    specialist build their own ChatOpenAI and bypass llm.client.get_llm, so
    the mock targets langchain_agents.base_agent.BaseAgent.llm instead.

    Tests configure per-test behavior by mutating the returned fake's
    ``classification`` / ``extraction`` canned dicts.
    """
    from langchain_agents.base_agent import BaseAgent as _LangChainBaseAgent
    from langchain_agents.mock import FakeLangChainLLM

    fake = FakeLangChainLLM()
    mocker.patch.object(_LangChainBaseAgent, "llm", new=lambda self: fake)
    return fake


def _make_mock_client(content: str) -> MagicMock:
    mock_choice = MagicMock()
    mock_choice.message.content = content
    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    mock_chat = MagicMock()
    mock_chat.completions.create.return_value = mock_completion
    mock_client = MagicMock()
    mock_client.chat = mock_chat
    return mock_client


@pytest.fixture
def mock_openai_client(mocker):
    mock_client = _make_mock_client(
        '{"doc_type": "contract", "contract_subtype": "other", "confidence": 0.99, "reasoning": "Contract found"}'
    )
    mocker.patch("llm.client.OpenAI", return_value=mock_client)
    mocker.patch("agents.base.BaseAgent.__init__", lambda self, mock=mock_client: setattr(self, "client", mock_client) or setattr(self, "model", "test-model"))
    return mock_client


@pytest.fixture
def mock_low_confidence_client(mocker):
    mock_client = _make_mock_client(
        '{"doc_type": "contract", "confidence": 0.50, "reasoning": "Unsure"}'
    )
    mocker.patch("llm.client.OpenAI", return_value=mock_client)
    mocker.patch("agents.base.BaseAgent.__init__", lambda self, mock=mock_client: setattr(self, "client", mock_client) or setattr(self, "model", "test-model"))
    return mock_client


@pytest.fixture
def sample_contract_text():
    fixture = Path(__file__).parent / "fixtures" / "contract" / "sample_msa.txt"
    return fixture.read_text()


@pytest.fixture
def sample_corporate_text():
    fixture = Path(__file__).parent / "fixtures" / "corporate_record" / "sample_bylaws.txt"
    return fixture.read_text()


@pytest.fixture
def sample_dd_text():
    fixture = Path(__file__).parent / "fixtures" / "due_diligence" / "sample_dd_report.txt"
    return fixture.read_text()


@pytest.fixture
def sample_correspondence_text():
    fixture = Path(__file__).parent / "fixtures" / "correspondence" / "sample_demand_letter.txt"
    return fixture.read_text()


@pytest.fixture
def sample_compliance_text():
    fixture = Path(__file__).parent / "fixtures" / "compliance_filing" / "sample_10k.txt"
    return fixture.read_text()


@pytest.fixture
def sample_ambiguous_text():
    fixture = Path(__file__).parent / "fixtures" / "contract" / "ambiguous_doc.txt"
    return fixture.read_text()


@pytest.fixture
def sample_court_opinion_text():
    fixture = Path(__file__).parent / "fixtures" / "court_opinion" / "sample_opinion.txt"
    return fixture.read_text()


@pytest.fixture
def sample_insurance_claim_text():
    """Synthetic FNOL-style sample — insurance_claim has no external benchmark
    (KANBAN-067 honesty note: synthetic samples only, no external dataset)."""
    fixture = Path(__file__).parent / "fixtures" / "insurance_claim" / "sample_claim.txt"
    return fixture.read_text()


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def all_fixture_files():
    files = {}
    for doc_type_dir in FIXTURES_DIR.iterdir():
        if doc_type_dir.is_dir():
            for f in doc_type_dir.iterdir():
                if f.suffix == ".txt":
                    files[f"{doc_type_dir.name}/{f.name}"] = f.read_text()
    return files
