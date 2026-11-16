"""#248: what counts as an edit, a confirmed class and a wasted review in export_review_labels.

``_subclass_edited`` is three-state (edited / not edited / unknown): an old snapshot that recorded no
subclass data cannot show that nothing was edited, so it must not make a review "wasted".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import export_review_labels as exp  # noqa: E402


def _label(*, human_sub=None, human_subtype=None, snap=None, **extra):
    base_snap = {"doc_type": "contract", "classification_confidence": 0.8, "extraction_confidence": 0.7}
    base_snap.update(snap or {})
    label = {"decision": "approved", "resolution": "approve_as_is", "doc_type": "contract",
             "doc_subclass": human_sub, "contract_subtype": human_subtype, "field_corrections": None,
             "model_snapshot": base_snap, "resolved_at": "2026-06-01T10:00:00+00:00"}
    label.update(extra)
    return label


def _record(label, **manifest_extra):
    manifest = {"doc_id": "d1", "original_filename": "d1.pdf", "stage": "archived", "human_label": label,
                "classification_attempts": 1, **manifest_extra}
    return exp.to_run_record(manifest, "run")


# -- (1) contract_subtype is a subclass edit too ---------------------------------------------


def test_contract_subtype_only_change_is_a_correction_not_a_wasted_review():
    label = _label(human_subtype="nda", snap={"doc_subclass": None, "contract_subtype": "license"})
    assert exp.is_corrected(label) is True
    assert exp.is_wasted(label) is False


@pytest.mark.parametrize("human, model", [("nda", None), (None, "license")])
def test_setting_or_clearing_a_contract_subtype_is_a_correction(human, model):
    label = _label(human_subtype=human, snap={"contract_subtype": model})
    assert exp.is_corrected(label) is True and exp.is_wasted(label) is False


def test_unchanged_contract_subtype_is_still_a_wasted_review():
    label = _label(human_subtype="nda", snap={"doc_subclass": None, "contract_subtype": "nda"})
    assert exp.is_corrected(label) is False and exp.is_wasted(label) is True


def test_subclass_and_subtype_are_compared_independently():
    same_sub_other_subtype = _label(human_sub="x", human_subtype="b",
                                    snap={"doc_subclass": "x", "contract_subtype": "a"})
    assert exp.is_corrected(same_sub_other_subtype) is True
    other_sub_same_subtype = _label(human_sub="y", human_subtype="a",
                                    snap={"doc_subclass": "x", "contract_subtype": "a"})
    assert exp.is_corrected(other_sub_same_subtype) is True


# -- (2) three states: an unknown is not a confirmation --------------------------------------


def test_subclass_edit_is_three_state():
    assert exp._subclass_edited(_label(human_sub="a", snap={"doc_subclass": "b"}), {"doc_subclass": "b"}) is True
    assert exp._subclass_edited(_label(human_sub="a"), {"doc_subclass": "a"}) is False
    assert exp._subclass_edited(_label(human_sub="a"), {}) is None


def test_snapshot_with_no_subclass_data_is_not_a_wasted_review():
    label = _label(human_sub="nda")
    assert "doc_subclass" not in label["model_snapshot"] and "contract_subtype" not in label["model_snapshot"]
    assert exp.is_corrected(label) is False  # nothing shows an edit...
    assert exp.is_wasted(label) is False  # ...and nothing shows there was none


def test_a_snapshot_recording_either_key_is_known():
    only_subclass = _label(snap={"doc_subclass": None})
    assert exp.is_wasted(only_subclass) is True
    only_subtype = _label(snap={"contract_subtype": None})
    assert exp.is_wasted(only_subtype) is True


def test_unknown_subclass_is_excluded_from_the_wasted_count():
    known = {"human_label": _label(snap={"doc_subclass": None}), "doc_id": "a", "stage": "archived"}
    legacy = {"human_label": _label(), "doc_id": "b", "stage": "archived"}
    summary = exp.summarize([known, legacy])
    assert summary["wasted_reviews"] == 1


# -- (3) a subclass-only override does not name the class ------------------------------------


def _rejected(**extra):
    return _label(decision="rejected", resolution="reject", snap={"doc_subclass": None}, **extra)


@pytest.mark.parametrize("override", [{"doc_subclass": "nda"}, {"contract_subtype": "nda"},
                                      {"doc_subclass": "nda", "contract_subtype": "nda"}])
def test_subclass_only_override_does_not_confirm_the_class(override):
    assert exp._class_confirmed(_rejected(class_override=override), {"doc_type": "contract"}) is False
    assert _record(_rejected(class_override=override)).gold.expected_doc_type is None


def test_doc_type_in_the_override_confirms_the_class_even_when_unchanged():
    label = _rejected(class_override={"doc_type": "contract", "doc_subclass": "nda"})
    assert exp._class_confirmed(label, {"doc_type": "contract"}) is True
    assert _record(label).gold.expected_doc_type == "contract"


def test_a_different_class_still_confirms_without_an_override():
    label = _rejected()
    label["doc_type"] = "correspondence"
    assert exp._class_confirmed(label, {"doc_type": "contract"}) is True


def test_no_override_and_same_class_is_unconfirmed():
    assert exp._class_confirmed(_rejected(), {"doc_type": "contract"}) is False
    assert exp._class_confirmed(_rejected(class_override=None), {"doc_type": "contract"}) is False
