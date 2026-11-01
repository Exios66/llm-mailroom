"""Dojo-owned sandbox MAUD/CUAD benchmark rubrics, additive to current suites.

Source-owned adoption from sandbox 872b081, never from a hub package copy.
These legacy-compatible rubrics have distinct metric identities: MAUD uses
all labeled questions and also reports its frozen clean subset; CUAD uses
category detection within each row's labeled universe. They do not replace
content_scoring.score_maud_extraction or field_scoring.score_category_presence,
whose populations and matching policies differ. Consumers only adapt rows.

Existing result keys and zero/null conventions are retained. This transfer
preserves the sandbox rubric; it does not certify ambiguous GT as clean gold.
The metric identities keep historical scores separate from canonical clean-GT
scores in existing Dojo entry points.
"""

from __future__ import annotations

from .classification import fbeta

SANDBOX_MAUD_METRIC_ID = "maud.sandbox_labeled.micro_accuracy"
SANDBOX_MAUD_CLEAN_METRIC_ID = "maud.sandbox_clean_subset.micro_accuracy"
SANDBOX_CUAD_METRIC_ID = "cuad.sandbox_category_detection.micro_f1"

import json
import re
from typing import Any, Mapping

TYPE_OF_CONSIDERATION = "Type of Consideration"
_CONSIDERATION_ENUM = {
    "all_cash": "All Cash",
    "all_stock": "All Stock",
    "mixed_cash_stock": "Mixed Cash/Stock",
    "mixed_cash_stock_election": "Mixed Cash/Stock: Election",
}
_NON_ANSWERS = {
    "", "none stated", "not specified", "not answered", "not stated", "unanswered",
    "n/a", "na", "unknown", "not found", "not applicable", "null", "none",
}


# Questions whose corpus answers all come from ONE MAUD sub-question. The Hub
# rows collapse several sub-questions under names like "No-Shop" / "MAE
# Definition" (answers "Yes", "Strict liability", … for different
# sub-questions), so only this subset has an unambiguous target per doc.
CLEAN_QUESTIONS = frozenset({
    "Type of Consideration", "General Antitrust Efforts Standard",
    "Compliance with Covenant Closing Condition", "Specific Performance",
    "Agreement provides for matching rights in connection with COR",
    "Agreement provides for matching rights in connection with FTR",
    "Limitations on FTR Exercise", "Absence of Litigation Closing Condition",
    "Accuracy of Target R&W Closing Condition", "Breach of Meeting Covenant",
    "Breach of No Shop", "FTR Triggers",
})


