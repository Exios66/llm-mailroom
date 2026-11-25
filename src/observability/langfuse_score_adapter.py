"""Langfuse transport aliases for canonical Mailroom score names."""

from types import MappingProxyType

LANGFUSE_SCORE_NAME_ALIASES = MappingProxyType({
    "extraction_overall_verified_precision": "extraction_verified_precision",
})


def langfuse_score_name(name: str) -> str:
    return LANGFUSE_SCORE_NAME_ALIASES.get(name, name)
