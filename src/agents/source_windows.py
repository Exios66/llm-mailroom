"""Whole-document coverage for the judge and the arbiters (#277).

A verifier that reads an excerpt cannot vouch for a document: text past the cut
can contradict what it accepts. These helpers split the source into the same
overlapping, paragraph-aware windows the sorter and intake agents use
(``agents.intake.sliding_windows``) so every character is read by some call.

The window size is the calling agent's ``agents.<name>.max_input_chars`` and the
overlap is ``chunking.overlap_chars`` (at most an eighth of the window, as the
specialists clamp it), both from ``taxonomy.yaml``. A document that fits in one
window is sent whole, exactly as before.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
from collections import OrderedDict

from agents.intake import sliding_windows

#: An overlap larger than this share of the window would starve the span budget.
_MAX_OVERLAP_SHARE = 8
_DEFAULT_OVERLAP_CHARS = 8_000


def window_overlap(budget: int) -> int:
    """``chunking.overlap_chars`` from the taxonomy, clamped to an eighth of ``budget``."""
    try:
        from pipeline.config import load_config

        configured = int((load_config().get("chunking") or {}).get("overlap_chars", _DEFAULT_OVERLAP_CHARS))
    except Exception:  # noqa: BLE001 - config trouble must not change what a verifier reads
        configured = _DEFAULT_OVERLAP_CHARS
    return max(0, min(configured, budget // _MAX_OVERLAP_SHARE))


def windows_for_budget(doc_text: str, budget: int) -> list[str]:
    """Every non-blank character of ``doc_text`` in windows of at most ``budget`` characters.

    Always returns at least one window, so a caller that checks "every window" never
    vacuously verifies nothing. A document with no non-blank text (empty, or only whitespace)
    is one empty window, never an over-budget run of blanks.
    """
    budget = max(1, int(budget))
    windows = [w for w, _start in sliding_windows(doc_text or "", budget, window_overlap(budget))]
    return windows or [""]


def source_windows(agent, doc_text: str) -> list[str]:
    """``doc_text`` in windows of ``agent``'s configured ``max_input_chars``."""
    return windows_for_budget(doc_text, agent._configured_max_input_chars())


def window_label(index: int, total: int) -> str:
    """``window 2 of 5`` (1-based), for findings and error messages."""
    return f"window {index} of {total}"


def valid_unit(value: object) -> float | None:
    """``value`` as a float in [0, 1], else None (bools, NaN, inf and out-of-range are invalid)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None


# -- resume cache ------------------------------------------------------------------------------
# A node retry after a transient provider error re-enters the judge or arbiter from the top.
# Without this, every window of every pass would be paid for again, and a long document is the
# one most likely to hit a transient error and exhaust the node's retry budget. Completed
# window results are kept in process, keyed by the document, the pass, the window's position
# and text, the thing being judged (the extraction, or the judge findings) and the model, so
# a retry re-runs only windows that were not finished. Only a usable result is stored, so a
# window that failed to parse is always asked again. The graph node drops a document's entries
# as soon as it returns anything but a transient error; the size bound only guards a crash
# between a transient error and its retry.

_CACHE_MAX_ENTRIES = 2048
_cache: OrderedDict[tuple, dict] = OrderedDict()
_cache_lock = threading.Lock()


def text_digest(value: object) -> str:
    """Short stable digest of text or of any JSON-serializable value."""
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def cached_window(key: tuple) -> dict | None:
    """A copy of the stored result for ``key``, or None."""
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        _cache.move_to_end(key)
        return copy.deepcopy(hit)


def store_window(key: tuple, result: dict) -> None:
    with _cache_lock:
        _cache[key] = copy.deepcopy(result)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)


def forget_document(doc_text: str) -> None:
    """Drop every stored window result of this document (keys lead with the document digest)."""
    digest = text_digest(doc_text or "")
    with _cache_lock:
        for key in [k for k in _cache if k and k[0] == digest]:
            del _cache[key]
