"""Vision-mode ingestion: when an input agent's model is vision-capable (e.g.
Qwen), PDFs are rendered to page-image data-URIs and sent to the sorter/
specialist prompts as multimodal content. Text-only models keep the
plain-string behaviour unchanged. The transcription is still stored as
`doc_text` for text-only processes, the judge, reports and auditability.
"""

import base64
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agents.base import BaseAgent


@pytest.fixture
def page_images(pdf_fixture):
    from llm.vision import render_document_pages

    return render_document_pages(pdf_fixture)


@pytest.fixture
def pdf_fixture(tmp_path):
    from scripts.prepare_samples import generate_pdf_from_text

    src = tmp_path / "doc.txt"
    src.write_text(
        "THIS IS A CONTRACT between Alpha Corp and Beta LLC. Effective Jan 1 2025.",
        encoding="utf-8",
    )
    pdf = tmp_path / "doc.pdf"
    generate_pdf_from_text(src, pdf)
    return pdf


def _completion(content):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.usage = None
    return resp


def test_vision_capable_model_matches_config():
    from llm.vision import is_vision_capable

    assert is_vision_capable("qwen/qwen3.7-flash")
    assert is_vision_capable("Qwen/Qwen3.7-flash")
    assert not is_vision_capable("deepseek/deepseek-v4-flash")
    assert not is_vision_capable("")


def test_render_pdf_pages_produces_data_uris(pdf_fixture):
    from llm.vision import render_document_pages

    pages = render_document_pages(pdf_fixture)
    assert pages
    assert pages[0].startswith("data:image/png;base64,")
    raw = base64.b64decode(pages[0].split(",", 1)[1])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_caps_pages():
    from llm.vision import render_pdf_pages

    # A positive cap bounds the image budget…
    ten = render_pdf_pages(Path("docs/examples/samples/contract/contract_03_service_agreement.pdf"), cap=10)
    assert len(ten) == 10
    # …while cap=0 (or None) means "ALL pages" — no document content is ever
    # dropped by the page cap.
    all_pages = render_pdf_pages(Path("docs/examples/samples/contract/contract_03_service_agreement.pdf"), cap=0)
    assert len(all_pages) == 52  # the 52-page transition services agreement


class _TestAgent(BaseAgent):
    agent_name = "sorter"
    client = object()
    model = "test-model"

    def system_prompt(self) -> str:
        return "test"


def test_multimodal_content_built_for_vision(page_images):
    agent = _TestAgent()
    agent.model = "qwen/qwen3.7-flash"  # vision-capable
    content = agent._build_multimodal("Read this contract.", page_images)
    assert isinstance(content, list)
    kinds = [part["type"] for part in content]
    assert kinds == ["text"] + ["image_url"] * len(page_images)
    assert content[1]["image_url"]["url"] == page_images[0]


def test_non_vision_model_ignores_pages(page_images):
    agent = _TestAgent()
    agent.model = "deepseek/deepseek-v4-flash"  # NOT vision-capable
    content = agent._build_multimodal("Read this contract.", page_images)
    assert content == "Read this contract."


def test_empty_pages_stays_string(pdf_fixture):
    agent = _TestAgent()
    agent.model = "qwen/qwen3.7-flash"
    content = agent._build_multimodal("Read this contract.", None)
    assert content == "Read this contract."


def test_sorter_sends_pages_when_vision(page_images, mock_langchain_llm):
    from agents.sorter import SorterAgent

    seen = {}
    orig_run = mock_langchain_llm._run

    def _run_with_capture(messages):
        seen["messages"] = messages
        return orig_run(messages)

    mock_langchain_llm._run = _run_with_capture

    sorter = SorterAgent()
    doc_type, contract_subtype, conf, _ = sorter.classify(
        "ignored transcription text", pages=page_images
    )

    assert doc_type == "contract"
    assert conf == 0.95
    human = seen["messages"][-1]
    content = human.content
    assert isinstance(content, list)
    assert len(content) == 1 + len(page_images)  # text + images


def test_ingest_produces_doc_pages(tmp_path, temp_base_dir):
    from graph.build_graph import ingest_node

    from scripts.prepare_samples import generate_pdf_from_text

    src = tmp_path / "doc.txt"
    src.write_text("This is a court opinion. The court holds for the appellant.", encoding="utf-8")
    pdf = tmp_path / "opinion.pdf"
    generate_pdf_from_text(src, pdf)

    state = {"file_path": str(pdf), "matter_id": "M-VISION"}
    out = ingest_node(state)
    assert out["doc_pages"]
    assert out["doc_pages"][0].startswith("data:image/png;base64,")
    assert out["doc_text"]
