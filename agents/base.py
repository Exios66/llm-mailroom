import json
import structlog
from abc import ABC, abstractmethod

from llm.client import get_llm, get_llm_model

logger = structlog.get_logger(__name__)


class BaseAgent(ABC):
    agent_name: str

    def __init__(self):
        self.client, self.model = get_llm(self.agent_name)

    @abstractmethod
    def system_prompt(self) -> str:
        ...

    def _call_llm(
        self,
        user_message: str,
        response_format: dict | None = None,
        temperature: float | None = None,
    ) -> str:
        messages = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": user_message},
        ]
        kwargs = {"model": self.model, "messages": messages}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if response_format:
            kwargs["response_format"] = response_format

        logger.info("llm_call", agent=self.agent_name, model=self.model)
        response = self.client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        logger.info("llm_response", agent=self.agent_name, length=len(content))
        return content

    def _call_structured(
        self,
        user_message: str,
        json_schema: dict,
        temperature: float = 0.1,
    ) -> dict:
        raw = self._call_llm(
            user_message,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": f"{self.agent_name}_output",
                    "strict": True,
                    "schema": json_schema,
                },
            },
            temperature=temperature,
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
