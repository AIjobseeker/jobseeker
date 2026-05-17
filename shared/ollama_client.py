"""Shared async Ollama client for all worker activities."""
from __future__ import annotations

from ollama import AsyncClient

from shared.config import settings

_client: AsyncClient | None = None


def get_client() -> AsyncClient:
    global _client
    if _client is None:
        _client = AsyncClient(host=settings.ollama_host)
    return _client


async def chat(model: str, prompt: str, max_tokens: int = 1000, think: bool = False) -> str:
    """Send a single-turn chat message and return the response text."""
    response = await get_client().chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        think=think,
        options={"num_predict": max_tokens},
    )
    return response.message.content.strip()
