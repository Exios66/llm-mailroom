"""CUAD families + 41 clause categories and MAUD consideration / clause labels.

Hub ground truth (``Lucius-Morningstar/mailroom-corpus`` config ``ground_truth``)
stores:

- ``expected_subclass`` — CUAD family (contracts) or MAUD consideration token
  (merger agreements)
- ``cuad_clause_labels`` — JSON object of the 41 Atticus CUAD categories →
  ``[{start, text}, ...]`` (empty list = clause absent)
- ``maud_clause_labels`` — JSON object of MAUD questions →
  ``{answer, category, excerpt_chars, valid_classes, ...}``

The contracts specialist emits flattened ``"<label>: <content>"`` lines in
``cuad_clauses`` / ``maud_clauses`` so chunked merge, field scoring, and the
HF expected_fields join share one shape.
"""

from __future__ import annotations

import json
import re
from typing import Any


# Official Atticus CUAD v1 clause categories (41). Names match the Hub JSON keys.
CUAD_CLAUSE_CATEGORIES: tuple[str, ...] = (
    "Document Name",
    "Parties",
    "Agreement Date",
    "Effective Date",
    "Expiration Date",
    "Renewal Term",
    "Notice Period To Terminate Renewal",
    "Governing Law",
    "Most Favored Nation",
    "Competitive Restriction Exception",
    "Non-Compete",
    "Exclusivity",
    "No-Solicit Of Customers",
    "No-Solicit Of Employees",
    "Non-Disparagement",
    "Termination For Convenience",
    "Rofr/Rofo/Rofn",
    "Change Of Control",
    "Anti-Assignment",
    "Revenue/Profit Sharing",
    "Price Restrictions",
    "Minimum Commitment",
    "Volume Restriction",
    "Ip Ownership Assignment",
    "Joint Ip Ownership",
    "License Grant",
    "Non-Transferable License",
    "Affiliate License-Licensor",
    "Affiliate License-Licensee",
    "Unlimited/All-You-Can-Eat-License",
    "Irrevocable Or Perpetual License",
    "Source Code Escrow",
    "Post-Termination Services",
    "Audit Rights",
    "Uncapped Liability",
    "Cap On Liability",
    "Liquidated Damages",
    "Warranty Duration",
    "Insurance",
    "Covenant Not To Sue",
    "Third Party Beneficiary",
)

MAUD_CONSIDERATION = (
    "all_cash",
    "all_stock",
    "mixed_cash_stock",
    "mixed_cash_stock_election",
    "other",
)

# Hub ``maud_clause_labels`` question names (LegalBench MAUD v1). A given
# merger row only stores the answered subset; omit unanswered questions.
MAUD_CLAUSE_QUESTIONS: tuple[str, ...] = (
    "Absence of Litigation Closing Condition",
    "Accuracy of Target R&W Closing Condition",
    "Agreement provides for matching rights in connection with COR",
    "Agreement provides for matching rights in connection with FTR",
    "Breach of Meeting Covenant",
    "Breach of No Shop",
    "Compliance with Covenant Closing Condition",
    "FTR Triggers",
    "Fiduciary exception to COR covenant",
    "Fiduciary exception:  Board determination (no-shop)",
    "General Antitrust Efforts Standard",
    "Intervening Event Definition",
    "Knowledge Definition",
    "Limitations on FTR Exercise",
    "MAE Definition",
    "Negative interim operating covenant",
    "No-Shop",
    "Ordinary course covenant",
    "Specific Performance",
    "Superior Offer Definition",
    "Tail Period & Acquisition Proposal Details",
    "Type of Consideration",
)