def norm(text: Any) -> str:
    """Normalize the frozen sandbox MAUD answer surface."""
    s = str(text or "").lower().replace("_", " ").replace("fundermental", "fundamental")
    s = re.sub(r"[\"'“”‘’`]", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .;:-")
    return s


def _multi_class_questions() -> frozenset[str]:
    """Questions whose catalog holds multi-select answers.

    A question is multi-select when some catalog class is a comma-joined list
    that includes another catalog class (``"General R&Ws, …"``). Only these
    compare comma-joined answers as sets; elsewhere order is meaningful.
    """
    from .maud import MAUD_ANSWER_CLASSES

    out = set()
    for question, classes in MAUD_ANSWER_CLASSES.items():
        singles = {norm(c) for c in classes}
        for c in classes:
            parts = [norm(p) for p in str(c).split(",") if p.strip()]
            if len(parts) > 1 and any(p in singles for p in parts):
                out.add(question)
                break
    return frozenset(out)


MULTI_CLASS_QUESTIONS = _multi_class_questions()


def _catalog_norms() -> dict[str, frozenset[str]]:
    """Return per-question canonical catalog answer surfaces."""
    from .maud import MAUD_ANSWER_CLASSES

    return {q: frozenset(norm(c) for c in cs) for q, cs in MAUD_ANSWER_CLASSES.items()}


_CATALOG_NORMS = _catalog_norms()


def is_non_answer(answer: Any, question: str | None = None) -> bool:
    """Missing / placeholder answer. A placeholder that is itself a catalog
    class for *question* (``"None"`` on the matching-rights and fiduciary
    questions) is a real answer."""
    na = norm(answer)
    return na in _NON_ANSWERS and na not in _CATALOG_NORMS.get(question or "", ())


def same_answer(a: Any, b: Any, question: str | None = None) -> bool:
    """Exact match after ``norm``; comma-joined answers compare as sets only
    for a multi-select *question* (:data:`MULTI_CLASS_QUESTIONS`)."""
    na, nb = norm(a), norm(b)
    if na == nb:
        return True
    if question not in MULTI_CLASS_QUESTIONS or "," not in na:
        return False
    parts = lambda v: sorted(p.strip() for p in v.split(",") if p.strip())  # noqa: E731
    return parts(na) == parts(nb)


def gt_labels(raw: Any) -> dict[str, str]:
    """``maud_clause_labels`` (JSON string or mapping) → {question: answer}."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, Mapping):
        return {}
    return {q: str(v.get("answer", "")) for q, v in raw.items()
            if isinstance(v, Mapping) and v.get("answer") is not None}


CONFLICT = "\x00conflict"


def _reduce(answers: list[str], question: str | None = None) -> str:
    """One answer per question from a chunk-merged list (SAND-042 RCA).

    Chunked runs answer every question on every chunk, so the preamble chunk
    contributes guesses / "not specified" before the chunk holding the
    clause. Position must not decide: drop non-answers, and when distinct
    real answers remain the question is a conflict (scored wrong, counted).
    """
    real: list[str] = []
    for a in answers:
        if not is_non_answer(a, question) and not any(same_answer(a, r, question) for r in real):
            real.append(a)
    if not real:
        return answers[0] if answers else ""
    return real[0] if len(real) == 1 else CONFLICT


def parse_maud_answers(pred: Mapping[str, Any], questions: list[str]) -> dict[str, str]:
    """Map the specialist's ``maud_clauses`` onto known question names.

    Entries are '<Question>: <Answer>'. Question names can themselves contain
    colons, so each entry is matched by normalized prefix against ``questions``
    (longest first) rather than split on the first colon. ``merger_consideration``
    backfills Type of Consideration when maud_clauses does not answer it.
    """
    ordered = sorted(questions, key=lambda q: -len(norm(q)))
    seen: dict[str, list[str]] = {}
    clauses = pred.get("maud_clauses") or []
    if isinstance(clauses, Mapping):
        # {question: null} is the object grammar's "not answered".
        clauses = [f"{k}: {v}" for k, v in clauses.items() if v is not None]
    for entry in clauses if isinstance(clauses, list) else []:
        text = str(entry)
        for q in ordered:
            nq = norm(q)
            # walk the raw entry until its normalized prefix covers the question
            for cut in range(len(nq), len(text) + 1):
                if norm(text[:cut]) == nq and (cut == len(text) or text[cut] in ": -–—"):
                    answer = text[cut:].lstrip(" :-–—").strip()
                    if norm(answer) == nq:  # echoed the question name — no answer
                        answer = ""
                    seen.setdefault(q, []).append(answer)
                    break
            else:
                continue
            break
    out = {q: _reduce(vals, q) for q, vals in seen.items()}
    if TYPE_OF_CONSIDERATION in questions and is_non_answer(out.get(TYPE_OF_CONSIDERATION), TYPE_OF_CONSIDERATION):
        enum = _CONSIDERATION_ENUM.get(str(pred.get("merger_consideration") or "").strip().lower())
        if enum:
            out[TYPE_OF_CONSIDERATION] = enum
    return out


def score_maud(pred: Mapping[str, Any], maud_clause_labels: Any) -> dict[str, Any]:
    """Score the sandbox MAUD all-label and frozen-clean populations."""
    gt = gt_labels(maud_clause_labels)
    answers = parse_maud_answers(pred or {}, list(gt))
    answered = {q: a for q, a in answers.items() if not is_non_answer(a, q)}
    conflicts = sum(1 for a in answered.values() if a == CONFLICT)
    correct = sum(1 for q, a in answered.items() if same_answer(a, gt[q], q))
    n = len(gt)
    clean = [q for q in gt if q in CLEAN_QUESTIONS]
    clean_correct = sum(1 for q in clean if q in answered and same_answer(answered[q], gt[q], q))
    return {
        "maud_clean_questions": len(clean),
        "maud_clean_correct": clean_correct,
        "maud_clean_accuracy": (clean_correct / len(clean)) if clean else None,
        "maud_questions": n,
        "maud_answered": len(answered),
        "maud_conflicts": conflicts,
        "maud_correct": correct,
        "maud_accuracy": (correct / n) if n else None,
        "maud_precision_answered": (correct / len(answered)) if answered else None,
    }


# schema field → CUAD metadata category it evidences
FIELD_CATEGORY = {
    "document_name": "Document Name",
    "parties": "Parties",
    "effective_date": "Effective Date",
    "governing_law": "Governing Law",
    "term_length": "Expiration Date",
    "renewal_terms": "Renewal Term",
}
VALUE_FIELDS = ("document_name", "parties", "governing_law")


def _norm(text: Any) -> str:
    """Normalize a CUAD category or metadata value for this benchmark."""
    s = re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _empty(v: Any) -> bool:
    """Return whether a sandbox metadata value is absent."""
    return v is None or (isinstance(v, (str, list, dict)) and not v)


def gt_categories(raw: Any) -> dict[str, list[str]]:
    """Decode the row-labeled CUAD universe and annotated spans."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, Mapping):
        return {}
    return {k: [str(s.get("text", "")) for s in (v or []) if isinstance(s, Mapping)]
            for k, v in raw.items()}


def predicted_categories(pred: Mapping[str, Any], universe: list[str]) -> tuple[set[str], int]:
    """Resolve category heads and schema fields within the row universe."""
    by_norm = {_norm(c): c for c in universe}
    found: set[str] = set()
    outside = 0
    for entry in pred.get("cuad_clauses") or []:
        head = _norm(str(entry).split(":", 1)[0])
        if head in by_norm:
            found.add(by_norm[head])
        else:
            outside += 1
    for field, cat in FIELD_CATEGORY.items():
        if cat in universe and not _empty(pred.get(field)):
            found.add(cat)
    return found, outside


def _value_ok(field: str, value: Any, spans: list[str]) -> bool:
    """Check the sandbox metadata containment rubric against GT spans."""
    blob = " ".join(_norm(s) for s in spans)
    if field == "parties":
        names = [_norm(v) for v in (value or [])] if isinstance(value, list) else [_norm(value)]
        return bool(names) and all(n and n in blob for n in names)
    v = _norm(value)
    return bool(v) and (v in blob or any(_norm(s) in v for s in spans if _norm(s)))


def score_cuad(pred: Mapping[str, Any], cuad_clause_labels: Any) -> dict[str, Any]:
    """Score category detection and metadata checks with unchanged keys."""
    gt = gt_categories(cuad_clause_labels)
    universe = list(gt)
    present = {c for c, spans in gt.items() if spans}
    found, outside = predicted_categories(pred or {}, universe)
    tp, fp, fn = len(found & present), len(found - present), len(present - found)
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = fbeta(p, r, beta=1.0)
    checked = correct = 0
    for field in VALUE_FIELDS:
        cat = FIELD_CATEGORY[field]
        if gt.get(cat) and not _empty((pred or {}).get(field)):
            checked += 1
            correct += _value_ok(field, pred[field], gt[cat])
    return {
        "cuad_universe": len(universe), "cuad_present": len(present),
        "cuad_tp": tp, "cuad_fp": fp, "cuad_fn": fn,
        "cuad_presence_precision": round(p, 4), "cuad_presence_recall": round(r, 4),
        "cuad_presence_f1": f1 if universe else None,
        "cuad_out_of_universe": outside,
        "cuad_value_checked": checked, "cuad_value_correct": correct,
    }


__all__ = [
    "score_maud", "score_cuad", "parse_maud_answers", "gt_labels",
    "same_answer", "is_non_answer", "norm", "CLEAN_QUESTIONS",
    "MULTI_CLASS_QUESTIONS", "TYPE_OF_CONSIDERATION", "CONFLICT",
    "gt_categories", "predicted_categories", "FIELD_CATEGORY", "VALUE_FIELDS",
    "SANDBOX_MAUD_METRIC_ID", "SANDBOX_MAUD_CLEAN_METRIC_ID",
    "SANDBOX_CUAD_METRIC_ID",
]


_SANDBOX_METRIC_NAMES = {
    "maud": {
        "maud_accuracy": "sandbox_maud_accuracy",
        "maud_clean_accuracy": "sandbox_maud_clean_accuracy",
        "maud_precision_answered": "sandbox_maud_precision_answered",
    },
    "cuad": {
        "cuad_presence_f1": "sandbox_cuad_presence_f1",
        "cuad_presence_precision": "sandbox_cuad_presence_precision",
        "cuad_presence_recall": "sandbox_cuad_presence_recall",
    },
}


def sandbox_metric_scores(benchmark: str, result: Mapping[str, Any]) -> dict[str, float]:
    """Translate legacy payload keys to strict registry names for emission.

    Counts stay report support, never fake scores. Zero-GT rows emit no quality
    rates. Zero scores on genuinely labeled rows are retained. Unknown benchmark
    names, missing registry entries, and non-finite results raise.
    """
    import math

    from .registry import get_registry
    from .scorecard_honesty import metric_id_for

    try:
        names = _SANDBOX_METRIC_NAMES[benchmark]
    except KeyError:
        raise ValueError(f"unknown sandbox benchmark {benchmark!r}") from None
    population = "maud_questions" if benchmark == "maud" else "cuad_universe"
    if not result.get(population):
        return {}
    out = {}
    for key, canonical in names.items():
        value = result.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"non-numeric sandbox score {key!r}: {value!r}")
        if not math.isfinite(value):
            raise ValueError(f"non-finite sandbox score {key!r}: {value!r}")
        get_registry().get(canonical)
        metric_id_for(canonical)
        out[canonical] = float(value)
    return out


