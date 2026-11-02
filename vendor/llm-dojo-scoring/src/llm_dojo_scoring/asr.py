"""Word- and character-error rates for PDF transcription / OCR.

WER and CER are the standard ASR/OCR evaluators: Levenshtein edit
distance over tokens (WER) or characters (CER), divided by the reference
length. Both are **lower-is-better**. ``word_accuracy`` / ``character_accuracy``
are the complementary ``max(0, 1 - error)`` headlines.

Empty reference + empty hypothesis scores 0.0 (perfect). Empty reference
with a non-empty hypothesis scores 1.0 (not infinity). WER may exceed 1.0
when the hypothesis is longer than the reference (insertions); the
accuracy complements are floored at 0.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from ._paired import require_equal_lengths

__all__ = [
    "word_error_rate",
    "character_error_rate",
    "word_accuracy",
    "character_accuracy",
    "score_transcription",
]

_WHITESPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[^\w']+", re.UNICODE)


def _normalize_text(value: Any) -> str:
    """Lowercase text and collapse whitespace, treating falsey values as empty."""
    return _WHITESPACE_RE.sub(" ", str(value or "").strip().lower()).strip()


def _words(value: Any) -> list[str]:
    """Whitespace-split tokens after lowercasing; punctuation is a separator."""
    text = _normalize_text(value)
    if not text:
        return []
    return [tok for tok in _WORD_RE.split(text) if tok]


def _chars(value: Any) -> list[str]:
    """Character sequence after lowercase + whitespace collapse (spaces kept)."""
    return list(_normalize_text(value))


def _levenshtein(ref: Sequence[str], hyp: Sequence[str]) -> int:
    """Classic Wagner–Fischer edit distance (insert / delete / substitute)."""
    n, m = len(ref), len(hyp)
    if n == 0:
        return m
    if m == 0:
        return n
    prev = list(range(m + 1))
    for i, ref_item in enumerate(ref, 1):
        curr = [i] + [0] * m
        for j, hyp_item in enumerate(hyp, 1):
            cost = 0 if ref_item == hyp_item else 1
            curr[j] = min(
                curr[j - 1] + 1,       # insertion
                prev[j] + 1,           # deletion
                prev[j - 1] + cost,    # substitution / match
            )
        prev = curr
    return prev[m]


def _error_rate(ref: Sequence[str], hyp: Sequence[str]) -> float:
    """Return edit distance per reference token, with empty-reference handling."""
    if not ref:
        return 0.0 if not hyp else 1.0
    return _levenshtein(ref, hyp) / len(ref)


def word_error_rate(predicted: Any, expected: Any) -> float:
    """WER = word-level Levenshtein(hyp, ref) / |ref|. Lower is better."""
    return _error_rate(_words(expected), _words(predicted))


def character_error_rate(predicted: Any, expected: Any) -> float:
    """CER = character-level Levenshtein(hyp, ref) / |ref|. Lower is better."""
    return _error_rate(_chars(expected), _chars(predicted))


def word_accuracy(predicted: Any, expected: Any) -> float:
    """``max(0, 1 - WER)`` — bounded complementary headline."""
    return max(0.0, 1.0 - word_error_rate(predicted, expected))


def character_accuracy(predicted: Any, expected: Any) -> float:
    """``max(0, 1 - CER)`` — bounded complementary headline."""
    return max(0.0, 1.0 - character_error_rate(predicted, expected))


def _mean(values: list[float]) -> float:
    """Return the four-decimal mean, or zero for an empty sample."""
    return round(sum(values) / len(values), 4) if values else 0.0


def score_transcription(expected: Any, predicted: Any) -> dict:
    """Batch or single-string WER/CER dict.

    ``expected`` / ``predicted`` are parallel strings, or a single pair of
    strings. Returns mean WER/CER over the run plus complementary accuracies.
    """
    if isinstance(expected, list) and isinstance(predicted, list):
        require_equal_lengths(expected, predicted)
        pairs = list(zip(expected, predicted))
    else:
        pairs = [(expected, predicted)]
    wers = [word_error_rate(p, e) for e, p in pairs]
    cers = [character_error_rate(p, e) for e, p in pairs]
    return {
        "task": "transcription",
        "kind": "transcription",
        "wer": _mean(wers),
        "cer": _mean(cers),
        "word_accuracy": _mean([max(0.0, 1.0 - w) for w in wers]),
        "character_accuracy": _mean([max(0.0, 1.0 - c) for c in cers]),
        "n": len(pairs),
    }
