"""Task-aware scoring across the additional document hierarchy.

Issue #19 / KANBAN-047: the CUAD-focused scoring suite is generalized to
cover every task type the eval loop produces — **MAUD** (merger-agreement
doc-class + consideration-type subclass + per-question classification),
**LegalBench** (task-mode binary Yes/No), **chained sorter→extractor runs**
(composite sorter + extractor scoring), **multi classification** (macro/micro
per-class + confusion), and **court opinions** (court_opinion doc-class path).

All functions are deterministic pure functions over ``(predicted, expected)``
pairs so offline rescoring, manifest re-scoring, and live Langfuse/Braintrust
scoring never disagree. Failed rows (``ERROR_PREFIX`` predictions) count as
mismatches in the headline accuracy and are skipped by per-class/confusion
breakdowns — the same convention as :mod:`.classification`.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from ._paired import require_equal_lengths
from .bootstrap import bootstrap_ci
from .classification import (
    ERROR_PREFIX,
    binary_metrics,
    confusion_matrix,
    fbeta,
    macro_accuracy,
    macro_prf,
    normalize_label,
    top_confusions,
)
from .config import (
    CONTRACTEVAL_NO_RELATED_PHRASE,
    CONTRACTEVAL_POSITIVE_DENOMINATOR,
    LEGALBENCH_BINARY_LABELS,
    LEGALBENCH_YES_NO,
    MAUD_CONSIDERATION_ALIASES,
    MAUD_CONSIDERATION_EQUIVALENCES,
    MAUD_CONSIDERATION_TYPES,
    TASK_KINDS,
)
from .equivalences import equivalent_doc_subclasses, normalize_doc_subclass

_ALIAS_RE = re.compile(r"[^a-z0-9]+")


def _fold(value: Any) -> str:
    """Lowercase, non-alphanumerics -> single spaces (for fuzzy matching)."""
    return _ALIAS_RE.sub(" ", str(value).strip().lower()).strip()


def task_kind(task: str) -> str:
    """Resolve a task key to its scoring kind (unknown keys fall back to the
    task name itself → plain label classification)."""
    return TASK_KINDS.get(task, task)


def normalize_maud_consideration(value: Any) -> str:
    """Coerce a raw MAUD consideration answer into the canonical key.

    Handles the MAUD surface ("All Cash", "Mixed Cash & Stock", ...) and the
    docclass eval's canonical snake_case (``all_cash``, ...); unknown values
    degrade to ``other`` (the GT gap convention).
    """
    if value is None:
        return "other"
    folded = _fold(value)
    if folded in MAUD_CONSIDERATION_ALIASES:
        return MAUD_CONSIDERATION_ALIASES[folded]
    for key in MAUD_CONSIDERATION_TYPES:
        if _fold(key) == folded:
            return key
    return "other"


def normalize_legalbench(value: Any) -> str:
    """Coerce a raw LegalBench task answer to the canonical label set."""
    folded = _fold(value)
    for label in LEGALBENCH_BINARY_LABELS:
        if folded == label or folded in LEGALBENCH_YES_NO.get(label, set()):
            return label
    return folded


def normalize_task_answer(task: str, value: Any, valid=None) -> str:
    """Task-aware label normalization (subtype/doc_class go through the
    classification normalizer; MAUD and LegalBench use their own tables)."""
    if task in ("maud_docclass", "maud_question"):
        return normalize_maud_consideration(value)
    kind = task_kind(task)
    if kind == "docclass":
        if valid is not None:
            return normalize_doc_subclass(value, allowed=set(valid))
        return normalize_doc_subclass(value)
    if kind == "legalbench":
        return normalize_legalbench(value)
    return normalize_label(value, valid=valid)


def _per_class(expected: Iterable, predicted: Iterable) -> dict[str, dict]:
    """Per-expected-class exact-match + one-vs-rest P/R/F1/F2.

    Failed rows (``ERROR_PREFIX`` predictions) are skipped, matching
    :func:`.classification.per_class_stats`. Labels are assumed already
    normalized (LegalBench / subclass tokens must not re-enter
    :func:`normalize_label`).
    """
    pairs = [
        (e, p) for e, p in zip(expected, predicted)
        if not str(p).startswith(ERROR_PREFIX)
    ]
    labels = sorted({e for e, _ in pairs})
    by_class: dict[str, dict] = {}
    for cls in labels:
        n = sum(1 for e, _ in pairs if e == cls)
        correct = sum(1 for e, p in pairs if e == cls and p == cls)
        tp = correct
        fp = sum(1 for e, p in pairs if e != cls and p == cls)
        fn = sum(1 for e, p in pairs if e == cls and p != cls)
        precision = round(tp / (tp + fp), 4) if tp + fp else 0.0
        recall = round(tp / (tp + fn), 4) if tp + fn else 0.0
        by_class[cls] = {
            "n": n,
            "correct": correct,
            "accuracy": round(correct / n, 4) if n else 0.0,
            "precision": precision,
            "recall": recall,
            "f1": fbeta(precision, recall, beta=1.0),
            "f2": fbeta(precision, recall, beta=2.0),
        }
    return by_class


def _macros_from_per_class(
    per_class: dict[str, dict],
    *,
    prefix: str = "",
) -> dict[str, float]:
    """Unweighted mean of per-class P/R/F1/F2.

    With ``prefix=""`` also copies macros onto registry T1 names
    ``precision`` / ``recall`` / ``f2`` and T0 ``f1_macro``.
    """
    n_cls = len(per_class)
    if not n_cls:
        zeros = {
            f"{prefix}precision_macro": 0.0,
            f"{prefix}recall_macro": 0.0,
            f"{prefix}f1_macro": 0.0,
            f"{prefix}f2_macro": 0.0,
        }
        if not prefix:
            zeros.update(precision=0.0, recall=0.0, f2=0.0, f1_macro=0.0)
        return zeros
    precision = round(sum(s["precision"] for s in per_class.values()) / n_cls, 4)
    recall = round(sum(s["recall"] for s in per_class.values()) / n_cls, 4)
    f1 = round(sum(s["f1"] for s in per_class.values()) / n_cls, 4)
    f2 = round(sum(s["f2"] for s in per_class.values()) / n_cls, 4)
    out = {
        f"{prefix}precision_macro": precision,
        f"{prefix}recall_macro": recall,
        f"{prefix}f1_macro": f1,
        f"{prefix}f2_macro": f2,
    }
    if not prefix:
        out.update(precision=precision, recall=recall, f2=f2, f1_macro=f1)
    return out


def _ci(per_row: list[float], *, seed: int, n_boot: int):
    """Bootstrap a confidence interval from per-row scores with a fixed seed."""
    return bootstrap_ci(per_row, seed=seed, n_boot=n_boot)


def _label_score(task: str, expected: list, predicted: list, *,
                 valid=None, seed: int, n_boot: int) -> dict:
    """Shared score dict for label-classification kinds (subtype, doc_class,
    multiclass, court_opinion, maud_question)."""
    require_equal_lengths(expected, predicted)
    norm = lambda v: normalize_task_answer(task, v, valid=valid)
    pairs = [(norm(e), str(p) if str(p).startswith(ERROR_PREFIX) else norm(p))
             for e, p in zip(expected, predicted)]
    exp = [e for e, _ in pairs]
    pred = [p for _, p in pairs]
    per_row = [1.0 if not p.startswith(ERROR_PREFIX) and e == p else 0.0
               for e, p in pairs]
    matrix, labels = confusion_matrix(exp, pred)
    exact = round(sum(per_row) / len(per_row), 4) if per_row else 0.0
    per_class = _per_class(exp, pred)
    return {
        "task": task,
        "kind": task_kind(task),
        "exact_match": exact,
        "accuracy": exact,
        "exact_match_ci": _ci(per_row, seed=seed, n_boot=n_boot),
        "macro_accuracy": macro_accuracy(exp, pred),
        "per_class": per_class,
        **_macros_from_per_class(per_class),
        "confusion": {"matrix": matrix, "labels": labels},
        "top_confusions": top_confusions(matrix, labels),
        "n": len(per_row),
    }


def score_task(
    task: str,
    expected: list,
    predicted: list,
    *,
    valid=None,
    expected_subclass: list | None = None,
    predicted_subclass: list | None = None,
    categories: list | None = None,
    positive_denominator: int | None = CONTRACTEVAL_POSITIVE_DENOMINATOR,
    seed: int = 42,
    n_boot: int = 2000,
) -> dict:
    """Score a task over paired per-document answers.

    Args:
        task: task key (subtype | doc_class | docclass | maud_docclass |
            maud_question | maud_extraction | enron_topic | enron_sentiment |
            transcription | legalbench | multiclass | court_opinion |
            contracteval).
        expected / predicted: parallel sequences of per-document answers.
            For ``contracteval``: ``expected`` = list of GT label-span lists
            (empty = category absent) and ``predicted`` = list of raw model
            outputs — the ContractEval (arXiv 2508.03080) rubric applies.
        valid: optional valid-label table (regex patterns for
            classification kinds, or a key set for doc_subclass scoping).
        expected_subclass / predicted_subclass: the second-level doc_subclass
            dimension (consideration type) for ``docclass`` / ``maud_docclass``.
        categories / positive_denominator: ``contracteval``-only — per-category
            breakdown labels and the false-"no related clause" rate denominator
            (paper default 1,244; ``None`` = the run's own positive count).
        seed / n_boot: bootstrap CI parameters.

    Returns a task-appropriate score dict (exact match, per-class, confusion,
    bootstrap CIs; doc_type + subclass for the hierarchical kinds). For
    ``chained`` runs use :func:`chained_composite` / :func:`chained_summary`.
    """
    kind = task_kind(task)

    if kind == "enron_topic":
        from .content_scoring import score_content_topic

        return score_content_topic(expected, predicted)

    if kind == "enron_sentiment":
        from .content_scoring import score_sentiment

        return score_sentiment(expected, predicted)

    if kind == "maud_extraction":
        from .content_scoring import score_maud_extraction

        return score_maud_extraction(expected, predicted)

    if kind == "transcription":
        from .asr import score_transcription

        return score_transcription(expected, predicted)

    if kind == "intake":
        from .intake import score_intake

        return score_intake(expected, predicted)

    if kind != "chained":
        require_equal_lengths(expected, predicted)
    if kind in ("pipeline", "docclass"):
        if expected_subclass is not None and predicted_subclass is not None:
            require_equal_lengths(expected_subclass, predicted_subclass)
            require_equal_lengths(expected, expected_subclass)

    if kind == "contracteval":
        return contracteval_metrics(
            expected, predicted,
            categories=categories,
            positive_denominator=positive_denominator,
        )

    if kind == "legalbench":
        norm = lambda v: normalize_task_answer(task, v)
        pairs = [(norm(e), norm(p)) for e, p in zip(expected, predicted)]
        exp = [e for e, _ in pairs]
        pred = [p for _, p in pairs]
        per_row = [1.0 if e == p else 0.0 for e, p in pairs]
        matrix, labels = confusion_matrix(exp, pred)
        positive = LEGALBENCH_BINARY_LABELS[0]
        per_class = _per_class(exp, pred)
        exact = round(sum(per_row) / len(per_row), 4) if per_row else 0.0
        return {
            "task": task,
            "kind": kind,
            "exact_match": exact,
            "accuracy": exact,
            "exact_match_ci": _ci(per_row, seed=seed, n_boot=n_boot),
            "per_class": per_class,
            **_macros_from_per_class(per_class),
            "binary": binary_metrics(exp, pred, positive=positive),
            "confusion": {"matrix": matrix, "labels": labels},
            "top_confusions": top_confusions(matrix, labels),
            "n": len(per_row),
        }

    if kind == "pipeline":
        from .mailroom import align_doc_type, score_aligned_classification

        aligned = score_aligned_classification(expected, predicted)
        prf = macro_prf(list(expected), list(predicted))
        result = {
            "task": task,
            "kind": kind,
            **aligned,
            "accuracy": aligned.get("exact_accuracy", 0.0),
            "f1_macro": prf["f1_macro"],
            "precision_macro": prf["precision_macro"],
            "recall_macro": prf["recall_macro"],
            "f2_macro": prf["f2_macro"],
            "precision": prf["precision"],
            "recall": prf["recall"],
            "f2": prf["f2"],
            "per_class": prf["per_class"],
        }
        if expected_subclass is not None and predicted_subclass is not None:
            from .corpus import normalize_corpus_subclass
            from .sorter_guide import subclass_scorable

            sub_ok = []
            sub_exp_norm = []
            sub_pred_norm = []
            unannotated = 0
            for e, p, doc in zip(expected_subclass, predicted_subclass, expected):
                parent = align_doc_type(doc)
                if str(e or "").strip() and not subclass_scorable(parent, e):
                    unannotated += 1
                    continue
                en = normalize_corpus_subclass(parent, e)
                pn = normalize_corpus_subclass(parent, p)
                sub_exp_norm.append(en)
                sub_pred_norm.append(pn)
                sub_ok.append(1.0 if en == pn else 0.0)
            result["subclass_accuracy"] = (
                round(sum(sub_ok) / len(sub_ok), 4) if sub_ok else 0.0
            )
            result["n_subclass_scored"] = len(sub_ok)
            result["n_subclass_unannotated"] = unannotated
            result["per_subclass"] = _per_class(sub_exp_norm, sub_pred_norm)
            result.update(
                _macros_from_per_class(result["per_subclass"], prefix="subclass_")
            )
        return result

    if kind == "docclass":
        norm_dt = lambda v: normalize_label(v)
        doc_exp = [norm_dt(e) for e in expected]
        doc_errors = [str(p).startswith(ERROR_PREFIX) for p in predicted]
        doc_pred = [str(p) if error else norm_dt(p)
                    for p, error in zip(predicted, doc_errors)]
        doc_ok = [1.0 if not error and e == p else 0.0
                  for e, p, error in zip(doc_exp, doc_pred, doc_errors)]
        doc_acc = round(sum(doc_ok) / len(doc_ok), 4) if doc_ok else 0.0
        per_class = _per_class(doc_exp, doc_pred)
        result = {
            "task": task,
            "kind": kind,
            "doc_type_accuracy": doc_acc,
            "accuracy": doc_acc,
            "doc_type_accuracy_ci": _ci(doc_ok, seed=seed, n_boot=n_boot),
            "per_class": per_class,
            **_macros_from_per_class(per_class),
            "n": len(doc_ok),
        }
        if expected_subclass is not None and predicted_subclass is not None:
            from .corpus import normalize_corpus_subclass, subclass_equivalent
            from .sorter_guide import subclass_scorable

            sub_ok = []
            sub_ok_equiv = []
            sub_exp_norm = []
            sub_pred_norm = []
            exact = []
            unannotated = 0
            for e, p, doc_type, parent_error, doc_pred_value in zip(
                expected_subclass, predicted_subclass, doc_exp, doc_errors, doc_pred
            ):
                # An unannotated expected subclass (merger ``other``: no MAUD
                # consideration annotation) is no label: the class is scored,
                # the subclass is not.
                if str(e or "").strip() and not subclass_scorable(doc_type, e):
                    unannotated += 1
                    exact.append(1.0 if not parent_error and doc_type == doc_pred_value else 0.0)
                    continue
                # Scope the subclass catalog to the *expected* doc type so a
                # CUAD family is not forced through the MAUD consideration
                # normalizer (the pre-0.8.1 bug on the merged corpus).
                en = normalize_corpus_subclass(doc_type, e)
                error = parent_error or str(p).startswith(ERROR_PREFIX)
                pn = (ERROR_PREFIX + "parent classification failed" if parent_error
                      else str(p) if error else normalize_corpus_subclass(doc_type, p))
                sub_exp_norm.append(en)
                sub_pred_norm.append(pn)
                sub_ok.append(1.0 if not error and en == pn else 0.0)
                sub_ok_equiv.append(
                    1.0 if not error and (en == pn or subclass_equivalent(doc_type, en, pn)) else 0.0
                )
                exact.append(1.0 if doc_type == doc_pred_value and en == pn else 0.0)
            per_subclass = _per_class(sub_exp_norm, sub_pred_norm)
            result.update({
                "subclass_accuracy": round(sum(sub_ok) / len(sub_ok), 4) if sub_ok else 0.0,
                "subclass_accuracy_ci": _ci(sub_ok, seed=seed, n_boot=n_boot),
                "subclass_accuracy_equiv": round(sum(sub_ok_equiv) / len(sub_ok_equiv), 4) if sub_ok_equiv else 0.0,
                "exact_match": round(sum(exact) / len(exact), 4) if exact else 0.0,
                "exact_match_ci": _ci(exact, seed=seed, n_boot=n_boot),
                "per_subclass": per_subclass,
                "n_subclass_scored": len(sub_ok),
                "n_subclass_unannotated": unannotated,
                **_macros_from_per_class(per_subclass, prefix="subclass_"),
            })
        return result

    if kind == "chained":
        raise ValueError(
            "chained runs are scored with chained_composite()/chained_summary() "
            "— score_task expects per-document (expected, predicted) pairs"
        )

    return _label_score(task, expected, predicted, valid=valid, seed=seed, n_boot=n_boot)


def multiclass_score(expected: list, predicted: list, *, valid=None,
                     seed: int = 42, n_boot: int = 2000) -> dict:
    """Multi-classification score (macro + micro per-class + confusion)."""
    result = _label_score("multiclass", expected, predicted, valid=valid,
                          seed=seed, n_boot=n_boot)
    result["micro_accuracy"] = result["exact_match"]
    return result


def court_opinion_score(expected: list, predicted: list, *, seed: int = 42,
                        n_boot: int = 2000) -> dict:
    """Court-opinion doc-class scoring (the court_opinion dimension)."""
    return _label_score("court_opinion", expected, predicted,
                        seed=seed, n_boot=n_boot)


def maud_docclass_score(expected_doc_type: list, predicted_doc_type: list,
                        expected_subclass: list, predicted_subclass: list,
                        *, seed: int = 42, n_boot: int = 2000) -> dict:
    """MAUD merger-agreement hierarchical score (doc_type + consideration)."""
    return score_task("maud_docclass", expected_doc_type, predicted_doc_type,
                      expected_subclass=expected_subclass,
                      predicted_subclass=predicted_subclass,
                      seed=seed, n_boot=n_boot)


def maud_question_score(expected: list, predicted: list, *,
                        seed: int = 42, n_boot: int = 2000) -> dict:
    """MAUD Type-of-Consideration classification (legacy per-question surface).

    Scores parallel consideration-type answers (``All Cash`` → ``all_cash``).
    For the 22 Hub ``maud_clause_labels`` questions use
    :func:`maud_extraction_score`.
    """
    return score_task("maud_question", expected, predicted,
                      seed=seed, n_boot=n_boot)


def maud_extraction_score(expected, predicted) -> dict:
    """MAUD per-question extraction over the 22 Hub clause keys."""
    from .content_scoring import score_maud_extraction

    return score_maud_extraction(expected, predicted)


def legalbench_score(expected: list, predicted: list, *,
                     seed: int = 42, n_boot: int = 2000) -> dict:
    """LegalBench task-mode score (binary Yes/No + per-class + metrics)."""
    return score_task("legalbench", expected, predicted, seed=seed, n_boot=n_boot)


def chained_composite(sorter_score: float, extractor_score: float,
                      *, weights: tuple[float, float] = (0.25, 0.75)) -> float:
    """Combine the sorter classification score and the extractor composite
    into one chained run score.

    The extractor carries the document-level output the pipeline is ultimately
    judged on, so it dominates the default weighting (0.25 / 0.75)."""
    w_s, w_e = weights
    return round(w_s * float(sorter_score) + w_e * float(extractor_score), 4)


def chained_summary(
    sorter_exact: float, sorter_subtype: float,
    extractor_overall: float, extractor_presence: float,
    n: int,
    *,
    weights: tuple[float, float] = (0.25, 0.75),
) -> dict:
    """Record-shaped score dict for a chained sorter→extractor run.

    Mirrors the repo's chained-eval composite: the sorter's document-type
    exact match + subtype accuracy, the extractor's overall extraction score +
    field presence, and the weighted composite (default 0.25/0.75)."""
    return {
        "sorter": {
            "exact_match": round(float(sorter_exact), 4),
            "subtype_accuracy": round(float(sorter_subtype), 4),
        },
        "extractor": {
            "overall_extraction_score": round(float(extractor_overall), 4),
            "field_presence": round(float(extractor_presence), 4),
        },
        "composite": chained_composite(sorter_exact, extractor_overall, weights=weights),
        "weights": {"sorter": weights[0], "extractor": weights[1]},
        "n": n,
    }


def get_jaccard(gt: str, pred: str) -> float:
    """Token-set Jaccard — EXACT copy of ContractEval's ``Evaluation.py``.

    Strips ``.,;:``, lowercases, replaces ``/`` with a space, then
    |∩|/|∪| over whitespace tokens (arXiv 2508.03080 §III-D). The degenerate
    both-empty case returns 1.0 (``"".split(" ")`` = ``[""]``) exactly as the
    paper's code does — it is never reached on the positive-label pairs the
    metric is defined over.
    """
    for token in (".", ",", ";", ":"):
        gt = gt.replace(token, "")
        pred = pred.replace(token, "")
    gt = gt.lower().replace("/", " ")
    pred = pred.lower().replace("/", " ")
    gt_words = set(gt.split(" "))
    pred_words = set(pred.split(" "))
    union = gt_words.union(pred_words)
    if not union:
        return 0.0
    return len(gt_words.intersection(pred_words)) / len(union)


def said_no_related(output: str) -> bool:
    """ContractEval's "no related clause" detector — case-insensitive substring
    on the whitespace/backtick-stripped output (mirrors ``Evaluation.py``)."""
    return CONTRACTEVAL_NO_RELATED_PHRASE in output.strip(" \n`").lower()


def contracteval_classified(label_spans: list[str], output: str) -> bool:
    """ContractEval's TP predicate: every GT label span, stripped of
    whitespace/backticks, is verbatim-contained in the stripped output."""
    out = output.strip(" \n`")
    return all(substr.strip(" \n`") in out for substr in label_spans)


def contracteval_metrics(
    expected_spans: list[list[str]],
    outputs: list[str],
    *,
    categories: list[str] | None = None,
    positive_denominator: int | None = CONTRACTEVAL_POSITIVE_DENOMINATOR,
) -> dict:
    """ContractEval's EXACT correctness / output-effectiveness / laziness
    metrics (arXiv 2508.03080 §III-D), mirroring ``Evaluation.py`` +
    ``open_source_model.py``.

    Args:
        expected_spans: parallel list of GT label-span lists (empty = the
            category is absent for that (contract, question) pair).
        outputs: parallel list of raw model outputs.
        categories: optional parallel category labels -> per-category breakdown.
        positive_denominator: the paper's false-"no related clause" rate
            divides by its HARDCODED positive count (1,244); pass ``None`` to
            use the run's own positive count. Both rates are returned.

    Confusion: TP = every GT span verbatim-contained in the output; TN =
    absent category + "no related clause"; FP = absent category + a non-empty
    clause; FN = present category + "no related clause" or partial coverage.
    F1/F2 over the pooled confusion; mean/median token-set Jaccard over
    POSITIVE pairs; no-related rate over all pairs; false-no-related rate over
    the positives.
    """
    require_equal_lengths(expected_spans, outputs)
    tp = tn = fn = fp = 0
    jaccards: list[float] = []
    no_related_cnt = 0
    false_no_related_cnt = 0
    per_category: dict[str, dict] = {}
    for i, (label, output) in enumerate(zip(expected_spans, outputs)):
        output = str(output or "")
        no_related = said_no_related(output)
        classified = bool(label) and contracteval_classified(label, output)
        if no_related:
            no_related_cnt += 1
        if not label:
            if no_related:
                tn += 1
            else:
                fp += 1
        else:
            if no_related:
                false_no_related_cnt += 1
            if classified:
                tp += 1
            else:
                fn += 1
            jaccards.append(get_jaccard(" ".join(label), output.strip(" \n`")))
        if categories is not None:
            cat = categories[i]
            pc = per_category.setdefault(
                cat, {"tp": 0, "tn": 0, "fp": 0, "fn": 0, "jaccards": []})
            if not label:
                if no_related:
                    pc["tn"] += 1
                else:
                    pc["fp"] += 1
            elif classified:
                pc["tp"] += 1
            else:
                pc["fn"] += 1
            if label:
                pc["jaccards"].append(get_jaccard(" ".join(label), output.strip(" \n`")))

    total = tp + tn + fp + fn
    n_pos = tp + fn
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    f2 = 5 * precision * recall / (4 * precision + recall) if (4 * precision + recall) else 0.0
    denominator = positive_denominator if positive_denominator is not None else n_pos

    result = {
        "task": "contracteval",
        "kind": "contracteval",
        "n_pairs": total,
        "n_positive": n_pos,
        "tp": tp, "tn": tn, "fp": fp, "fn": fn,
        "accuracy": round((tp + tn) / total, 4) if total else 0.0,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "f2": round(f2, 4),
        "jaccard_mean": round(sum(jaccards) / len(jaccards), 4) if jaccards else 0.0,
        "jaccard_median": round(sorted(jaccards)[len(jaccards) // 2], 4) if jaccards else 0.0,
        "no_related_rate": round(no_related_cnt / total, 4) if total else 0.0,
        "false_no_related_rate": round(false_no_related_cnt / n_pos, 4) if n_pos else 0.0,
        "false_no_related_rate_paper": (
            round(false_no_related_cnt / denominator, 4) if denominator else 0.0),
        "false_no_related_denominator": denominator,
    }
    if categories is not None:
        result["per_category"] = _contracteval_per_category(per_category)
    return result


def _contracteval_per_category(per_category: dict[str, dict]) -> dict[str, dict]:
    """Summarize per-category confusion + Jaccard into metric dicts (the paper's
    Fig-4 per-question breakdown)."""
    out: dict[str, dict] = {}
    for cat, c in per_category.items():
        tp, tn, fp, fn = c["tp"], c["tn"], c["fp"], c["fn"]
        total = tp + tn + fp + fn
        n_pos = tp + fn
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        f2 = 5 * precision * recall / (4 * precision + recall) if (4 * precision + recall) else 0.0
        j = c["jaccards"]
        out[cat] = {
            "n_pairs": total, "n_positive": n_pos,
            "tp": tp, "tn": tn, "fp": fp, "fn": fn,
            "accuracy": round((tp + tn) / total, 4) if total else 0.0,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "f2": round(f2, 4),
            "jaccard_mean": round(sum(j) / len(j), 4) if j else 0.0,
        }
    return out


def contracteval_score(
    expected_spans: list[list[str]],
    outputs: list[str],
    *,
    categories: list[str] | None = None,
    positive_denominator: int | None = CONTRACTEVAL_POSITIVE_DENOMINATOR,
) -> dict:
    """Convenience alias for :func:`contracteval_metrics` (the ``contracteval``
    task kind of :func:`score_task`)."""
    return contracteval_metrics(
        expected_spans, outputs,
        categories=categories, positive_denominator=positive_denominator,
    )


__all__ = [
    "task_kind", "normalize_maud_consideration", "normalize_legalbench",
    "normalize_task_answer", "score_task", "multiclass_score",
    "court_opinion_score", "maud_docclass_score", "maud_question_score",
    "maud_extraction_score",
    "legalbench_score", "chained_composite", "chained_summary",
    "get_jaccard", "said_no_related", "contracteval_classified",
    "contracteval_metrics", "contracteval_score",
]
