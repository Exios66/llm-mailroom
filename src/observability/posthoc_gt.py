"""Deterministic post-hoc extraction GT from document text.

Hub official labels (CUAD clauses, MAUD questions, CMS columns, subclass
tokens) always win. Remaining specialist schema fields are filled from
conservative regexes over the source text so every included document has
scorable labels — even when the Hub row is subclass-only.

These extractors are evaluation gold, not the model under test. They must
stay conservative: only emit a field when the pattern is unambiguous. Do
not invent Hub n=0 accuracy for ``compliance_filing``.
"""

from __future__ import annotations

import re
from typing import Any

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_JURISDICTIONS = (
    ("cayman islands", "Cayman Islands"),
    ("new york", "New York"),
    ("delaware", "Delaware"),
    ("nevada", "Nevada"),
    ("california", "California"),
    ("texas", "Texas"),
    ("federal republic of germany", "Germany"),
    ("germany", "Germany"),
    ("netherlands", "Netherlands"),
    ("united kingdom", "United Kingdom"),
    ("england", "England"),
)

_COMPANY_TAIL = (
    r"(?:LLC|L\.L\.C\.|Inc\.?|Incorporated|Corp\.?|Corporation|Ltd\.?|"
    r"Limited|LP|L\.P\.|PLC|N\.V\.|B\.V\.|GmbH|AG|S\.A\.?|Company|Co\.)"
)

_COMPANY = re.compile(
    rf"\b([A-Z][A-Za-z0-9&.'/-]*(?:\s+[A-Z][A-Za-z0-9&.'/-]*){{0,7}}"
    rf",?\s+{_COMPANY_TAIL})\b"
)

_ISO_DATE = re.compile(r"\b(20\d{2}|19\d{2})-(\d{2})-(\d{2})\b")
_US_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2}|19\d{2})\b")
_LONG_DATE = re.compile(
    r"\b("
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
    r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+(?:20\d{2}|19\d{2})"
    r"|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?"
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)"
    r",?\s+(?:20\d{2}|19\d{2})"
    r")\b",
    re.I,
)
_MONEY = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_BLANK = (None, "", [], {}, ())


def _clean_ws(text: str) -> str:
    return re.sub(r"[ \t]+", " ", str(text or "")).strip()


def _norm_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _present(value: Any) -> bool:
    return value not in _BLANK


