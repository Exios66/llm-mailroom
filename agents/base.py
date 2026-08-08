import json
import structlog
from abc import ABC, abstractmethod

from llm.client import get_llm
from llm.retry import retry_chat_completion
from observability.tracing import langfuse_call_attrs

logger = structlog.get_logger(__name__)

# Reasonable default so a single agent can never run away generating tokens
# (qwen "flash" models emit heavy reasoning output). Per-agent caps live in
# taxonomy.yaml under agents.<name>.max_tokens.
_DEFAULT_MAX_TOKENS = 4096

# Default input-text budget per agent; override per agent in taxonomy.yaml via
# agents.<name>.max_input_chars (large docs — e.g. 52-page contracts — need a
# bigger window or key fields from later pages get truncated away).
_DEFAULT_MAX_INPUT_CHARS = 25000

# Literal `json` token injected into the SYSTEM message of every structured
# call. Some providers (Qwen via Alibaba) gate `response_format: json_object`
# on the word "json" appearing somewhere in the messages array — the user
# message alone is not always sufficient (pilot: contract_01 first extract
# failed with HTTP 400 while the identical call succeeded on retry), so we
# guarantee it in both messages.
_JSON_MODE_SYSTEM_NOTE = (
    "\n\nOutput must be a single json object conforming to the provided "
    "json schema (response_format is json_object)."
)


class BaseAgent(ABC):
    agent_name: str

    def __init__(self):
        self.client, self.model = get_llm(self.agent_name)
        # Set by system_prompt() when a Langfuse-managed prompt is active;
        # passed to the LLM call as `langfuse_prompt=` so generations link to
        # the exact prompt version used.
        self._langfuse_prompt = None

    @abstractmethod
    def system_prompt(self) -> str:
        ...

    def _configured_max_tokens(self) -> int:
        from pipeline.config import get_agent_config

        try:
            return get_agent_config(self.agent_name).get("max_tokens", _DEFAULT_MAX_TOKENS)
        except Exception:
            return _DEFAULT_MAX_TOKENS

    def _configured_max_input_chars(self) -> int:
        from pipeline.config import get_agent_config

        try:
            return int(
                get_agent_config(self.agent_name).get(
                    "max_input_chars", _DEFAULT_MAX_INPUT_CHARS
                )
            )
        except Exception:
            return _DEFAULT_MAX_INPUT_CHARS

    def _truncate_input(self, text: str) -> str:
        """Truncate document text to the agent's configured input budget,
        marking the truncation so downstream code can react to it."""
        max_chars = self._configured_max_input_chars()
        if len(text) <= max_chars:
            return text
        return (
            text[:max_chars]
            + f"\n\n[... document truncated, {len(text)} total chars ...]"
        )

    def _configured_reasoning_effort(self) -> str | None:
        from pipeline.config import get_agent_config

        try:
            return get_agent_config(self.agent_name).get("reasoning_effort")
        except Exception:
            return None

    def _call_llm(
        self,
        user_message: str,
        response_format: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system_prompt: str | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        from pipeline.limits import get_run_deadline, record_usage

        messages = [
            {"role": "system", "content": system_prompt or self.system_prompt()},
            {"role": "user", "content": user_message},
        ]
        kwargs = {"model": self.model, "messages": messages}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if response_format:
            kwargs["response_format"] = response_format
        if max_tokens is None:
            max_tokens = self._configured_max_tokens()
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if reasoning_effort is None:
            reasoning_effort = self._configured_reasoning_effort()
        if reasoning_effort:
            kwargs["extra_body"] = {"reasoning": {"effort": reasoning_effort}}
        kwargs.update(langfuse_call_attrs(self.agent_name))
        langfuse_prompt = getattr(self, "_langfuse_prompt", None)
        if langfuse_prompt is not None:
            kwargs["langfuse_prompt"] = langfuse_prompt
        kwargs["run_deadline"] = get_run_deadline()

        logger.info("llm_call", agent=self.agent_name, model=self.model, max_tokens=max_tokens)
        response = retry_chat_completion(self.client, **kwargs)
        record_usage(getattr(response, "usage", None), self.model)
        content = response.choices[0].message.content or ""
        logger.info("llm_response", agent=self.agent_name, length=len(content))
        return content

    def _call_structured(
        self,
        user_message: str,
        json_schema: dict,
        temperature: float = 0.1,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> dict:
        # `json_object` response format is broadly supported across OpenRouter
        # providers (OpenAI `json_schema` strict mode is not). The schema is
        # embedded in the prompt, and the lowercase "json" wording is required
        # verbatim by some providers (e.g. Qwen via Alibaba) whose gate rejects
        # requests that lack the literal token `json` in the messages — an
        # uppercase-only "JSON" does not satisfy it. The token is guaranteed in
        # BOTH the system message (see _JSON_MODE_SYSTEM_NOTE) and the user
        # message below: the pilot showed Alibaba intermittently rejecting a
        # user-message-only variant with HTTP 400.
        schema_text = json.dumps(json_schema)
        user_message = (
            f"{user_message}\n\n"
            "Return ONLY a valid json object that conforms to the schema below. "
            "Do not include any text outside the json object. Output strict JSON only.\n\n"
            f"JSON schema:\n{schema_text}"
        )
        base_system = system_prompt if system_prompt is not None else self.system_prompt()
        system_prompt = f"{base_system}{_JSON_MODE_SYSTEM_NOTE}"
        raw = self._call_llm(
            user_message,
            response_format={"type": "json_object"},
            temperature=temperature,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.error("json_parse_failed", agent=self.agent_name, raw=raw[:200])
            return {"_raw": raw, "_parse_error": True}


def build_structured_schema(
    properties: dict,
    required: list[str] | None = None,
    additional_properties: bool = False,
) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or list(properties.keys()),
        "additionalProperties": additional_properties,
    }
