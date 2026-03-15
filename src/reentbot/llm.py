"""OpenRouter LLM client wrapper."""

from openai import AsyncOpenAI

DEFAULT_MODEL = "minimax/minimax-m2.5"

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
