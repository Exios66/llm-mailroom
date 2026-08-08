import os
import sys
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _set_test_env():
    os.environ.setdefault("OPENROUTER_API_KEY", "test-key-not-real")
    os.environ.setdefault("MAILROOM_BASE_DIR", os.environ.get("MAILROOM_BASE_DIR", "/tmp/mailroom-test"))
    # Keep tests hermetic: never pick up the real .env Langfuse/Braintrust keys
    # (llm/client.py now loads .env at import time).
    os.environ["OBSERVABILITY_PROVIDER"] = "none"
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
        yield Path(tmpdir)
        os.environ.pop("MAILROOM_BASE_DIR", None)


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
        '{"doc_type": "contract", "confidence": 0.95, "reasoning": "Contract found"}'
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