__all__ += ["sandbox_metric_scores"]


def aggregate_sandbox_scores(benchmark: str, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Pool benchmark event counts, preserving micro rather than document means.

    Callers select their report's document population before passing score rows.
    Actual zeros remain zeros; a missing labeled population remains None.
    Producer formats, run-store IO, and prompt lineage are outside this engine.
    """
    if benchmark not in {"maud", "cuad"}:
        raise ValueError(f"unknown sandbox benchmark {benchmark!r}")

    def total(key: str) -> int:
        """Sum one report-support count over the supplied population."""
        return sum(int(row.get(key) or 0) for row in rows)

    if benchmark == "maud":
        questions = total("maud_questions")
        answered = total("maud_answered")
        correct = total("maud_correct")
        clean_questions = total("maud_clean_questions")
        clean_correct = total("maud_clean_correct")
        return {
            "kind": "maud", "questions": questions, "answered": answered,
            "correct": correct, "accuracy": correct / questions if questions else None,
            "coverage": answered / questions if questions else None,
            "precision_answered": correct / answered if answered else None,
            "clean_correct": clean_correct, "clean_questions": clean_questions,
            "clean_accuracy": clean_correct / clean_questions if clean_questions else None,
        }
    tp, fp, fn = total("cuad_tp"), total("cuad_fp"), total("cuad_fn")
    labeled = sum(1 for row in rows if row.get("cuad_presence_f1") is not None)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "kind": "cuad", "docs_labeled": labeled,
        "precision": precision if labeled else None,
        "recall": recall if labeled else None,
        "f1": fbeta(precision, recall, beta=1.0) if labeled else None,
        "value_correct": total("cuad_value_correct"),
        "value_checked": total("cuad_value_checked"),
    }


__all__ += ["aggregate_sandbox_scores"]
