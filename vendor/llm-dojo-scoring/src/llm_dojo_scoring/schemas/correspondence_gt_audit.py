"""Audit correspondence ground truth for templated and metadata-derived labels (#94).

The SAND-042 RCA found that correspondence quality tracks the GT design more
than the model:

- only ``intent``, ``subject_matter`` and ``keywords`` carry GT, so eight of
  the eleven live fields are never scored;
- ``subject_matter`` looked templated (``"Correspondence concerning
  general_business."``) and ``keywords`` looked like folder and timestamp
  tokens, which a grounded answer cannot match.

The second point is an *inference*. This module measures it against real Hub
rows, the way :func:`~.corporate_record_schema.audit_gt_rows` does for
corporate records. Its rules are deliberately narrow, so a flagged value is
template- or metadata-shaped rather than merely terse:

- ``subject_matter_templated`` — the value starts with a fixed sentence frame
  around a topic key (``Correspondence concerning <topic>`` followed by the end,
  ``.`` or ``:`` and anything, as in ``"Correspondence concerning
  general_business: Construction Duration"``), or it embeds a snake_case topic
  key from :data:`.corpus.CORRESPONDENCE_TOPICS`;
- ``keywords_metadata`` — at least one keyword is *strongly* metadata (see
  below); weak metadata alone does not fire, because real content keywords such
  as ``"press release"``, ``"Meeting Request"``, ``"letter"``, ``"2:00 PM"``,
  ``"Calendar"`` or a contact's email address look like it;
- ``keywords_all_metadata`` — every keyword is strong or weak metadata and at
  least one is strong (the 5-slot templated shape);
- ``intent_missing`` — the row has no usable ``intent`` GT;
- ``gt_unparseable`` — ``gt_fields`` could not be parsed.

Keyword metadata has two tiers (:func:`metadata_keyword_kind`):

- **strong** — a topic key from :data:`.corpus.CORRESPONDENCE_TOPICS`; a date
  *and* time stamp (``2000-06-26T09:40:00+00:00``, ``05/14/2001 09:32``,
  ``20010514093200``); a path with a folder segment (``allen-p/_sent_mail/3.``);
  or an underscore-prefixed folder (``_sent_mail``, ``_americas``);
- **weak** — a communication-type token (the ``correspondence`` vocabulary of
  :data:`.corpus.CORPUS_SUBCLASS_SURFACES`), a plain folder name (``inbox``), an
  Enron maildir mailbox id (``delainey-d``, ``williams-w3``), a bare date, a bare
  clock time, or an email address.

Templated rows carry the 5-slot shape ``[topic_key, communication_type,
mailbox id, folder, ISO timestamp]``, so they always contain a strong keyword.
Weak tokens can be real content, and the mailbox pattern also matches
hyphenated words such as ``plan-b`` or ``vitamin-c``, so they never fire alone.

Known gap: mailbox ids whose owner part has digits or underscores
(``bill_williams_iii``) are not matched; there is no safe rule for them. Such
slots stay unflagged, so ``keywords_all_metadata`` can under-count templated
rows.

The report also states the scored population: how many rows populate each
live field, and how many live fields each row populates.

It proposes no relabels. A relabel needs the audit's numbers first (#94 step 2).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from .corpus import CORRESPONDENCE_TOPICS, RAW_CORPUS_SUBCLASS_SURFACES
from .gt_metadata import (
    ABSENT_PRESENCE_STATUSES,
    gt_presence_map,
    is_empty_value,
    parse_gt_fields,
    parse_json_container,
)

__all__ = [
    "CORRESPONDENCE_DEFECT_CODES",
    "audit_correspondence_gt",
    "audit_correspondence_gt_row",
    "correspondence_populated_fields",
    "is_metadata_keyword",
    "is_templated_subject_matter",
    "metadata_keyword_kind",
]

#: Every defect code the audit can emit.
CORRESPONDENCE_DEFECT_CODES: tuple[str, ...] = (
    "subject_matter_templated",
    "keywords_metadata",
    "keywords_all_metadata",
    "intent_missing",
    "gt_unparseable",
)

_TOPIC_KEYS = frozenset(CORRESPONDENCE_TOPICS)
#: ``communication_type`` vocabulary (the 8 Hub tokens), normalized like keywords.
_COMMUNICATION_TYPES = frozenset(RAW_CORPUS_SUBCLASS_SURFACES["correspondence"])
# The frame must START the value; the topic is followed by the end, a period,
# or a colon and anything (the real GT appends ": <title>" to the frame).
_TEMPLATE_FRAME = re.compile(
    r"^\s*(?:an?\s+)?(?:correspondence|email|e-mail|message|communication)\s+"
    # The topic is words joined by single-or-more spaces (not a lazy ``[a-z_ ]+?``,
    # which is quadratic on a long run of trailing spaces).
    r"(?:concerning|regarding|about|re:?|on)\s+([a-z_]+(?:[ ]+[a-z_]+)*)\s*(?=$|\.|:)",
    re.IGNORECASE,
)
_SNAKE_TOKEN = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")
_MONTH = r"(?:0?[1-9]|1[0-2])"
_DAY = r"(?:0?[1-9]|[12]\d|3[01])"
_CLOCK = r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
_ZONE = r"(?:\s*(?:z|utc|gmt|[+-]\d{2}:?\d{2}))"
_TIME = rf"{_CLOCK}(?:\s*[ap]\.?m\.?)?{_ZONE}?"
_DATE_OR_TIME = re.compile(
    # Year-first date (any of - / .), optionally with a time and zone:
    # 2001-05-14, 2000-06-26T09:40:00+00:00, 2001/5/14 9:32.
    rf"^(?:(?:19|20)\d{{2}}[-/.]{_MONTH}[-/.]{_DAY}(?:[ t]{_TIME})?"
    # Day/month-first date; dots only with a four-digit year, so version
    # numbers like 2.0.1 and 10.12.15 are not dates: 05/14/2001 09:32.
    rf"|\d{{1,2}}[-/]\d{{1,2}}[-/](?:\d{{4}}|\d{{2}})(?:[ t]{_TIME})?"
    rf"|\d{{1,2}}\.\d{{1,2}}\.\d{{4}}(?:[ t]{_TIME})?"
    # Bare clock time: 9:32 am, 09:40:00Z.
    rf"|{_TIME}"
    # Compact stamp YYYYMMDD[hhmm[ss]] with a plausible month, day and time.
    r"|(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
    r"(?:(?:[01]\d|2[0-3])[0-5]\d(?:[0-5]\d)?)?)$",
    re.IGNORECASE,
)
#: A date *and* time (or a compact stamp with hhmm): strong metadata. A bare date
#: or bare clock time is only weak.
_TIMESTAMP = re.compile(
    rf"^(?:(?:19|20)\d{{2}}[-/.]{_MONTH}[-/.]{_DAY}[ t]{_TIME}"
    rf"|\d{{1,2}}[-/]\d{{1,2}}[-/](?:\d{{4}}|\d{{2}})[ t]{_TIME}"
    rf"|\d{{1,2}}\.\d{{1,2}}\.\d{{4}}[ t]{_TIME}"
    r"|(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
    r"(?:[01]\d|2[0-3])[0-5]\d(?:[0-5]\d)?)$",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)
#: Enron maildir mailbox id: ``lastname-i`` with an optional digit suffix
#: (``delainey-d``, ``DASOVICH-J``, ``williams-w3``).
_MAILBOX_ID = re.compile(r"^[a-z]+(?:-[a-z]+)*-[a-z]\d*$", re.IGNORECASE)
#: Enron / Outlook mailbox folder names (lower-case, spaces as underscores).
_FOLDER_NAMES = frozenset(
    {
        "inbox",
        "sent",
        "sent_items",
        "sent_mail",
        "_sent_mail",
        "deleted_items",
        "all_documents",
        "discussion_threads",
        "notes_inbox",
        "calendar",
        "contacts",
        "drafts",
        "outbox",
        # Observed in the Enron maildir snapshot.
        "_americas",
        "meetings",
        "ect_admin",
        "e_mail_bin",
        "to_do",
        "personal",
        "ferc",
        "enron_power",
        "private_folders",
        "misc",
    }
)
#: Folder names that are also ordinary content words. They are weak keywords, but
#: do not make ``FERC/ISO`` or ``Personal/Confidential`` a metadata path.
_CONTENT_LIKE_FOLDERS = frozenset({"meetings", "personal", "ferc", "misc", "enron_power"})

#: Keyword metadata tier; see :func:`metadata_keyword_kind`.
KeywordKind = Literal["strong", "weak"]


def _norm_token(value: Any) -> str:
    """Lower-cased, trimmed text with inner whitespace runs as underscores."""
    return re.sub(r"\s+", "_", str(value).strip().lower())


def is_templated_subject_matter(value: Any) -> bool:
    """True when ``subject_matter`` is a topic-key template, not a summary."""
    if not isinstance(value, str) or is_empty_value(value):
        return False
    text = value.strip()
    frame = _TEMPLATE_FRAME.match(text)
    if frame and _norm_token(frame.group(1)) in _TOPIC_KEYS:
        return True
    return any(token in _TOPIC_KEYS for token in _SNAKE_TOKEN.findall(text.lower()))


def metadata_keyword_kind(value: Any) -> KeywordKind | None:
    """Classify a keyword as ``"strong"`` metadata, ``"weak"`` metadata or content (``None``).

    Strong: a topic key, a date *and* time stamp, a path with a folder segment, or
    an underscore-prefixed folder (``_sent_mail``). Weak: a communication-type
    token, a plain folder name, an Enron mailbox id, a bare date, a bare clock
    time, or an email address. Weak values can be real content (``"letter"``,
    ``"2:00 PM"``, ``plan-b``), so the audit only acts on them next to a strong
    one. A ``/`` alone does not make a path (``SPP/Entergy``).
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    token = _norm_token(text)
    if token in _TOPIC_KEYS:
        return "strong"
    if token in _FOLDER_NAMES and token.startswith("_"):
        return "strong"
    if _TIMESTAMP.match(text):
        return "strong"
    if "/" in text or "\\" in text:
        segments = (_norm_token(seg) for seg in re.split(r"[\\/]+", text) if seg)
        if any(seg in _FOLDER_NAMES and seg not in _CONTENT_LIKE_FOLDERS for seg in segments):
            return "strong"
    if token in _FOLDER_NAMES or token in _COMMUNICATION_TYPES:
        return "weak"
    if _DATE_OR_TIME.match(text) or _EMAIL.match(text) or _MAILBOX_ID.match(text):
        return "weak"
    return None


