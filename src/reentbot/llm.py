"""OpenRouter LLM client wrapper."""

from openai import AsyncOpenAI

DEFAULT_MODEL = "minimax/minimax-m2.7"

EXTRA_HEADERS = {
    "HTTP-Referer": "https://github.com/reentbot",
    "X-Title": "ReentBot",
}


def create_client(api_key: str) -> AsyncOpenAI:
    """Create an AsyncOpenAI client configured for OpenRouter."""
    return AsyncOpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers=EXTRA_HEADERS,
    )


def build_reasoning_body(reasoning_config: dict | None) -> dict | None:
    """Build extra_body dict for OpenRouter reasoning parameter.

    Returns None if reasoning is disabled (effort is 'off' or not set).
    """
    if not reasoning_config or reasoning_config.get("effort") in (None, "off"):
        return None
    return {"reasoning": {"effort": reasoning_config["effort"]}}
