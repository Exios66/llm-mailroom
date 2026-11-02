"""Cardinality validation for scorers consuming paired document sequences."""


def require_equal_lengths(expected, predicted) -> None:
    """Reject incomplete document pairs before normalization or row filtering."""
    if len(expected) != len(predicted):
        raise ValueError(
            f"expected length {len(expected)} != predicted length {len(predicted)}"
        )
