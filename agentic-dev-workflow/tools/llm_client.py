"""
llm_client.py — Async wrapper around the llama.cpp OpenAI-compatible /v1/ API.

Uses the official `openai` SDK pointed at the local llama.cpp server.
No API key required — llama.cpp ignores the key field but the SDK requires
a non-empty string, so we pass a dummy value.

Each LLMClient is bound to a *role* (developer / reviewer / security /
validator). The model is resolved lazily via ModelManager on the first call.
"""

import asyncio
import logging
from typing import Optional

from openai import AsyncOpenAI

from tools.config import LLAMA_HOST, LLAMA_TIMEOUT
from tools.model_manager import get_model

logger = logging.getLogger(__name__)

# llama.cpp doesn't validate API keys but the SDK requires a non-empty string
_DUMMY_KEY = "local"


class LLMClient:
    """Async llama.cpp client bound to a specific agent role."""

    def __init__(self, role: str = "developer") -> None:
        self.role = role
        self._model: Optional[str] = None
        self._client: Optional[AsyncOpenAI] = None

    # ── Public ────────────────────────────────────────────────────────────────

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Send a chat completion request and return the assistant reply."""
        model  = await self._resolve_model()
        client = self._get_client()

        logger.debug(
            "[LLMClient:%s] → %s  (%d chars prompt)",
            self.role, model, len(system_prompt) + len(user_prompt),
        )

        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    temperature=0.15,
                    max_tokens=4096,
                ),
                timeout=LLAMA_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"llama.cpp server timed out after {LLAMA_TIMEOUT}s "
                f"(model: {model}). Increase LLAMA_TIMEOUT in .env or "
                "try a smaller context."
            )

        content = response.choices[0].message.content or ""
        logger.debug("[LLMClient:%s] ← %d chars", self.role, len(content))
        return content

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _resolve_model(self) -> str:
        if self._model is None:
            loop = asyncio.get_event_loop()
            self._model = await loop.run_in_executor(None, get_model, self.role)
        return self._model

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=_DUMMY_KEY,
                base_url=f"{LLAMA_HOST.rstrip('/')}/v1",
            )
        return self._client