# Hub ``expected_subclass`` strings for the 25 CUAD families (plus the
# Joint-Venture 13D/13G filing folder, which maps onto joint_venture).
HUB_CUAD_FAMILY_LABELS: tuple[str, ...] = (
    "Affiliate_Agreements",
    "Agency Agreements",
    "Co_Branding",
    "Collaboration",
    "Consulting Agreements",
    "Development",
    "Distributor",
    "Endorsement",
    "Franchise",
    "Hosting",
    "IP",
    "Joint Venture",
    "Joint Venture _ Filing",
    "License_Agreements",
    "Maintenance",
    "Manufacturing",
    "Marketing",
    "Non_Compete_Non_Solicit",
    "Outsourcing",
    "Promotion",
    "Reseller",
    "Service",
    "Sponsorship",
    "Strategic Alliance",
    "Supply",
    "Transportation",
)

# Hub Type-of-Consideration ``valid_classes`` → merger_consideration token.
_MAUD_CONSIDERATION_ANSWERS = {
    "allcash": "all_cash",
    "allstock": "all_stock",
    "mixedcashstock": "mixed_cash_stock",
    "mixedcashstockelection": "mixed_cash_stock_election",
}


def normalize_consideration(value) -> str:
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


def infer_merger_consideration(extracted: dict | None) -> str:
    data = extracted or {}
    for key in ("merger_consideration", "contract_value", "document_name"):
        token = normalize_consideration(data.get(key) or "")
        if token:
            return token
    for line in as_clause_lines(data.get("maud_clauses")):
        if line.lower().startswith("type of consideration:"):
            token = normalize_consideration(line.split(":", 1)[-1])
            if token:
                return token
    blobs = []
    for key in ("maud_clauses", "cuad_clauses", "key_obligations"):
        val = data.get(key) or []
        if isinstance(val, list):
            blobs.extend(str(x) for x in val)
    if blobs:
        return normalize_consideration(" ".join(blobs))
    return ""

# Inventory fields are document-specific (not matter identity) and empty is a
# valid "clause absent" extraction.
INVENTORY_FIELDS = frozenset({
    "cuad_family",
    "merger_consideration",
    "cuad_clauses",
    "maud_clauses",
})

_CONFLICT_SKIP = INVENTORY_FIELDS | {"reasoning", "confidence", "document_name"}


