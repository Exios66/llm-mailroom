# VENDORED from github.com/Exios66/llm-entity-extraction (verified against commit 3a03d5c, 2026-08-10).
# Imported verbatim (import paths rewritten to ``langchain_agents.*``) so the
# eval-validated LangChain sorter/contracts-specialist agents run inside the
# mailroom. Local adaptations (pages/vision, usage/deadline hooks) are marked
# ``MAILROOM PATCH``. Keep diffs against upstream small and documented.


"""Document classification via OpenRouter vision models.

Adapted from the RVL-CDIP classifier — sends document images to a vision LLM
for one-of-N class prediction. Includes confidence extraction and runner-up tracking.
"""

from __future__ import annotations

import base64
import re
import os
from pathlib import Path
from typing import Union

import requests

from langchain_agents.openrouter_utils import OPENROUTER_API_URL


def _valid_classes() -> list[str]:
    """Live taxonomy classes plus the ``unknown`` routing token.

    MAILROOM PATCH: ``unknown`` is a sorter/reviewer label, not a specialist.
    """
    try:
        from pipeline.config import get_sorter_label_set

        return sorted(get_sorter_label_set())
    except Exception:
        return [
            "compliance_filing",
            "contract",
            "corporate_record",
            "correspondence",
            "insurance_claim",
            "unknown",
        ]


VALID_CLASSES = _valid_classes()


def clean_prediction(text: Union[str, None]) -> str:
    """Extract valid class name from LLM response using word boundary matching."""
    if not text:
        return ""
    text = text.strip().lower()
    tagged = re.search(r"<label>\s*([^<\s][^<]*?)\s*</label>", text, flags=re.DOTALL)
    # MAILROOM PATCH: an explicit <label> is the model's committed answer —
    # honor it even when it is `unknown` or a retired/hallucinated class so
    # after_classify can park it. Fuzzy word-boundary matching below still
    # requires VALID_CLASSES so a random mention of "contract" in reasoning
    # does not win over an explicit tag.
    if tagged:
        return tagged.group(1).strip().lower()
    for line in reversed(text.splitlines()):
        candidate = line.strip().strip("`*_ ").lower()
        if candidate in VALID_CLASSES:
            return candidate
    for cls in VALID_CLASSES:
        if re.search(r'\b' + re.escape(cls) + r'\b', text):
            return cls
    return text


def extract_runner_up(text: str) -> str:
    """Extract the model's runner-up (second-choice) label from the reasoning trace."""
    if not text:
        return ""
    marker = re.search(r"(?i)runner[- ]?up\s*:?\s*(.+)", text)
    if not marker:
        return ""
    remainder = marker.group(1).lower()
    candidates = [
        (match.start(), cls)
        for cls in VALID_CLASSES
        for match in [re.search(r"\b" + re.escape(cls) + r"\b", remainder)]
        if match
    ]
    if not candidates:
        return ""
    return min(candidates, key=lambda pair: pair[0])[1]


def extract_reasoning(text: str) -> str:
    """Extract the model's reasoning from a ``<reasoning>...</reasoning>`` tag.

    Used by the vision sorter prompt (RVL-CDIP-style tag output). Falls back
    to the last non-empty line when the tag is absent.
    """
    if not text:
        return ""
    tag = re.search(r"<reasoning>\s*(.*?)\s*</reasoning>", text, flags=re.DOTALL | re.IGNORECASE)
    if tag:
        return tag.group(1).strip().strip('"')
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if candidate:
            return candidate[:500]
    return ""


def extract_confidence(text: str) -> Union[float, None]:
    """Extract the model's self-reported confidence (0-1) from a response."""
    if not text:
        return None
    tag = re.search(r"<confidence>\s*(\d{1,3})\s*</confidence>", text, flags=re.IGNORECASE)
    if tag:
        value = int(tag.group(1))
        return float(max(0, min(100, value))) / 100.0
    for line in reversed(text.splitlines()):
        line = line.strip()
        if re.fullmatch(r"\d{1,3}", line):
            value = int(line)
            if 0 <= value <= 100:
                return value / 100.0
    return None


def classify_image(api_key: str, image_path: Path, model: str = "qwen/qwen3.7-flash", prompt: str = "") -> dict:
    """Classify a document image using a vision model through OpenRouter API.

    Args:
        api_key: OpenRouter API key.
        image_path: Path to the document image file.
        model: Model identifier (default: qwen/qwen3.7-flash).
        prompt: Classification prompt text.

    Returns:
        Dict with status, classification, raw_response, model, usage.
    """
    if not prompt:
        raise ValueError("prompt is required for classification")

    with open(image_path, "rb") as f:
        image_base64 = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{image_base64}"
                }},
            ]},
        ],
        "max_tokens": 4096,
        "temperature": 0.1,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    response = requests.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=120)

    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        try:
            error_body = response.json()
        except Exception:
            error_body = response.text
        # O-6: never print the full provider error body (it can echo document
        # content back); log structure + status only, keep the body for DEBUG.
        import structlog

        structlog.get_logger(__name__).warning(
            "openrouter_api_error",
            status_code=response.status_code,
            error_body_truncated=str(error_body)[:120],
        )
        raise

    result = response.json()
    try:
        prediction = result["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, AttributeError):
        prediction = ""

    cleaned = clean_prediction(prediction)

    return {
        "status": "success" if cleaned else "empty_response",
        "classification": cleaned,
        "raw_response": prediction,
        "model": model,
        "usage": result.get("usage", {}),
    }
