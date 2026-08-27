"""MAILROOM-LOCAL (not from upstream): deterministic fake LangChain LLM.

The vendored LangChain agents build their own ``ChatOpenAI`` and bypass
``llm.client.get_llm``, so mock pilot runs and the test suite must patch
``langchain_agents.base_agent.BaseAgent.llm`` instead. This fake mirrors the
contract of the real client: ``bind`` is chainable, ``with_structured_output``
returns a structured runnable whose ``invoke`` yields
``{"raw", "parsed", "parsing_error"}``, and plain ``invoke`` yields an
AIMessage-like object. Canned responses are keyed off the user message text
(same markers as ``scripts/run_pilot.py``'s ``_fake_client``).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

# Canned usage shape consumed by pipeline.limits.record_usage (dict form).
DEFAULT_USAGE = {"input_tokens": 120, "output_tokens": 60, "total_tokens": 180}

# Markers in the user message that select the canned classification response
# (mirrors the mailroom's historical mock keying in run_pilot._fake_client).
_CLASSIFY_MARKERS = ("Classify this legal document", "RE-EVALUATION REQUESTED")


def user_text_from_messages(messages) -> str:
    """Extract the human text from a LangChain message list, handling
    multimodal list content (text + image_url parts) built by the vendored
    base agent when page images are attached."""
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if isinstance(content, list):
            texts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            return "\n".join(texts)
        if isinstance(content, str):
            return content
    return ""


def is_classify_call(user_text: str) -> bool:
    return any(marker in user_text for marker in _CLASSIFY_MARKERS)


class FakeLangChainLLM:
    """Replacement for the ChatOpenAI instance the vendored agents construct.

    - ``classification``: canned sorter output dict (doc_type / contract_subtype /
      confidence / reasoning).
    - ``extraction``: canned specialist output dict.
    - ``on_call``: optional callback ``(user_text, parsed_dict)`` fired on every
      invocation (the pilot uses it to record metrics).
    """

    def __init__(self, classification=None, extraction=None, usage=None, on_call=None):
        self.classification = classification or {
            "doc_type": "contract",
            "contract_subtype": "other",
            "confidence": 0.95,
            "reasoning": "mock",
        }
        self.extraction = extraction or {
            "confidence": 0.9,
            "document_name": "Mock Agreement",
            "parties": ["Mock Party"],
        }
        self.usage = usage if usage is not None else dict(DEFAULT_USAGE)
        self.on_call = on_call
        self.calls = 0

    def bind(self, **kwargs):
        return self

    def with_structured_output(self, json_schema, **kwargs):
        return _FakeStructuredRunner(self)

    def invoke(self, messages, **kwargs):
        return self._run(messages)

    def _run(self, messages):
        self.calls += 1
        text = user_text_from_messages(messages)
        parsed = dict(self.classification if is_classify_call(text) else self.extraction)
        if self.on_call:
            self.on_call(text, parsed)
        return self._make_message(parsed)

    def _make_message(self, parsed: dict):
        msg = MagicMock()
        msg.content = json.dumps(parsed)
        msg.usage_metadata = dict(self.usage)
        msg.response_metadata = {}
        return msg


class _FakeStructuredRunner:
    """Runnable returned by ``with_structured_output``: invoke() yields the
    include_raw contract ``{"raw": AIMessage, "parsed": dict,
    "parsing_error": None}`` the vendored ``_call_structured`` unpacks."""

    def __init__(self, owner: FakeLangChainLLM):
        self._owner = owner

    def invoke(self, messages, **kwargs):
        raw = self._owner._run(messages)
        parsed = json.loads(raw.content)
        return {"raw": raw, "parsed": parsed, "parsing_error": None}
