import pytest

pytest.importorskip("pypdf")
pytest.importorskip("reportlab")
pytest.importorskip("pdfplumber")

from scripts.prepare_samples import generate_pdf_from_text  # noqa: E402


def test_generated_pdf_transcribes_short_text(tmp_path):
    src = tmp_path / "doc.txt"
    src.write_text(
        "THIS IS A TEST AGREEMENT between Alpha and Beta. Effective January 1 2025. Term of two years.",
        encoding="utf-8",
    )
    pdf = tmp_path / "doc.pdf"
    generate_pdf_from_text(src, pdf)
    assert pdf.exists() and pdf.stat().st_size > 0

    from agents.pdf_transcriber import PDFTranscriber

    transcriber = PDFTranscriber()
    transcriber.client = object()  # short text (<500 chars) returns without an LLM call
    transcriber.model = "test-model"
    result = transcriber.transcribe(pdf)
    assert result["confidence"] > 0
    text = (result.get("markdown") or result.get("text") or "")
    assert "Alpha" in text


def test_prepare_samples_materializes_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILROOM_BASE_DIR", str(tmp_path))
    from pathlib import Path as _Path

    from scripts.prepare_samples import REAL_CONTRACTS_DIR

    if not REAL_CONTRACTS_DIR.is_dir():
        pytest.skip(
            "docs/examples/samples/ absent (pruned heavy asset; see upstream repo)"
        )
    from scripts.prepare_samples import prepare_samples

    samples_dir = prepare_samples(tmp_path)
    assert samples_dir.exists()
    assert list(samples_dir.rglob("*.pdf"))
