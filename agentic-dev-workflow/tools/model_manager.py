"""
model_manager.py — Discovers loaded models from a llama.cpp /v1/models endpoint,
                   scores them per agent role using keyword hints, and returns
                   the best available model name for each role.

The llama.cpp server only ever has the models it was started with loaded, so
"auto" selection is mostly a tie-breaker when multiple models are served.
In single-model setups (the common case) every role gets the same model name.
"""

import logging
import re
import urllib.request
import json
from typing import Optional

from tools.config import LLAMA_HOST, LLAMA_MODE, LLAMA_MODELS, LLAMA_HINTS

logger = logging.getLogger(__name__)


class ModelManager:
    """Queries /v1/models once, then scores and caches role → model mappings."""

    def __init__(self) -> None:
        self._available: list[dict] = []    # populated by _refresh()
        self._resolved: dict[str, str] = {} # role → model name cache

    # ── Public API ────────────────────────────────────────────────────────────

    def get_model(self, role: str) -> str:
        """Return the best available model name for *role*. Result is cached."""
        if role in self._resolved:
            return self._resolved[role]

        self._refresh()
        model = self._pick(role)
        self._resolved[role] = model
        logger.info("[ModelManager] %-10s → %s", role, model)
        return model

    # ── Internal ──────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        """Fetch model list from /v1/models (one-shot, cached for the session)."""
        if self._available:
            return

        url = f"{LLAMA_HOST.rstrip('/')}/v1/models"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            raise RuntimeError(
                f"Cannot reach llama.cpp server at {LLAMA_HOST}.\n"
                f"Start it first:  llama serve --fim-qwen-3b-default -ngl 99 --port 8012\n"
                f"Error: {exc}"
            ) from exc

        # /v1/models returns {data: [{id: "...", ...}, ...]} (OpenAI format)
        # llama.cpp also includes a top-level "models" list — handle both
        raw = data.get("data") or data.get("models") or []
        self._available = [
            {"name": m.get("id") or m.get("name", ""), "meta": m}
            for m in raw
            if m.get("id") or m.get("name")
        ]

        if not self._available:
            raise RuntimeError(
                "llama.cpp server returned no models. "
                "Check that the server started successfully."
            )

        logger.info(
            "[ModelManager] %d model(s) available: %s",
            len(self._available),
            [m["name"] for m in self._available],
        )

    def _pick(self, role: str) -> str:
        """Score every available model and return the best for *role*."""

        # Manual mode: use configured model if it's in the available list
        if LLAMA_MODE == "manual":
            manual = LLAMA_MODELS.get(role, "")
            if manual:
                names = [m["name"] for m in self._available]
                if manual in names:
                    return manual
                logger.warning(
                    "[ModelManager] Configured model '%s' not found. "
                    "Falling back to auto-selection.",
                    manual,
                )

        hints: list[str] = LLAMA_HINTS.get(role, [])
        scored: list[tuple[float, str]] = []

        for m in self._available:
            name_lc = m["name"].lower()
            score: float = 0.0

            # Hint scoring: earlier hints carry more weight
            for idx, hint in enumerate(hints):
                if hint.lower() in name_lc:
                    score += 10.0 - (idx * 0.5)

            # Tie-break by parameter count embedded in name (e.g. "7b", "13b")
            size_match = re.search(r"(\d+)b", name_lc)
            if size_match:
                score += min(float(size_match.group(1)) * 0.1, 3.0)

            scored.append((score, m["name"]))

        scored.sort(key=lambda t: t[0], reverse=True)
        return scored[0][1]


# Module-level singleton
_manager = ModelManager()


def get_model(role: str) -> str:
    """Convenience wrapper used by LLMClient."""
    return _manager.get_model(role)
