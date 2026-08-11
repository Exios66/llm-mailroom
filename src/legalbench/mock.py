"""Deterministic mock model for LegalBench runs (no network, no OpenAI).

Answers are derived from a stable hash of the row content, so mock runs are
reproducible (same n/seed -> same answers) and exercise the scoring math with
realistic misses (binary ~50% accuracy, family ~1/N + other-fallback). Used
by ``--mock`` and the test suite; never shipped as real results.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional


def _hash(*parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(digest, 16)


class MockLegalBenchModel:
    """Implements the LegalBenchAgent interface deterministically."""

    model = "mock/mock-legalbench"

    def __init__(self, classes: tuple[str, ...] = ("yes", "no"), seed: int = 42) -> None:
        self._classes = tuple(classes)
        self._seed = seed
        self._last_usage: dict[str, Any] = {}

    def answer_binary(self, question: str, document_text: str) -> dict[str, Any]:
        h = _hash(str(self._seed), question[:200], document_text[:200])
        self._last_usage = {
            "prompt_tokens": 1200,
            "completion_tokens": 60,
            "total_tokens": 1260,
            "cost": None,
        }
        return {
            "answer": "yes" if h % 2 == 0 else "no",
            "evidence": "mock evidence (deterministic baseline)",
            "confidence": 0.5,
        }

    def classify_family(self, document_text: str) -> dict[str, Any]:
        h = _hash(str(self._seed), "family", document_text[:200])
        self._last_usage = {
            "prompt_tokens": 1500,
            "completion_tokens": 80,
            "total_tokens": 1580,
            "cost": None,
        }
        if h % 7 == 0:
            family = "other"
        else:
            family = self._classes[h % len(self._classes)] if self._classes else "other"
        return {"family": family, "reasoning": "mock reasoning (deterministic baseline)", "confidence": 0.5}

    def usage(self) -> dict[str, Any]:
        usage = self._last_usage or {}
        return {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "cost": usage.get("cost"),
        }

    def last_usage(self) -> Optional[dict[str, Any]]:
        return self._last_usage
