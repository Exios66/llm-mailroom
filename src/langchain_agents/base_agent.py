# VENDORED from github.com/Exios66/llm-entity-extraction (re-vendored to the
# sibling's current HEAD, 2026-08-15 — adds the optional ``callbacks``
# LangChain callback-handler support since the 3a03d5c pin).
# Imported verbatim (import paths rewritten to ``langchain_agents.*``) so the
# eval-validated LangChain sorter/contracts-specialist agents run inside the
# mailroom. Local adaptations (pages/vision, usage/deadline hooks) are marked
# ``MAILROOM PATCH``. Keep diffs against upstream small and documented.


"""Base agent class — LangChain-powered LLM helpers with structured output.

Every mailroom agent is a small LangChain Runnable wrapper: a ``ChatOpenAI``
instance pointed at OpenRouter (or any OpenAI-compatible endpoint via
``OPENROUTER_BASE_URL``) composed with prompt templates and optional JSON
schema parsing.

Design notes
------------
- Prompts are loaded by version from ``src.prompts`` so the evaluation loops
  can test exactly ONE prompt version per Braintrust experiment.
- Structured calls use ``with_structured_output`` (JSON schema) so specialists
  and the sorter emit strict JSON that Braintrust scorers can rely on.
- All calls are traced to Braintrust via ``braintrust.integrations.langchain``
  when the eval runners call ``setup_langchain`` first (they always do).
"""

from __future__ import annotations

import json
import os
import structlog
from abc import ABC, abstractmethod
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from langchain_agents.openrouter_utils import OPENROUTER_BASE_URL

logger = structlog.get_logger(__name__)


def _is_retryable_error(exc: Exception) -> bool:
    """Same retryable classification as llm/retry.py: connection errors,
    timeouts, 429 rate limits, and 5xx server errors are retried; 4xx (incl.
    the Alibaba/Qwen 400 json marker) are never retried."""
    try:
        from llm.retry import _is_retryable

        return _is_retryable(exc)
    except ImportError:
        import openai

        return isinstance(
            exc,
            (
                openai.APIConnectionError,
                openai.APITimeoutError,
                openai.RateLimitError,
            ),
        ) or (getattr(exc, "status_code", None) in (500, 502, 503, 504))


def build_structured_schema(
    properties: dict,
    required: list[str] | None = None,
    additional_properties: bool = False,
    title: str = "StructuredOutput",
) -> dict:
    """Build a JSON schema dict for structured output.

    ``title`` is required by LangChain's ``with_structured_output`` (it is used
    as the function/tool name on OpenAI-compatible endpoints).
    """
    return {
        "type": "object",
        "title": title,
        "properties": properties,
        "required": required or list(properties.keys()),
        "additionalProperties": additional_properties,
    }


