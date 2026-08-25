"""PDF transcription agent — converts PDF documents to markdown for downstream agents.

Uses multiple strategies in priority order:
1. Direct PDF parsing (pypdf, pdfplumber) for text-based PDFs
2. LLM-based transcription for complex/scanned PDFs
3. pdftotext CLI as fallback
"""
import structlog
from pathlib import Path
from agents.base import BaseAgent
from llm.prompt_doctrine import PDF_TRANSCRIBER as _PRODUCTION_DOCTRINE
from llm.prompts import get_managed_prompt
from llm.retry import retry_chat_completion
from observability.tracing import langfuse_call_attrs

logger = structlog.get_logger(__name__)

# Below this many chars, just return the raw text — no need for an LLM pass.
_DIRECT_MIN_CHARS = 500

SYSTEM_PROMPT_V0 = """You are a legal document transcriber. Your job is to convert the raw text
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
8. Return only the cleaned markdown transcription. Do not add a confidence score,
   commentary, or a summary; the pipeline records transcription confidence separately."""

SYSTEM_PROMPT = SYSTEM_PROMPT_V0.rstrip() + "\n\n" + _PRODUCTION_DOCTRINE


class PDFTranscriber(BaseAgent):
    agent_name = "pdf_transcriber"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
        return text

    def transcribe(self, file_path: Path) -> dict:
        raw_text, pages = self._extract_raw_text(file_path)
        if not raw_text or not raw_text.strip():
            return {"markdown": f"[PDF: {file_path.name} — no extractable text]", "confidence": 0.0}

        if len(raw_text) < _DIRECT_MIN_CHARS or self._looks_clean_text_pdf(raw_text, pages):
            logger.info(
                "pdf_direct_no_llm",
                file=file_path.name,
                chars=len(raw_text),
                pages=pages,
            )
            return {"markdown": raw_text, "text": raw_text, "confidence": 0.8, "method": "direct"}

        # Vision-capable pipelines render the PDF pages as images for the
        # downstream agents, so a separate LLM transcription pass is redundant
        # (and expensive). Keep the raw direct extraction as `doc_text` for
        # text-only processes / audit, but skip the reformat call.
        try:
            from llm.vision import pipeline_uses_vision
        except Exception:
            pipeline_uses_vision = lambda: False  # noqa: E731
        if pipeline_uses_vision():
            logger.info(
                "pdf_llm_pass_skipped_for_vision",
                file=file_path.name,
                chars=len(raw_text),
                hint="pipeline is vision-capable; page images carry the content",
            )
            return {"markdown": raw_text, "text": raw_text, "confidence": 0.8, "method": "direct-vision"}

        try:
            markdown = self._llm_transcribe(raw_text, file_path.name)
            if markdown and len(markdown) > 100:
                logger.info("pdf_llm_transcribed", file=file_path.name, chars=len(markdown))
                return {"markdown": markdown, "text": raw_text, "confidence": 0.85, "method": "llm"}
        except Exception:
            logger.exception("pdf_llm_transcription_failed", file=str(file_path))

        return {"markdown": raw_text, "text": raw_text, "confidence": 0.75, "method": "direct"}

    def _looks_clean_text_pdf(self, raw_text: str, pages: int) -> bool:
        """Heuristic: if a PDF yields a dense, clean text extraction, the LLM
        reformat pass adds little value and costs ~30-40s. Scanned/garbled PDFs
        extract sparsely and still get the LLM pass."""
        if not pages or pages <= 0:
            return False
        try:
            from pipeline.config import load_config
            threshold = load_config().get("pipeline", {}).get("pdf_direct_chars_per_page", 800)
        except Exception:
            threshold = 800
        density = len(raw_text) / pages
        return density >= threshold

    def _extract_raw_text(self, file_path: Path) -> tuple[str, int]:
        text = ""
        pages = 0

        try:
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                extracted_pages = []
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        extracted_pages.append(page_text)
                text = "\n\n".join(extracted_pages)
                pages = len(extracted_pages)
            if text.strip():
                logger.info("pdf_text_extracted", method="pdfplumber", pages=pages, chars=len(text))
                return text, pages
        except ImportError:
            logger.debug("pdfplumber not available")
        except Exception:
            logger.exception("pdfplumber_failed", file=str(file_path))

        try:
            import pypdf
            reader = pypdf.PdfReader(file_path)
            extracted_pages = []
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    extracted_pages.append(page_text)
            text = "\n\n".join(extracted_pages)
            pages = len(extracted_pages)
            if text.strip():
                logger.info("pdf_text_extracted", method="pypdf", pages=pages, chars=len(text))
                return text, pages
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
                return text, 0
        except FileNotFoundError:
            logger.debug("pdftotext not available (install poppler-utils)")
        except Exception:
            logger.exception("pdftotext_failed", file=str(file_path))

        return text, pages

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

        kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt()},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": self._configured_max_tokens(),
            "temperature": 0.1,
        }
        kwargs.update(langfuse_call_attrs("pdf-transcriber"))
        if getattr(self, "_langfuse_prompt", None) is not None:
            kwargs["langfuse_prompt"] = self._langfuse_prompt
        from pipeline.limits import get_run_deadline, record_usage

        kwargs["run_deadline"] = get_run_deadline()
        response = retry_chat_completion(self.client, **kwargs)
        record_usage(getattr(response, "usage", None), self.model)
        return response.choices[0].message.content or ""


def transcribe_pdf(file_path: Path) -> dict:
    transcriber = PDFTranscriber()
    return transcriber.transcribe(file_path)
