"""Contract and scoring for the trained intake classifier (``mailroom-ml``).

``mailroom-ml`` never imports this package: it vendors the label tables and the
intake clerk. :func:`ml_contract` publishes those tables, with golden cases, as
one JSON document (``ml/data/ml_contract.json``) that it can pin and test against.
:func:`score_intake_model` scores the classifier's predictions with the Dojo's
definitions of accuracy, macro F1, calibration and fast-path risk.
"""

from __future__ import annotations

from .contract import CONTRACT_SCHEMA, contract_path, load_contract, ml_contract
from .metrics import score_intake_model

__all__ = [
    "CONTRACT_SCHEMA",
    "contract_path",
    "load_contract",
    "ml_contract",
    "score_intake_model",
]