def _iso(year: int, month: int, day: int) -> str | None:
    if not (1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 2100):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_date(token: str | None) -> str | None:
    """Best-effort date → ISO ``YYYY-MM-DD``."""
    raw = _norm_space(token or "")
    if not raw:
        return None
    m = _ISO_DATE.search(raw)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _US_DATE.search(raw)
    if m:
        return _iso(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    m = _LONG_DATE.search(raw)
    if not m:
        return None
    chunk = m.group(1)
    parts = re.findall(r"[A-Za-z]+|\d+", chunk)
    month = year = day = None
    for part in parts:
        low = part.lower()
        if low in _MONTHS:
            month = _MONTHS[low]
        elif part.isdigit() and len(part) == 4:
            year = int(part)
        elif part.isdigit() and month is None:
            day = int(part)
        elif part.isdigit() and day is None:
            day = int(part)
    if year and month:
        return _iso(year, month, day or 1)
    return None


def first_date(text: str, *, window: int = 2500) -> str | None:
    chunk = text[:window]
    for rx in (_ISO_DATE, _LONG_DATE, _US_DATE):
        m = rx.search(chunk)
        if m:
            parsed = parse_date(m.group(0))
            if parsed:
                return parsed
    return None


def first_money(text: str) -> float | None:
    m = _MONEY.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _label_value(text: str, labels: tuple[str, ...], *, window: int = 4000) -> str | None:
    chunk = text[:window]
    for label in labels:
        rx = re.compile(
            rf"{re.escape(label)}\s*[:\-]\s*(.+)",
            re.I,
        )
        m = rx.search(chunk)
        if not m:
            continue
        value = _clean_ws(m.group(1).split("\n", 1)[0])
        value = value.strip(" .;,-")
        if value:
            return value
    return None


def _companies(text: str, *, limit: int = 6) -> list[str]:
    seen: list[str] = []
    for match in _COMPANY.finditer(text[:5000]):
        name = _norm_space(match.group(1)).rstrip(".,;")
        name = re.sub(r"\s+", " ", name)
        key = name.lower()
        if key in {s.lower() for s in seen}:
            continue
        if len(name) < 5:
            continue
        seen.append(name)
        if len(seen) >= limit:
            break
    return seen


def _jurisdiction(text: str) -> str | None:
    low = text[:4000].lower()
    for needle, label in _JURISDICTIONS:
        if needle in low:
            return label
    return None


def _articles(text: str, *, limit: int = 4) -> list[str]:
    titles: list[str] = []
    for m in re.finditer(
        r"(?:ARTICLE|Article|SECTION|Section)\s+[IVXLC\d.]+[.\s—:-]+([^\n]{8,120})",
        text[:8000],
    ):
        title = _norm_space(m.group(0))
        title = re.sub(r"\s+", " ", title).strip(" .;")
        if title and title not in titles:
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def extract_contract_fields(text: str) -> dict[str, Any]:
    """Parties / dates / title / governing law from CUAD and MAUD preambles."""
    head = text[:4500]
    out: dict[str, Any] = {}
    title = None
    m = re.search(
        r"(?:THIS|This)\s+((?:AMENDED AND RESTATED\s+)?"
        r"[A-Z][A-Za-z /&-]{6,80}AGREEMENT)",
        head,
    )
    if m:
        title = _norm_space(m.group(1))
    if not title:
        m = re.search(
            r"(AGREEMENT AND PLAN OF MERGER|"
            r"AMENDED AND RESTATED\s+AGREEMENT AND PLAN OF MERGER|"
            r"[A-Z][A-Z /&-]{8,70} AGREEMENT)",
            head,
        )
        if m:
            title = _norm_space(m.group(1)).title() if m.group(1).isupper() else _norm_space(m.group(1))
    if title:
        out["document_name"] = title[:160]
    parties = _companies(head, limit=6)
    among = re.search(
        r"(?:BY AND AMONG|by and among|by and between|by and between|"
        r"entered into by and between)\s+(.{20,600})",
        head,
        re.I | re.S,
    )
    if among:
        block = among.group(1)
        extra = _companies(block, limit=6)
        for name in extra:
            if name.lower() not in {p.lower() for p in parties}:
                parties.append(name)
    if parties:
        out["parties"] = parties[:6]
    dated = (
        _label_value(head, ("Effective Date", "Dated as of", "Date"))
        or first_date(head)
    )
    parsed = parse_date(dated) if dated else first_date(head)
    if parsed:
        out["effective_date"] = parsed
    m = re.search(
        r"(?:laws of(?: the(?: State of)?)?|State of)\s+"
        r"(Delaware|Nevada|California|New York|Texas|Cayman Islands|"
        r"the Cayman Islands|England|Germany)",
        head,
        re.I,
    )
    if m:
        law = _norm_space(m.group(1))
        if law.lower().startswith("the "):
            law = law[4:]
        out["governing_law"] = law.title() if law.lower() != "cayman islands" else "Cayman Islands"
    elif "delaware" in head.lower():
        out["governing_law"] = "Delaware"
    m = re.search(
        r"(?:Initial Term|term)\s+of\s+(?:three|two|one|four|five|\d+)\s*"
        r"\(?\d+\)?\s*years",
        head,
        re.I,
    )
    if m:
        out["term_length"] = _norm_space(m.group(0))
    money = first_money(head)
    if money is not None and money >= 100:
        out["contract_value"] = money
    return out


def extract_corporate_fields(text: str) -> dict[str, Any]:
    head = text[:5000]
    out: dict[str, Any] = {}
    m = re.search(
        r"(?:BYLAWS|By-Laws|BY-LAWS|ARTICLES OF (?:INCORPORATION|ASSOCIATION)|"
        r"CERTIFICATE OF (?:INCORPORATION|FORMATION)|"
        r"MEMORANDUM AND\s+ARTICLES OF ASSOCIATION)\s+"
        r"(?:FOR THE REGULATION OF\s+|OF\s+)?([A-Z][A-Za-z0-9&.,' -]{2,80}?"
        r"(?:INC\.?|INCORPORATED|CORP\.?|CORPORATION|LLC|LTD\.?|LIMITED)?)",
        head,
        re.I,
    )
    if m:
        name = _norm_space(m.group(1))
        name = re.sub(r"\s+", " ", name).strip(" ,.")
        if name.lower() not in {"the corporation", "a nevada", "a delaware"}:
            out["entity_name"] = name[:160]
    if "entity_name" not in out:
        m = re.search(
            r"(?:name of the Company is|The name of the Company is)\s+"
            r"([A-Z][A-Za-z0-9&.,' -]{2,80})",
            head,
        )
        if m:
            out["entity_name"] = _norm_space(m.group(1)).rstrip(".")
    juris = _jurisdiction(head)
    if juris:
        out["jurisdiction"] = juris
    dated = first_date(head)
    if dated:
        out["effective_date"] = dated
    articles = _articles(head)
    if articles:
        out["key_provisions"] = articles
    file_no = _label_value(
        head,
        ("File Number", "FILE NUMBER", "Commission File Number", "Filing Number"),
    )
    if file_no:
        out["filing_number"] = file_no.split()[0]
    signers: list[str] = []
    for m in re.finditer(
        r"/s/\s*([A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){0,3})",
        text[:8000],
    ):
        name = _norm_space(m.group(1))
        if name not in signers and len(name) > 3:
            signers.append(name)
    if signers:
        out["signatories"] = signers[:6]
    return out


def extract_correspondence_fields(text: str) -> dict[str, Any]:
    head = text[:3500]
    out: dict[str, Any] = {}
    sender = _label_value(head, ("FROM", "From", "Sender"))
    recipient = _label_value(head, ("TO", "To", "Recipient"))
    if sender:
        out["sender"] = sender.split(",")[0].strip()[:120]
    if recipient:
        out["recipient"] = recipient.split("\n")[0].split(",")[0].strip()[:120]
    dated = _label_value(head, ("DATE", "Date")) or first_date(head)
    parsed = parse_date(dated) if dated else None
    if parsed:
        out["communication_date"] = parsed
    subject = _label_value(head, ("RE", "Re", "SUBJECT", "Subject"))
    if subject:
        out["key_points"] = [subject[:240]]
    m = re.search(
        r"(?:Cordially|Sincerely|Best regards|Best Rgds|Very truly yours)"
        r"[,.]?\s+([A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){0,3})",
        text[:4000],
        re.I,
    )
    if m and "sender" not in out:
        out["sender"] = _norm_space(m.group(1))
    m = re.search(r"Dear\s+([^,:\n]{2,80})[,:]", head)
    if m and "recipient" not in out:
        out["recipient"] = _norm_space(m.group(1))
    cc = _label_value(head, ("cc", "CC", "Cc"))
    if cc:
        extras = [p.strip() for p in re.split(r"[,;/]|\n", cc) if p.strip()]
        extras = [p for p in extras if len(p) > 2][:8]
        if extras:
            out["additional_recipients"] = extras
    if re.search(r"\b(urgent|immediate|time.?sensitive)\b", head, re.I):
        out["urgency"] = "high"
    money = first_money(head)
    if money is not None:
        out["demand_amount"] = money
    refs = []
    for m in re.finditer(
        r"((?:Demand )?Letter dated [A-Z][a-z]+ \d{1,2},? \d{4}|"
        r"Case No\.\s+\S+|"
        r"Invoice Nos?\.\s+[0-9 ]+)",
        head,
        re.I,
    ):
        refs.append(_norm_space(m.group(1)))
    if refs:
        out["referenced_communications"] = refs[:4]
    if "key_points" not in out:
        body = _norm_space(head)
        if len(body) > 20:
            out["key_points"] = [body[:240]]
    m = re.match(r"([A-Z][a-zA-Z.'-]{2,40})\s*[?:]", _norm_space(head[:80]))
    if m and "recipient" not in out:
        out["recipient"] = m.group(1)
    return out


def extract_compliance_fields(text: str) -> dict[str, Any]:
    head = text[:4000]
    out: dict[str, Any] = {}
    m = re.search(
        r"\bFORM\s+(10-K|10-Q|8-K|S-1|DEF\s*14A|13D|13G|4|20-F|6-K)\b",
        head,
        re.I,
    )
    if m:
        token = re.sub(r"\s+", " ", m.group(1).upper().replace("DEF 14A", "DEF 14A"))
        if token == "4":
            token = "Form 4"
        out["filing_type"] = token
    if "SECURITIES AND EXCHANGE COMMISSION" in head.upper() or re.search(
        r"\bSEC\b", head
    ):
        out["regulatory_body"] = "SEC"
    elif "DIVISION OF CORPORATIONS" in head.upper():
        state = _jurisdiction(head) or "state"
        out["regulatory_body"] = f"{state} Division of Corporations"
    entity = _label_value(
        head,
        ("ENTITY NAME", "Exact name of registrant", "Entity Name"),
    )
    if not entity:
        m = re.search(
            r"\n([A-Z][A-Za-z0-9&.,' -]{3,80})\n\s*\(Exact name of registrant",
            head,
        )
        if m:
            entity = _norm_space(m.group(1))
    if entity:
        out["entity_name"] = entity[:160]
    file_no = _label_value(
        head,
        (
            "Commission File Number",
            "FILE NUMBER",
            "File Number",
            "Confirmation Number",
        ),
    )
    if file_no:
        out["reference_number"] = file_no.split()[0]
    filed = _label_value(head, ("Filing Date", "FILED", "Filed"))
    parsed = parse_date(filed) if filed else first_date(head)
    if parsed:
        out["filing_date"] = parsed
    due = _label_value(head, ("DUE DATE", "Due Date"))
    if due:
        parsed_due = parse_date(due)
        if parsed_due:
            out["due_date"] = parsed_due
    status = _label_value(head, ("STATUS", "Status", "Filing Status"))
    if status:
        low = status.lower()
        if "filed" in low:
            out["status"] = "filed"
        elif "pending" in low:
            out["status"] = "pending"
        else:
            out["status"] = status.split()[0].lower()
    reqs: list[str] = []
    for m in re.finditer(r"^[\-\*]\s+(.+)$", head, re.M):
        item = _norm_space(m.group(1))
        if item:
            reqs.append(item[:200])
    if not reqs:
        m = re.search(
            r"(ANNUAL REPORT PURSUANT TO[^\n]+|Documents Incorporated by Reference:[^\n]+)",
            head,
            re.I,
        )
        if m:
            reqs.append(_norm_space(m.group(1)))
    if reqs:
        out["key_requirements"] = reqs[:6]
    return out


def extract_insurance_fields(text: str) -> dict[str, Any]:
    """CMS Medicare Summary Notice template + FNOL letterhead."""
    head = text[:4000]
    out: dict[str, Any] = {}
    notice = _label_value(
        head,
        ("Notice ID", "Fill Reference", "Claim No.", "Claim Number", "CLAIM NO."),
    )
    if notice:
        out["claim_number"] = notice.split()[0]
    policy = _label_value(head, ("Policy number", "Policy No.", "POLICY NO.", "Policy Number"))
    if policy:
        out["policy_number"] = policy.split()[0]
    insurer = _label_value(head, ("Insurer", "INSURER"))
    if insurer:
        out["insurer"] = insurer.split("\n")[0][:120]
    insured = _label_value(head, ("Insured party", "INSURED", "Insured"))
    if insured:
        out["insured_party"] = insured.split("(")[0].strip()[:120]
    adjuster = _label_value(head, ("ADJUSTER", "Adjuster"))
    if adjuster:
        out["adjuster"] = adjuster.split(",")[0].strip()[:80]
    loss = (
        _label_value(
            head,
            (
                "Date of Loss",
                "DATE OF LOSS",
                "Service start date",
                "Admission date",
                "Date of service",
                "Claim period start",
            ),
        )
    )
    parsed = parse_date(loss) if loss else None
    if parsed:
        out["date_of_loss"] = parsed
    filed = _label_value(head, ("Date Filed", "DATE FILED", "Date filed"))
    if filed:
        parsed_filed = parse_date(filed)
        if parsed_filed:
            out["date_filed"] = parsed_filed
    amount = _label_value(
        head,
        (
            "Claim total paid by Medicare",
            "Total drug cost",
            "CLAIMED AMOUNT",
            "Claimed Amount",
        ),
    )
    money = first_money(amount or "") if amount else first_money(head)
    if money is not None:
        out["claimed_amount"] = money
    det = _label_value(head, ("COVERAGE DETERMINATION", "Coverage Determination", "Stated outcome (verbatim)"))
    if det:
        low = det.lower()
        if "denied" in low:
            out["coverage_determination"] = "denied"
        elif "partial" in low:
            out["coverage_determination"] = "partial"
        elif "pending" in low:
            out["coverage_determination"] = "pending"
        elif "approved" in low or "covered" in low:
            out["coverage_determination"] = "approved"
    desc = _label_value(head, ("DESCRIPTION OF LOSS", "Damages", "DIAGNOSES (ICD-9-CM)"))
    if desc:
        out["damages_description"] = desc[:400]
    docs = _label_value(head, ("SUPPORTING DOCUMENTS", "Supporting Documents"))
    if docs:
        parts = [p.strip() for p in re.split(r"[;•\n]", docs) if p.strip()]
        if parts:
            out["supporting_documents"] = parts[:8]
    return out


_EXTRACTORS = {
    "contract": extract_contract_fields,
    "merger_agreement": extract_contract_fields,
    "corporate_record": extract_corporate_fields,
    "correspondence": extract_correspondence_fields,
    "compliance_filing": extract_compliance_fields,
    "insurance_claim": extract_insurance_fields,
}


def extract_posthoc_fields(doc_class: str | None, text: str | None) -> dict[str, Any]:
    """Schema-shaped labels parsed from ``text``. Empty when there is no extractor."""
    kind = str(doc_class or "").strip()
    fn = _EXTRACTORS.get(kind)
    if fn is None or not str(text or "").strip():
        return {}
    try:
        raw = fn(str(text))
    except Exception:
        return {}
    return {k: v for k, v in (raw or {}).items() if _present(v)}
