"""PDF transcription agent — converts PDF documents to markdown for downstream agents.

Uses multiple strategies in priority order:
1. Direct PDF parsing (pypdf, pdfplumber) for text-based PDFs
2. LLM-based transcription for complex/scanned PDFs
3. pdftotext CLI as fallback
"""
import structlog
from pathlib import Path
from agents.base import BaseAgent

logger = structlog.get_logger(__name__)


class PDFTranscriber(BaseAgent):
    agent_name = "sorter"

    def system_prompt(self) -> str:
        return """You are a legal document transcriber. Your job is to convert the raw text
extracted from a PDF into clean, well-structured markdown suitable for downstream legal
document analysis agents.

Rules:
1. Preserve the original document structure — headings, sections, paragraphs.
2. Use markdown formatting: # for titles, ## for sections, **bold** for emphasized text.
3. If the document contains tables, format them as markdown tables.
4. If the document has signatures, preserve the signature blocks.
5. Do not add, remove, or alter any facts — only format and structure.
6. If the original text extraction garbled certain sections, note it as [corrupted text].
7. Remove PDF artifact text (page numbers, headers/footers that are clearly metadata).
8. Include a confidence score for the transcription quality."""

    def transcribe(self, file_path: Path) -> dict:
        raw_text = self._extract_raw_text(file_path)
        if not raw_text or not raw_text.strip():
            return {"markdown": f"[PDF: {file_path.name} — no extractable text]", "confidence": 0.0}

        if len(raw_text) < 500:
            return {"markdown": raw_text, "text": raw_text, "confidence": 0.8, "method": "direct"}

        try:
            markdown = self._llm_transcribe(raw_text, file_path.name)
            if markdown and len(markdown) > 100:
                logger.info("pdf_llm_transcribed", file=file_path.name, chars=len(markdown))
                return {"markdown": markdown, "text": raw_text, "confidence": 0.85, "method": "llm"}
        except Exception:
            logger.exception("pdf_llm_transcription_failed", file=str(file_path))

        return {"markdown": raw_text, "text": raw_text, "confidence": 0.75, "method": "direct"}

    def _extract_raw_text(self, file_path: Path) -> str:
        text = ""

        try:
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                pages = []
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        pages.append(page_text)
                text = "\n\n".join(pages)
            if text.strip():
                logger.info("pdf_text_extracted", method="pdfplumber", pages=len(pages), chars=len(text))
                return text
        except ImportError:
            logger.debug("pdfplumber not available")
        except Exception:
            logger.exception("pdfplumber_failed", file=str(file_path))

        try:
            import pypdf
            reader = pypdf.PdfReader(file_path)
            pages = []
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    pages.append(page_text)
            text = "\n\n".join(pages)
            if text.strip():
                logger.info("pdf_text_extracted", method="pypdf", pages=len(pages), chars=len(text))
                return text
        except ImportError:
            logger.debug("pypdf not available")
        except Exception:
            logger.exception("pypdf_failed", file=str(file_path))

        try:
            import subprocess, tempfile
            with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as tmp:
                tmp_path = tmp.name
            result = subprocess.run(
                ["pdftotext", "-layout", str(file_path), tmp_path],
                capture_output=True, text=True, timeout=30
            )
            text = Path(tmp_path).read_text(errors="replace")
            Path(tmp_path).unlink(missing_ok=True)
            if text.strip():
                logger.info("pdf_text_extracted", method="pdftotext", chars=len(text))
                return text
        except FileNotFoundError:
            logger.debug("pdftotext not available (install poppler-utils)")
        except Exception:
            logger.exception("pdftotext_failed", file=str(file_path))

        return text

    def _llm_transcribe(self, raw_text: str, filename: str) -> str:
        max_chars = 16000
        truncated = raw_text[:max_chars]
        if len(raw_text) > max_chars:
            truncated += f"\n\n[... PDF content truncated, {len(raw_text)} total characters ...]"

        user_message = (
            f"Convert the following raw PDF text extraction into clean markdown.\n"
            f"Filename: {filename}\n\n"
            f"--- RAW TEXT ---\n{truncated}\n--- END RAW TEXT ---"
        )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_prompt()},
                {"role": "user", "content": user_message},
            ],
            max_tokens=8192,
            temperature=0.1,
        )
        return response.choices[0].message.content or ""


def transcribe_pdf(file_path: Path) -> dict:
    transcriber = PDFTranscriber()
    return transcriber.transcribe(file_path)