class BaseAgent(ABC):
    """Abstract base class for all mailroom agents (LangChain runnables)."""

    agent_name: str

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        callbacks: list | None = None,
    ):
        self.model = model or "qwen/qwen3.7-flash"
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        # Optional LangChain callback handlers (e.g. the Langfuse
        # CallbackHandler) attached to every invoke; None keeps the default
        # (langfuse.openai auto-tracing) path unchanged. Upstream feature,
        # kept aligned with the sibling repo.
        self._callbacks = list(callbacks) if callbacks else None
        self._max_tokens = 4096
        self._max_input_chars = 100_000  # full-document budget; only a hard safety cap
        self._temperature = 0.1
        self._reasoning_effort = None
        self._llm: ChatOpenAI | None = None
        self._last_usage: dict | None = None
        self._last_truncated = False

    @abstractmethod
    def system_prompt(self) -> str:
        """Return the agent's system prompt string."""
        ...

    # ------------------------------------------------------------------
    # MAILROOM PATCH: per-agent skills + tools + memory context. The
    # eval-validated prompt text (issue #10) stays byte-stable; everything
    # below is APPENDED so the agent's working context can grow without
    # touching the versioned prompt.
    # ------------------------------------------------------------------

    def _skill_context(self) -> str:
        try:
            from langchain_agents.skills import load_skills

            return load_skills(self.agent_name)
        except Exception:
            return ""

    def _tool_context(self) -> str:
        try:
            from langchain_agents.toolkit import render_tools

            return render_tools(self.agent_name)
        except Exception:
            return ""

    def _memory_context(self, doc_type: str = "") -> str:
        try:
            from langchain_agents.memory import recent_context

            return recent_context(self.agent_name, doc_type, k=5)
        except Exception:
            return ""

    def augmented_system_prompt(self, doc_type: str = "") -> str:
        """System prompt + agent's skill files + tool descriptions + recent
        outcome memory. Used by _call_llm/_call_structured when no explicit
        system_prompt override is given; the base (eval-validated) prompt is
        always the head, so the versioned behavior is preserved."""
        parts = [self.system_prompt()]
        skills = self._skill_context()
        if skills:
            parts.append(skills)
        tools = self._tool_context()
        if tools:
            parts.append(tools)
        memory = self._memory_context(doc_type)
        if memory:
            parts.append(memory)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # End MAILROOM PATCH
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # LangChain plumbing
    # ------------------------------------------------------------------

    def llm(self) -> ChatOpenAI:
        """Lazily build the LangChain ``ChatOpenAI`` client.

        Uses the OpenRouter base URL so any OpenAI-compatible endpoint
        (Ollama, vLLM) can be swapped in via ``OPENROUTER_BASE_URL``.
        """
        if self._llm is None:
            from langchain_agents.env_utils import load_env

            load_env()
            # MAILROOM PATCH (L-16/L-17): max_retries=0 — the SDK's internal
            # retry layer is disabled so the mailroom's shared retry contract
            # (llm/retry.py) is the SINGLE retry layer. Upstream used
            # max_retries=3, which combined with the wrapper's 3 attempts and
            # the graph's retry loop produced a ~27-call cascade per node.
            self._llm = ChatOpenAI(
                model=self.model,
                api_key=self.api_key or os.environ.get("OPENROUTER_API_KEY") or None,
                base_url=OPENROUTER_BASE_URL,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                timeout=120,
                max_retries=0,
            )
            if self._reasoning_effort:
                self._llm.extra_body = {"reasoning": {"effort": self._reasoning_effort}}
        return self._llm

    # ------------------------------------------------------------------
    # MAILROOM PATCH (L-16/L-17): route LangChain invokes through the
    # mailroom's shared retry contract — retryable-exception filtering,
    # exponential backoff + jitter from taxonomy `llm_retry:`, deadline
    # re-checks between attempts, and the same `llm_retry` event shape as
    # llm/retry.py so observability sees the LangChain agents' retries.
    # ------------------------------------------------------------------

    def _invoke_with_retry(self, fn, *, what: str = "invoke"):
        """Call ``fn()`` retrying transient failures with backoff + jitter.

        Mirrors ``llm/retry.py:retry_chat_completion`` semantics: only
        retryable errors (connection errors, timeouts, 429, 5xx) are retried;
        the run deadline is re-checked before every attempt; backoff/jitter
        come from taxonomy.yaml ``llm_retry:``; the ``llm_retry`` log event
        has the same shape as the native path.
        """
        import random
        import time as _time

        from pipeline.limits import get_call_timeout_seconds

        def _retry_config():
            try:
                from pipeline.config import load_config

                return load_config().get("llm_retry", {}) or {}
            except Exception:
                return {}

        cfg = _retry_config()
        max_attempts = int(cfg.get("max_attempts", 3))
        base_delay = float(cfg.get("base_delay", 1.0))
        max_delay = float(cfg.get("max_delay", 30.0))
        jitter = float(cfg.get("jitter", 0.3))

        attempt = 0
        while True:
            attempt += 1
            self._check_deadline()
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 — inspected below
                if not _is_retryable_error(exc) or attempt >= max_attempts:
                    raise
                delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                delay = delay * (1 + random.uniform(-jitter, jitter))
                logger.warning(
                    "llm_retry",
                    agent=self.agent_name,
                    model=self.model,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=type(exc).__name__,
                    backoff_s=round(delay, 2),
                    what=what,
                )
                _time.sleep(delay)

    # ------------------------------------------------------------------
    # End MAILROOM PATCH (L-16/L-17)
    # ------------------------------------------------------------------

    # Share of the input budget kept from the document's TAIL when truncation
    # fires: deal-critical sections (term, termination, renewal, governing law,
    # signatures) sit in the closing portion of long agreements, which a
    # head-only cap loses entirely — the 292k-char Phasebio agreement's
    # governing-law clause at char 276k is invisible under a 100k head cap.
    TRUNCATION_TAIL_FRACTION = 0.4

    def truncate_input(self, text: str) -> str:
        """Return the FULL document text, capping only past the hard budget.

        The sorter is meant to classify the full document (fully extracted
        markdown text, not a 50-token preview). ``_max_input_chars`` is a
        safety cap for pathological documents only; when it fires, the input
        is kept as a HEAD + TAIL window instead of the head alone: the first
        ``(1 - TRUNCATION_TAIL_FRACTION)`` of the budget from the opening
        (recitals, parties, definitions, early obligations) plus the remaining
        share from the CLOSING portion (term, termination, renewal, governing
        law, signature pages). A marker between the two records the truncation
        on ``_last_truncated`` so callers (and the eval loop's span metadata)
        can see that the row saw partial input.
        """
        if len(text) <= self._max_input_chars:
            self._last_truncated = False
            return text
        self._last_truncated = True
        logger.warning(
            "input_truncated",
            agent=self.agent_name,
            chars=len(text),
            cap=self._max_input_chars,
            tail_fraction=self.TRUNCATION_TAIL_FRACTION,
        )
        budget = max(1, int(self._max_input_chars))
        head = int(budget * (1.0 - self.TRUNCATION_TAIL_FRACTION))
        return (
            text[:head]
            + f"\n\n[... document truncated, {len(text)} total chars; middle omitted — "
              f"closing portion continues below (term, termination, governing law) ...]\n\n"
            + text[-(budget - head):]
        )

    # ------------------------------------------------------------------
    # Completion helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # MAILROOM PATCH: mailroom integration helpers (pages/vision, run
    # deadline, per-call usage accounting). Everything below marked
    # ``MAILROOM PATCH`` is a local adaptation; upstream has none of it.
    # ------------------------------------------------------------------

    def _uses_vision(self) -> bool:
        """True when this agent's model accepts image input. Vision capability
        is config-driven (``config/taxonomy.yaml`` -> ``vision.models``
        substrings) via ``llm/vision.py`` — same rule as agents/base.py."""
        try:
            from llm.vision import is_vision_capable

            return is_vision_capable(self.model)
        except Exception:
            return False

    def _build_user_content(self, text: str, pages: list[str] | None = None):
        """Build the human-message content for a document input.

        Vision-capable models get a multimodal content list (text instruction +
        ``image_url`` data-URI parts for each page). Text-only models get the
        plain string — identical to upstream behaviour when no pages are passed.
        """
        if pages and self._uses_vision():
            parts: list[dict] = [{"type": "text", "text": text}]
            for uri in pages:
                parts.append({"type": "image_url", "image_url": {"url": uri}})
            return parts
        return text

    def _check_deadline(self) -> None:
        """Abort (RunDeadlineExceeded) when the run's wall-clock deadline has
        passed — mirrors the mailroom's retry_chat_completion deadline guard."""
        try:
            from pipeline.limits import check_run_deadline, get_run_deadline

            check_run_deadline(get_run_deadline())
        except ImportError:
            pass

    def _record_usage(self, usage) -> None:
        """Feed per-call usage into the run's token/cost accumulator
        (pipeline.limits.record_usage) so Langfuse scores and run budgets see
        the LangChain agents' token spend like every other agent's."""
        try:
            from pipeline.limits import record_usage

            record_usage(usage, self.model)
        except ImportError:
            pass

    @staticmethod
    def _usage_from_message(message) -> dict:
        if message is None:
            return {}
        return getattr(message, "usage_metadata", None) or (message.response_metadata or {}).get("usage") or {}

    # ------------------------------------------------------------------
    # End MAILROOM PATCH
    # ------------------------------------------------------------------

    def _call_llm(
        self,
        user_message: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        pages: list[str] | None = None,  # MAILROOM PATCH: page-image data-URIs
    ) -> str:
        """Plain text completion via the LangChain chain.

        Args:
            user_message: The user-facing message content.
            system_prompt: System prompt (defaults to self.system_prompt()).
            temperature: Sampling temperature (defaults to self._temperature).
            max_tokens: Max output tokens (defaults to self._max_tokens).
            reasoning_effort: Reasoning effort level for Qwen models.
            pages: MAILROOM PATCH — page-image data-URIs appended as multimodal
                content when the model is vision-capable.

        Returns:
            The model's response text.
        """
        llm = self.llm()
        if temperature is not None or max_tokens is not None:
            llm = llm.bind(
                temperature=temperature if temperature is not None else self._temperature,
                max_tokens=max_tokens or self._max_tokens,
            )
        if reasoning_effort:
            llm = llm.bind(extra_body={"reasoning": {"effort": reasoning_effort}})

        self._check_deadline()  # MAILROOM PATCH
        system = system_prompt or self.augmented_system_prompt()
        # System prompts are literal text (they may legally contain curly
        # braces, e.g. embedded JSON schemas) — never template-parsed.
        messages = [
            SystemMessage(content=system),
            HumanMessage(content=self._build_user_content(user_message, pages)),  # MAILROOM PATCH
        ]

        logger.info("llm_call", agent=self.agent_name, model=self.model,
                    pages=len(pages) if pages else None)  # MAILROOM PATCH
        response = self._invoke_with_retry(  # MAILROOM PATCH (L-16/L-17)
            lambda: llm.invoke(messages, config={"callbacks": self._callbacks} if self._callbacks else None),
            what="llm",
        )
        content = response.content if isinstance(response.content, str) else str(response.content)
        self._last_usage = {  # MAILROOM PATCH: usage accounting parity with _call_structured
            "prompt_tokens": self._usage_from_message(response).get("input_tokens")
            or self._usage_from_message(response).get("prompt_tokens") or 0,
            "completion_tokens": self._usage_from_message(response).get("output_tokens")
            or self._usage_from_message(response).get("completion_tokens") or 0,
            "total_tokens": self._usage_from_message(response).get("total_tokens") or 0,
            "cost": (response.response_metadata or {}).get("cost"),
        }
        self._record_usage(self._usage_from_message(response))  # MAILROOM PATCH
        logger.info("llm_response", agent=self.agent_name, length=len(content))
        return content

    def _call_structured(
        self,
        user_message: str,
        json_schema: dict,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        pages: list[str] | None = None,  # MAILROOM PATCH: page-image data-URIs
    ) -> dict:
        """Structured JSON extraction via ``with_structured_output``.

        Args:
            user_message: User message containing the document text.
            json_schema: JSON schema dict describing expected output structure.
            system_prompt: Override system prompt.
            temperature: Sampling temperature.
            max_tokens: Max output tokens.
            pages: MAILROOM PATCH — page-image data-URIs appended as multimodal
                content when the model is vision-capable.

        Returns:
            Parsed JSON dict, or {"_raw": raw_text, "_parse_error": True} on failure.
        """
        llm = self.llm()
        if temperature is not None or max_tokens is not None:
            llm = llm.bind(
                temperature=temperature if temperature is not None else self._temperature,
                max_tokens=max_tokens or self._max_tokens,
            )
        try:
            structured = llm.with_structured_output(
                json_schema, method="json_schema", include_raw=True
            )
        except Exception:  # pragma: no cover - older SDKs fall back to prompting
            structured = llm.with_structured_output(json_schema, method="function_calling", include_raw=True)

        self._check_deadline()  # MAILROOM PATCH
        system = system_prompt or self.augmented_system_prompt()
        # Literal SystemMessage: system prompts may contain curly braces
        # (embedded JSON schemas) and must not be template-parsed.
        messages = [
            SystemMessage(content=system),
            HumanMessage(content=self._build_user_content(user_message, pages)),  # MAILROOM PATCH
        ]

        logger.info("llm_structured_call", agent=self.agent_name, model=self.model,
                    pages=len(pages) if pages else None)  # MAILROOM PATCH
        raw_out: Any = self._invoke_with_retry(  # MAILROOM PATCH (L-16/L-17)
            lambda: structured.invoke(messages, config={"callbacks": self._callbacks} if self._callbacks else None),
            what="structured"
        )

        # include_raw=True returns {"raw": AIMessage, "parsed": ..., "parsing_error": ...}
        if isinstance(raw_out, dict):
            message = raw_out.get("raw")
            result = raw_out.get("parsed")
            parsing_error = raw_out.get("parsing_error")
        else:
            message = getattr(raw_out, "raw", None)
            result = getattr(raw_out, "parsed", None)
            parsing_error = getattr(raw_out, "parsing_error", None)

        # Capture usage/cost from the raw AIMessage for the Braintrust cost scorer.
        if message is not None:
            usage = getattr(message, "usage_metadata", None) or (message.response_metadata or {}).get("usage") or {}
            self._last_usage = {
                "prompt_tokens": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
                "completion_tokens": usage.get("output_tokens") or usage.get("completion_tokens") or 0,
                "total_tokens": usage.get("total_tokens") or 0,
                "cost": (message.response_metadata or {}).get("cost"),
            }
            self._record_usage(usage)  # MAILROOM PATCH
        else:
            self._last_usage = None

        if result is None and parsing_error is not None:
            logger.error("structured_output_parse_error", agent=self.agent_name, error=str(parsing_error))
            raw_text = ""
            if message is not None:
                raw_text = message.content if isinstance(message.content, str) else ""
            return {"_raw": raw_text, "_parse_error": True}

        if not isinstance(result, dict):
            try:
                result = result.model_dump()
            except AttributeError:
                logger.error("structured_output_unparseable", agent=self.agent_name)
                return {"_raw": str(result), "_parse_error": True}

        logger.info("llm_structured_response", agent=self.agent_name, keys=list(result.keys()))
        return result

    def _call_vision(
        self,
        system_prompt: str,
        user_text: str,
        image_base64: str,
        image_format: str = "png",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Vision classification call: system prompt + user text + ONE image.

        The image is sent as an inline data URI (``data:image/png;base64,...``)
        in a multimodal LangChain message — the same payload shape the
        RVL-CDIP classifier uses, but through the LangChain stack so the call
        is traced to Braintrust like every other agent call.

        Args:
            system_prompt: The classification rules (e.g. the intro half of the
                vision prompt, split at ``## Output format``).
            user_text: The output-format contract + any worked examples.
            image_base64: The document page image, base64-encoded.
            image_format: Image MIME format ('png', 'jpeg').
            temperature: Sampling temperature.
            max_tokens: Max output tokens.

        Returns:
            The model's raw response text.
        """
        return self._call_vision_multi(
            system_prompt=system_prompt,
            user_text=user_text,
            images=[(image_base64, image_format)],
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def _call_vision_multi(
        self,
        system_prompt: str,
        user_text: str,
        images: list[tuple[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Vision classification call with MULTIPLE page images in ONE request.

        The FULL document (every rendered page) is sent in a single call so the
        model sees the entire PDF at once — one classification per PDF, not one
        per page. ``images`` is a list of ``(base64, format)`` tuples, each
        attached as an inline data URI.
        """
        from langchain_core.messages import HumanMessage, SystemMessage

        self._check_deadline()  # MAILROOM PATCH
        llm = self.llm()
        if temperature is not None or max_tokens is not None:
            llm = llm.bind(
                temperature=temperature if temperature is not None else self._temperature,
                max_tokens=max_tokens or self._max_tokens,
            )

        content: list[dict] = [{"type": "text", "text": user_text}]
        for base64_image, image_format in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/{image_format};base64,{base64_image}"},
            })

        messages = [SystemMessage(content=system_prompt), HumanMessage(content=content)]

        logger.info("llm_vision_call", agent=self.agent_name, model=self.model, pages=len(images))
        response = self._invoke_with_retry(  # MAILROOM PATCH (L-16/L-17)
            lambda: llm.invoke(messages, config={"callbacks": self._callbacks} if self._callbacks else None),
            what="vision",
        )
        raw_content = response.content if isinstance(response.content, str) else str(response.content)

        usage = getattr(response, "usage_metadata", None) or (response.response_metadata or {}).get("usage") or {}
        self._last_usage = {
            "prompt_tokens": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
            "completion_tokens": usage.get("output_tokens") or usage.get("completion_tokens") or 0,
            "total_tokens": usage.get("total_tokens") or 0,
            "cost": (response.response_metadata or {}).get("cost"),
        }
        self._record_usage(usage)  # MAILROOM PATCH
        logger.info("llm_vision_response", agent=self.agent_name, length=len(raw_content))
        return raw_content
