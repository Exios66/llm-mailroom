"""Compatibility imports for the Dojo-owned bootstrap engine."""

from llm_dojo_scoring.bootstrap import (
    DEFAULT_ALPHA,
    DEFAULT_N_BOOT,
    DEFAULT_SEED,
    bootstrap_ci,
    delta_significance,
)

__all__ = [
    "DEFAULT_ALPHA", "DEFAULT_N_BOOT", "DEFAULT_SEED",
    "bootstrap_ci", "delta_significance",
]