def is_metadata_keyword(value: Any) -> bool:
    """True when a keyword *could be* metadata (strong or weak).

    This is the permissive test; it is not what fires ``keywords_metadata``.
    Use :func:`metadata_keyword_kind` for the tier.
    """
    return metadata_keyword_kind(value) is not None


def _keyword_list(value: Any) -> list[str]:
    """Keywords as a list of strings.

    A string is read as a JSON list, then as a Python list literal
    (``"['email', 'foo']"``), and only then split on commas / semicolons.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            parsed = parse_json_container(text)
            if not isinstance(parsed, list):
                try:
                    parsed = ast.literal_eval(text)
                except (ValueError, SyntaxError, MemoryError, RecursionError):
                    parsed = None
            if isinstance(parsed, (list, tuple)):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [part.strip() for part in re.split(r"[;,]", text) if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _parse_fields(gt_fields: Any) -> dict[str, Any]:
    """:func:`parse_gt_fields`, with parser blow-ups reported as ``ValueError``."""
    try:
        return parse_gt_fields(gt_fields)
    except (RecursionError, MemoryError):
        raise ValueError("gt_fields is too deeply nested or too large to parse") from None


def _usable(fields: Mapping[str, Any], presence: Mapping[str, str], field: str) -> bool:
    """A field carries expected GT: present, not absent-by-presence, not empty."""
    if presence.get(field) in ABSENT_PRESENCE_STATUSES:
        return False
    return field in fields and not is_empty_value(fields[field])


def correspondence_populated_fields(gt_fields: Any) -> list[str]:
    """Live correspondence fields that carry expected GT for one row, in map order."""
    from ..scoring.suites import DEFAULT_FIELD_TYPES

    fields = _parse_fields(gt_fields)
    presence = gt_presence_map(fields)
    return [f for f in DEFAULT_FIELD_TYPES["correspondence"] if _usable(fields, presence, f)]


def audit_correspondence_gt_row(gt_fields: Any) -> list[str]:
    """Defect codes for one correspondence row's ``gt_fields`` (``[]`` when clean).

    ``TypeError`` / ``ValueError`` from parsing propagate (a too-deeply-nested
    or oversized value is reported as ``ValueError``); :func:`audit_correspondence_gt`
    turns them into ``gt_unparseable``.
    """
    fields = _parse_fields(gt_fields)
    presence = gt_presence_map(fields)
    defects: list[str] = []
    if _usable(fields, presence, "subject_matter") and is_templated_subject_matter(
        fields["subject_matter"]
    ):
        defects.append("subject_matter_templated")
    if _usable(fields, presence, "keywords"):
        keywords = _keyword_list(fields["keywords"])
        kinds = [metadata_keyword_kind(k) for k in keywords]
        if "strong" in kinds:
            defects.append("keywords_metadata")
            if all(kind is not None for kind in kinds):
                defects.append("keywords_all_metadata")
    if not _usable(fields, presence, "intent"):
        defects.append("intent_missing")
    return defects


def audit_correspondence_gt(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate :func:`audit_correspondence_gt_row` over Hub ground-truth rows.

    Each row needs ``expected`` (doc class) and ``gt_fields``; rows of other
    classes are skipped. Returns:

    - ``n_rows`` — correspondence rows audited;
    - ``totals`` — defect code → row count;
    - ``field_population`` — live field → rows that populate it;
    - ``populated_field_counts`` — number of populated live fields → rows;
    - ``unscored_fields`` — live fields that no row populates.
    """
    from ..scoring.suites import DEFAULT_FIELD_TYPES

    live = list(DEFAULT_FIELD_TYPES["correspondence"])
    totals: dict[str, int] = {}
    population = dict.fromkeys(live, 0)
    histogram: dict[int, int] = {}
    n_rows = 0
    for row in rows:
        if row.get("expected") != "correspondence":
            continue
        n_rows += 1
        raw = row.get("gt_fields")
        raw = {} if raw is None else raw
        try:
            defects = audit_correspondence_gt_row(raw)
            populated = correspondence_populated_fields(raw)
        except (TypeError, ValueError, RecursionError, MemoryError):
            defects, populated = ["gt_unparseable"], []
        for code in defects:
            totals[code] = totals.get(code, 0) + 1
        for field in populated:
            population[field] += 1
        histogram[len(populated)] = histogram.get(len(populated), 0) + 1
    return {
        "n_rows": n_rows,
        "totals": totals,
        "field_population": population,
        "populated_field_counts": dict(sorted(histogram.items())),
        "unscored_fields": [f for f in live if population[f] == 0] if n_rows else [],
    }