def parse_json_obj(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if raw in (None, "", [], {}):
        return {}
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def flatten_cuad_clause_labels(raw: Any) -> list[str]:
    """Hub CUAD spans → ``['Anti-Assignment: <verbatim>', ...]`` (present only)."""
    data = parse_json_obj(raw)
    out: list[str] = []
    seen: set[str] = set()
    for category, spans in data.items():
        cat = str(category).strip()
        if not cat:
            continue
        items = spans if isinstance(spans, list) else [spans]
        for span in items:
            if span in (None, "", [], {}):
                continue
            if isinstance(span, dict):
                text = str(span.get("text") or "").strip()
            else:
                text = str(span).strip()
            if not text:
                continue
            line = f"{cat}: {text}"
            key = re.sub(r"\s+", " ", line.casefold())
            if key in seen:
                continue
            seen.add(key)
            out.append(line)
    return out


def flatten_maud_clause_labels(raw: Any) -> list[str]:
    """Hub MAUD answers → ``['Question: Answer', ...]`` (answered only)."""
    data = parse_json_obj(raw)
    out: list[str] = []
    seen: set[str] = set()
    for question, item in data.items():
        name = str(question).strip()
        if not name:
            continue
        if isinstance(item, dict):
            answer = item.get("answer")
        else:
            answer = item
        if answer in (None, "", [], {}):
            continue
        line = f"{name}: {answer}"
        key = re.sub(r"\s+", " ", line.casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def as_clause_lines(value: Any) -> list[str]:
    """Coerce LLM / Hub clause payloads into scoring lines."""
    if value in (None, "", [], {}):
        return []
    if isinstance(value, str):
        parsed = parse_json_obj(value)
        if parsed:
            if any(isinstance(v, list) for v in parsed.values()):
                return flatten_cuad_clause_labels(parsed)
            return flatten_maud_clause_labels(parsed)
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        if any(isinstance(v, list) for v in value.values()):
            return flatten_cuad_clause_labels(value)
        return flatten_maud_clause_labels(value)
    if not isinstance(value, list):
        return [str(value)]
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str) and item.strip():
            line = item.strip()
        elif isinstance(item, dict):
            cat = str(item.get("category") or item.get("question") or item.get("label") or "").strip()
            text = str(
                item.get("text") or item.get("answer") or item.get("excerpt") or ""
            ).strip()
            if cat and text:
                line = f"{cat}: {text}"
            elif text:
                line = text
            elif cat:
                line = cat
            else:
                continue
        else:
            continue
        key = re.sub(r"\s+", " ", line.casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def clause_handoff(doc_type: str | None, contract_subtype: str | None) -> str:
    """Additive specialist instructions listing every CUAD / MAUD inventory."""
    lines = [
        "CUAD/MAUD CLAUSE INVENTORY — emit every PRESENT item; omit absent ones "
        "(empty arrays are correct when the visible text has no such clause).",
        "cuad_clauses: one string per present CUAD category using the exact "
        "Atticus names below, formatted '<Category>: <verbatim operative span>'.",
        "Categories: " + "; ".join(CUAD_CLAUSE_CATEGORIES) + ".",
    ]
    if contract_subtype:
        lines.append(
            f"This agreement's CUAD family is {contract_subtype}: still scan "
            "ALL 41 categories (family-characteristic clauses are required, "
            "not exclusive)."
        )
    if (doc_type or "") == "merger_agreement":
        lines.append(
            "maud_clauses: one string per answered MAUD question using the "
            "exact question names below, formatted '<Question>: <Answer>' "
            "where Answer is the Hub valid_class (Yes/No, All Cash, "
            "Continuous matching right, General R&Ws, …) — never a paraphrase. "
            "Questions: " + "; ".join(MAUD_CLAUSE_QUESTIONS) + ". "
            "Omit unanswered questions. merger_consideration must be exactly "
            "one of: " + ", ".join(MAUD_CONSIDERATION) + " (All Cash→all_cash, "
            "All Stock→all_stock, Mixed Cash/Stock→mixed_cash_stock, Mixed "
            "Cash/Stock: Election→mixed_cash_stock_election)."
        )
        lines.append("cuad_family is null for merger_agreement.")
    else:
        lines.append(
            "maud_clauses is [] unless the document is actually a merger "
            "agreement. cuad_family is the CUAD family key (affiliate, "
            "license, distributor, …) matching the sorter subtype."
        )
    return " ".join(lines)


def enrich_contract_extraction(
    extracted: dict | None,
    *,
    doc_type: str | None = None,
    contract_subtype: str | None = None,
) -> dict:
    """Fill CUAD/MAUD inventory fields without overwriting a specialist value."""
    result = dict(extracted or {})
    result["cuad_clauses"] = as_clause_lines(result.get("cuad_clauses"))
    result["maud_clauses"] = as_clause_lines(result.get("maud_clauses"))
    family = str(result.get("cuad_family") or "").strip()
    if not family and contract_subtype and (doc_type or "") != "merger_agreement":
        try:
            from langchain_agents.sorter_agent import normalize_subtype

            family = normalize_subtype(contract_subtype)
        except Exception:
            family = str(contract_subtype)
        if family and family != "other":
            result["cuad_family"] = family
    elif family:
        result["cuad_family"] = family
    if (doc_type or "") == "merger_agreement":
        if not str(result.get("merger_consideration") or "").strip():
            token = infer_merger_consideration(result)
            if token:
                result["merger_consideration"] = token
        result.setdefault("cuad_family", None)
    else:
        result.setdefault("merger_consideration", None)
    result.setdefault("cuad_family", None)
    result.setdefault("cuad_clauses", [])
    result.setdefault("maud_clauses", [])
    return result


def skip_conflict_field(name: str) -> bool:
    return name in _CONFLICT_SKIP
