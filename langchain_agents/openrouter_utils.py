# VENDORED from github.com/Exios66/llm-entity-extraction (verified against commit 3a03d5c, 2026-08-10).
# Imported verbatim (import paths rewritten to ``langchain_agents.*``) so the
# eval-validated LangChain sorter/contracts-specialist agents run inside the
# mailroom. Local adaptations (pages/vision, usage/deadline hooks) are marked
# ``MAILROOM PATCH``. Keep diffs against upstream small and documented.


"""Shared OpenRouter constants and request helpers.

The API base is overridable via the ``OPENROUTER_BASE_URL`` environment
variable so any OpenAI-compatible vision endpoint can be plugged in without
code changes: OpenRouter (default), a local Ollama server, or a self-hosted vLLM server.
"""

import os

OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
).rstrip("/")
OPENROUTER_API_URL = os.environ.get(
    "OPENROUTER_API_URL", f"{OPENROUTER_BASE_URL}/chat/completions"
)

OUTPUT_FORMAT_MARKER = "## Output format"


def split_prompt(prompt: str) -> tuple[str, str]:
    """Split a classification prompt into (system_text, user_text).

    system_text is the instruction context up to (and excluding) the first
    ``## Output format`` header; user_text is the remainder (the output
    format contract, plus any trailing calibration/work-example text).
    """
    if not prompt:
        return "", ""
    idx = prompt.find(OUTPUT_FORMAT_MARKER)
    if idx == -1:
        return prompt, ""
    system_text = prompt[:idx]
    user_text = prompt[idx:]
    return system_text, user_text


def build_vision_messages(
    prompt: str,
    image_base64: str,
    image_format: str = "png",
    split_intro: bool = False,
) -> list[dict]:
    """Build an OpenAI-style ``messages`` payload with a text prompt and image."""
    if split_intro:
        system_text, user_text = split_prompt(prompt)
        messages = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        user_content: list[dict] = []
        if user_text:
            user_content.append({"type": "text", "text": user_text})
        user_content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/{image_format};base64,{image_base64}"
            },
        })
        messages.append({"role": "user", "content": user_content})
        return messages

    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/{image_format};base64,{image_base64}"
                    },
                },
            ],
        }
    ]


def build_text_messages(system_prompt: str, user_text: str) -> list[dict]:
    """Build a standard text-only messages payload for classification/extraction tasks."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]
