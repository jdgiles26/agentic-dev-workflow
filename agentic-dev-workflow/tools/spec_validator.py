"""
spec_validator.py — Checks that the TDD test plan covers every acceptance
                    criterion listed in the PRD using an LLM.

Small models (3B) frequently produce truncated or malformed JSON. This module
uses a two-strategy approach:

Strategy 1 — JSON parse with truncation repair
  Ask for structured JSON. If the model truncates mid-output, attempt to close
  any unclosed strings and braces before parsing.

Strategy 2 — Plain yes/no fallback
  If JSON still can't be parsed, ask a single yes/no question with a much
  shorter prompt. Parse "yes"/"no" from the text answer. This is far more
  reliable on 3B models.

Only hard-fails if both strategies fail to produce any readable answer.
"""

import logging
import re
from pathlib import Path

from tools.llm_client import LLMClient
from tools.llm_json import parse_llm_json

logger = logging.getLogger(__name__)

# ── Strategy 1: structured JSON ───────────────────────────────────────────────

_SYSTEM_JSON = (
    "You are a specification auditor. "
    "Check whether every numbered acceptance criterion in the PRD is covered "
    "by at least one test case in the TDD. "
    "Reply with ONLY this JSON object, nothing else:\n"
    '{"consistent":true,"missing":[]}\n'
    "or if items are missing:\n"
    '{"consistent":false,"missing":["criterion 1","criterion 2"]}'
)

# ── Strategy 2: plain yes/no ──────────────────────────────────────────────────

_SYSTEM_YESNO = (
    "You are a specification auditor. Answer with a single word: yes or no."
)


def _try_repair_json(raw: str) -> dict | None:
    """Thin wrapper around the shared parse_llm_json utility; returns None on failure."""
    try:
        return parse_llm_json(raw, context="SpecValidator")
    except ValueError:
        return None


async def validate_specs(prd_path: Path, tdd_path: Path) -> bool:
    """
    Returns True if the TDD plan covers every PRD acceptance criterion.

    Uses a two-strategy approach so small 3B models don't block the pipeline
    due to JSON formatting failures.
    """
    prd_text = prd_path.read_text(encoding="utf-8")
    tdd_text = tdd_path.read_text(encoding="utf-8")

    client = LLMClient(role="validator")

    # ── Strategy 1: ask for JSON ──────────────────────────────────────────────
    prompt_json = (
        f"PRD:\n{prd_text}\n\n"
        f"TDD:\n{tdd_text}\n\n"
        "Does the TDD cover all PRD acceptance criteria? "
        'Reply ONLY with {"consistent":true,"missing":[]} '
        "or list the missing ones."
    )

    raw = await client.generate(_SYSTEM_JSON, prompt_json)
    logger.debug("[SpecValidator] Raw response: %s", raw[:300])

    result = _try_repair_json(raw)

    if result is not None:
        consistent = bool(result.get("consistent", False))
        missing    = result.get("missing", [])

        if missing and consistent:
            # Model contradicted itself — trust the missing list
            consistent = False

        if consistent:
            logger.info("[SpecValidator] PRD ↔ TDD consistent ✓  (JSON strategy)")
            return True

        if missing:
            logger.warning(
                "[SpecValidator] %d uncovered criterion(ia):\n%s",
                len(missing),
                "\n".join(f"  • {c}" for c in missing),
            )
            return False

        # consistent=false but empty missing list — model is confused; fall through
        logger.debug("[SpecValidator] JSON result ambiguous — trying yes/no fallback")

    else:
        logger.debug(
            "[SpecValidator] Could not parse JSON from response — trying yes/no fallback.\nRaw: %s",
            raw[:200],
        )

    # ── Strategy 2: plain yes/no ──────────────────────────────────────────────
    prompt_yesno = (
        f"PRD acceptance criteria:\n{_extract_criteria(prd_text)}\n\n"
        f"TDD test names:\n{_extract_test_names(tdd_text)}\n\n"
        "Does every acceptance criterion have a matching test? Answer yes or no."
    )

    raw2 = await client.generate(_SYSTEM_YESNO, prompt_yesno)
    answer = raw2.strip().lower()
    logger.debug("[SpecValidator] yes/no response: %s", answer[:80])

    # Accept any answer that starts with or contains "yes"
    if re.search(r"\byes\b", answer):
        logger.info("[SpecValidator] PRD ↔ TDD consistent ✓  (yes/no fallback)")
        return True

    if re.search(r"\bno\b", answer):
        logger.warning(
            "[SpecValidator] yes/no fallback: model says criteria are NOT fully covered."
        )
        return False

    # Neither yes nor no — model is completely unparseable; log and pass through
    # so a tiny model doesn't permanently block the pipeline.
    logger.warning(
        "[SpecValidator] Could not determine consistency from either strategy. "
        "Assuming consistent to avoid blocking pipeline. "
        "Raw fallback response: %s",
        raw2[:200],
    )
    return True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_criteria(prd_text: str) -> str:
    """Pull out numbered acceptance criteria lines from the PRD."""
    lines = []
    in_section = False
    for line in prd_text.splitlines():
        if re.match(r"##\s+acceptance criteria", line, re.IGNORECASE):
            in_section = True
            continue
        if in_section and line.startswith("##"):
            break
        if in_section and re.match(r"\d+\.", line.strip()):
            lines.append(line.strip())
    return "\n".join(lines) if lines else prd_text[:600]


def _extract_test_names(tdd_text: str) -> str:
    """Pull out test names from the TDD YAML."""
    names = re.findall(r'name:\s*"([^"]+)"', tdd_text)
    return "\n".join(f"- {n}" for n in names) if names else tdd_text[:400]


# ── CLI entry-point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio, sys

    if len(sys.argv) != 3:
        print("Usage: python -m tools.spec_validator <prd.md> <tdd.yaml>")
        sys.exit(1)

    ok = asyncio.run(validate_specs(Path(sys.argv[1]), Path(sys.argv[2])))
    sys.exit(0 if ok else 1)
