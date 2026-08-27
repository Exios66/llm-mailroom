"""Image extraction agent — uses vision-capable LLMs to extract text from images.

Supports both local (Ollama vision models) and cloud (OpenRouter/gpt-4o) providers.
For non-vision LLMs, falls back to a descriptive message indicating the image type.
"""
import base64
import structlog
from pathlib import Path
from agents.base import BaseAgent
from llm.prompt_doctrine import IMAGE_EXTRACTOR as _PRODUCTION_DOCTRINE
from llm.prompts import get_managed_prompt
from observability.tracing import langfuse_call_attrs

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT_V0 = """You are an expert document image analyst. Your task is to extract all visible text
from images of legal documents. This could be scanned contracts, photographed correspondence,
screenshots of filings, or any image containing legal text.

Rules:
1. Extract ALL visible text exactly as it appears.
2. Preserve the document structure including headings, paragraphs, and signatures.
3. If text is partially obscured, note it as [illegible].
4. If the image contains a table or form, render it in markdown table format when possible.
5. If no text is present (e.g., a photo of a person or office), state that clearly.
6. Do not interpret or analyze the content — just transcribe.
7. Include a confidence score for the extraction quality."""

SYSTEM_PROMPT = SYSTEM_PROMPT_V0.rstrip() + "\n\n" + _PRODUCTION_DOCTRINE


class ImageExtractor(BaseAgent):
    agent_name = "image_extractor"

    def system_prompt(self) -> str:
        text, self._langfuse_prompt = get_managed_prompt(self.agent_name, SYSTEM_PROMPT)
        return text

    def extract(self, file_path: Path) -> dict:
        ext = file_path.suffix.lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif",
            ".bmp": "image/bmp", ".webp": "image/webp",
            ".tiff": "image/tiff", ".tif": "image/tiff",
        }
        mime_type = mime_map.get(ext, "image/jpeg")

        try:
            image_data = file_path.read_bytes()
            b64 = base64.b64encode(image_data).decode("utf-8")
            data_uri = f"data:{mime_type};base64,{b64}"

            return self._extract_with_vision(data_uri, file_path.name)
        except Exception:
            logger.exception("image_read_failed", file=str(file_path))
            return {"text": f"[Image file: {file_path.name} — could not be read]", "confidence": 0.0}

    def _extract_with_vision(self, data_uri: str, filename: str) -> dict:
        try:
            # Route through the shared retry/deadline/usage plumbing so a
            # transient provider error is retried (not silently downgraded to
            # the fallback marker) and the call counts toward run limits and
            # cost/token scores. Raises on persistent failure — the caller
            # (`graph/build_graph.py:_extract_text_from_image`) catches and
            # routes the document to review.
            from llm.retry import retry_chat_completion
            from pipeline.limits import get_run_deadline, record_usage

            response = retry_chat_completion(
                self.client,
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt_with_skills()},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"Extract all text from this legal document image: {filename}",
                            },
                            {"type": "image_url", "image_url": {"url": data_uri}},
                        ],
                    },
                ],
                max_tokens=self._configured_max_tokens(),
                run_deadline=get_run_deadline(),
                **langfuse_call_attrs(self.agent_name),
                **(
                    {"langfuse_prompt": self._langfuse_prompt}
                    if getattr(self, "_langfuse_prompt", None) is not None
                    else {}
                ),
            )
            record_usage(getattr(response, "usage", None), self.model)
            text = response.choices[0].message.content or ""
            logger.info("vision_extraction_complete", filename=filename, chars=len(text))
            return {"text": text, "confidence": 0.85, "method": "vision"}
        except Exception as e:
            logger.warning("vision_call_failed", filename=filename, error=str(e))
            raise

    def _fallback_extract(self, filename: str) -> dict:
        text = (
            f"[Image file: {filename}]\n"
            "This document was provided as an image. Automatic text extraction was attempted "
            "but could not be completed. The image may require manual review or a vision-capable "
            "LLM provider for automated transcription."
        )
        logger.info("image_fallback_used", filename=filename)
        return {"text": text, "confidence": 0.1, "method": "fallback"}
