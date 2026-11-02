"""Exact pipeline label policies, separate from public generic task defaults.

Catalog assembly and producer field selection remain with the pipeline. These
policies retain its compact matching, unknown tokens and ordered fallbacks.
"""
from __future__ import annotations
import re
from .corpus import normalize_corpus_subclass
from .equivalences import equivalent_subtypes
from .config import Settings

def compact_label(value: object) -> str:
    """Compact the historical inventory label (falsy values become blank)."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

def normalize_inventory_label(value, keys, aliases) -> str:
    """Map a free-text label onto a canonical inventory token.

    Longest canonical key wins so ``attorney_demand`` is not swallowed by
    ``demand``, and ``articles_of_incorporation`` is not swallowed by a
    short alias collision.
    """
    key = compact_label(value)
    if not key:
        return ""
    compact_keys = {compact_label(k): k for k in keys}
    if key in compact_keys:
        return compact_keys[key]
    alias_norm = {compact_label(k): v for k, v in aliases.items()}
    if key in alias_norm:
        return alias_norm[key]
    ranked_aliases = sorted(aliases.items(), key=lambda kv: -len(compact_label(kv[0])))
    for alias, canonical in ranked_aliases:
        ak = compact_label(alias)
        if len(ak) >= 8 and (key.startswith(ak) or ak in key):
            return canonical
    ranked = sorted(keys, key=lambda item: -len(compact_label(item)))
    for canonical in ranked:
        ck = compact_label(canonical)
        if not ck or canonical == "other":
            continue
        if key.startswith(ck):
            return canonical
    for canonical in ranked:
        ck = compact_label(canonical)
        if not ck or canonical == "other":
            continue
        if len(ck) >= 4 and ck in key:
            return canonical
        if len(ck) >= 3 and ck[:1].isdigit() and ck in key:
            return canonical
    if key == "other":
        return "other"
    return ""

def normalize_pipeline_subtype(value, *, keys, aliases, subtypes) -> str:
    """Coerce a raw sorter subtype output (or a CUAD folder name) to a
    canonical subtype key; unknown/non-contract values become ``other``."""
    if value is None:
        return "other"
    key = re.sub(r"[^a-z0-9]", "", str(value).strip().lower())
    if not key:
        return "other"
    compact = {re.sub(r"[^a-z0-9]", "", k): k for k in keys}
    if key in compact:
        return compact[key]
    alias_norm = {re.sub(r"[^a-z0-9]", "", k): v for k, v in aliases.items()}
    if key in alias_norm:
        return alias_norm[key]
    # "License Agreement" -> "license"; "Non-Compete" -> non_compete_no_solicit.
    for subtype in subtypes:
        norm_label = re.sub(r"[^a-z0-9]", "", subtype["label"].lower())
        if key == norm_label or key.startswith(norm_label[:8]):
            return subtype["key"]
    return "other"

_MAUD_CONSIDERATION_ANSWERS = {
    "allcash": "all_cash",
    "allstock": "all_stock",
    "mixedcashstock": "mixed_cash_stock",
    "mixedcashstockelection": "mixed_cash_stock_election",
}

def normalize_pilot_consideration(value) -> str:
    """Map MAUD merger-consideration labels to the Hub token set."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    key = re.sub(r"[^a-z0-9]", "", raw.lower())
    aliases = {
        "allcash": "all_cash",
        "cash": "all_cash",
        "cashonly": "all_cash",
        "allstock": "all_stock",
        "stock": "all_stock",
        "stockonly": "all_stock",
        "mixedcashstockelection": "mixed_cash_stock_election",
        "mixedelection": "mixed_cash_stock_election",
        "mixedcashstock": "mixed_cash_stock",
        "mixed": "mixed_cash_stock",
        "cashandstock": "mixed_cash_stock",
        "cashstock": "mixed_cash_stock",
        "other": "other",
        **_MAUD_CONSIDERATION_ANSWERS,
    }
    if key in aliases:
        return aliases[key]
    lower = raw.lower()
    if "election" in lower and "cash" in lower and "stock" in lower:
        return "mixed_cash_stock_election"
    if "cash" in lower and "stock" in lower:
        return "mixed_cash_stock"
    if "all cash" in lower or "cash consideration" in lower or "per share in cash" in lower:
        return "all_cash"
    if "all stock" in lower or "stock consideration" in lower:
        return "all_stock"
    return ""

def normalize_sorter_token(kind, value, catalog, *, contract_token=None):
    """Keep scoped corpus normalization, including its unknown-to-other policy.

    ``contract_token`` is the producer's already normalized CUAD token. Exact
    accepted producer tokens retain their identity; other values use the
    existing canonical corpus normalizer.
    """
    if value is None or not str(value).strip() or not catalog:
        return None
    text = str(value).strip()
    if kind != "contract" and text in catalog:
        return text
    token = contract_token if kind == "contract" else normalize_corpus_subclass(kind, text)
    if token in catalog:
        return token
    return text if text in catalog else None


def loose_label_match(predicted, expected):
    """Historical nonempty alphanumeric containment, in either direction."""
    a, b = compact_label(predicted), compact_label(expected)
    return bool(a and b and (a == b or a in b or b in a))


def catalog_subtypes_equivalent(a, b, equivalences):
    """Reuse Dojo equivalence with the producer's explicit frozen family groups."""
    return equivalent_subtypes(a, b, settings=Settings(subtype_equivalences=list(equivalences)))


def pilot_subclass_match(kind, expected, *, sorter_token=None, fallback_token=None,
                         inferred_token=None, predicted_consideration=None,
                         expected_token=None, loose_predicted=None, contract_equivalences=None):
    """Score pre-adapted subclass candidates with the pilot's ordered policy.

    Missing subclass GT has no support. CUAD equivalent families and unknown
    tokens retain their historical meaning; loose matching is a separate policy.
    """
    if not str(expected or "").strip():
        return None
    if kind == "contract":
        if not sorter_token or not expected_token:
            return False
        if contract_equivalences is not None:
            return catalog_subtypes_equivalent(sorter_token, expected_token, contract_equivalences)
        return equivalent_subtypes(sorter_token, expected_token)
    got = sorter_token or fallback_token
    need = expected_token
    if kind == "merger_agreement":
        got = got or inferred_token or predicted_consideration
        return bool(got and need and got == need)
    if kind in {"corporate_record", "insurance_claim", "correspondence"}:
        if got and need:
            return got == need
    return loose_label_match(loose_predicted, expected)
