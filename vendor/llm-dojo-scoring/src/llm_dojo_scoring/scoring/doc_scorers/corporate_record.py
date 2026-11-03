"""``corporate_record``: always the detailed, per-record-type suite path.

The corporate suite aligns fields per record type (bylaws, board resolution, ...)
only on its detailed path, so this module pins ``detailed=True``; tier 2 normalises
the record-type surface. No extras: everything is in the suite's field map.
"""

from __future__ import annotations

from ...schemas.corpus import DOC_TYPE_SUBCLASSES
from .base import TypeScorer

SCORER = TypeScorer(
    doc_type="corporate_record",
    subclasses=DOC_TYPE_SUBCLASSES["corporate_record"],
    detailed=True,
)
