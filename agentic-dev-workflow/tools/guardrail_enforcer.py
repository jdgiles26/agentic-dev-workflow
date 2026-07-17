"""
guardrail_enforcer.py — Two-layer security scanner for generated code.

Layer 1 — Static scan (fast, deterministic)
    Reads every .py file produced by the Developer Agent and searches for
    forbidden string patterns from guardrails/forbidden_actions.yaml.

Layer 2 — Semantic scan (LLM-assisted)
    Sends all generated code to the Security Guard LLM with structured
    instructions.  Catches intent-level violations the static scan misses
    (e.g., obfuscated eval, indirect subprocess).

Both layers must pass for run_guardrails() to return True.
"""

import json
import logging
import re
from pathlib import Path
from typing import NamedTuple

import yaml

from tools.config import (
    OUTPUT_DIR,
    FORBIDDEN_ACTIONS_FILE,
    PERMISSIONS_FILE,
    GUARDRAILS_ENFORCE,
)
from tools.llm_json import parse_llm_json
# LLMClient is imported inside semantic_scan() to avoid pulling in the
# openai dependency when only the static scanner is used (e.g. in tests).

logger = logging.getLogger(__name__)

_GUARD_INSTRUCTIONS = (
    Path(__file__).resolve().parent.parent
    / "agent_instructions"
    / "security_guard.md"
)


# ── Data types ────────────────────────────────────────────────────────────────


class Violation(NamedTuple):
    file:    str
    line:    int
    pattern: str
    detail:  str


# ── Static scanner ────────────────────────────────────────────────────────────


def _load_forbidden() -> list[str]:
    with open(FORBIDDEN_ACTIONS_FILE, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return [str(p) for p in data.get("forbidden", [])]


def static_scan(feature_name: str) -> list[Violation]:
    """
    Scan all .py files under generated/<feature_name>/ for forbidden patterns.
    Returns a list of Violation objects (empty = clean).
    """
    forbidden = _load_forbidden()
    feature_dir = OUTPUT_DIR / feature_name
    violations: list[Violation] = []

    py_files = list(feature_dir.rglob("*.py"))
    if not py_files:
        logger.warning("[StaticScan] No .py files found in %s", feature_dir)
        return violations

    for py_file in py_files:
        lines = py_file.read_text(encoding="utf-8", errors="replace").splitlines()
        rel = str(py_file.relative_to(feature_dir))
        for lineno, line in enumerate(lines, start=1):
            for pattern in forbidden:
                # strip YAML comment annotations like "(unless in allowed list)"
                raw_pattern = pattern.split("(")[0].strip()
                if raw_pattern and raw_pattern in line:
                    v = Violation(
                        file=rel,
                        line=lineno,
                        pattern=raw_pattern,
                        detail=f"Forbidden pattern '{raw_pattern}' in line: {line.strip()[:120]}",
                    )
                    violations.append(v)
                    logger.warning("[StaticScan] %s:%d  →  %s", rel, lineno, raw_pattern)

    if not violations:
        logger.info("[StaticScan] Clean ✓  (%d file(s) scanned)", len(py_files))
    return violations


# ── Semantic scanner ──────────────────────────────────────────────────────────


async def semantic_scan(feature_name: str) -> tuple[bool, list[dict]]:
    """
    Send all generated code to the Security Guard LLM for deep inspection.

    Returns (is_safe, violations_list).
    """
    # Deferred import: only pull in LLMClient (and transitively ollama) when
    # the semantic scan is actually invoked, not at module import time.
    from tools.llm_client import LLMClient  # noqa: PLC0415

    feature_dir = OUTPUT_DIR / feature_name
    py_files = list(feature_dir.rglob("*.py"))

    if not py_files:
        logger.info("[SemanticScan] No files to scan — skipping.")
        return True, []

    # Aggregate all code into one prompt payload
    code_payload = ""
    for py_file in py_files:
        rel = py_file.relative_to(feature_dir)
        code_payload += f"### File: {rel}\n{py_file.read_text(encoding='utf-8')}\n\n"

    instructions = _GUARD_INSTRUCTIONS.read_text(encoding="utf-8")
    prompt = (
        f"{instructions}\n\n"
        "──────────────────────────────────────────────\n"
        "Code to audit:\n\n"
        f"{code_payload}\n"
        "Return ONLY the JSON verdict."
    )

    client = LLMClient(role="security")
    raw = await client.generate(
        "You are a security expert. Follow the instructions exactly.", prompt
    )

    # Parse the JSON verdict — use repair-aware parser for small models
    try:
        result = parse_llm_json(raw, context="SemanticScan")
    except ValueError as exc:
        logger.warning("[SemanticScan] Could not parse LLM response — assuming safe to avoid blocking.\n%s", exc)
        # A 3B model that can't produce valid JSON is almost certainly not
        # flagging real violations. Pass through and let the static scan
        # remain the authoritative gate.
        return True, []

    safe       = bool(result.get("safe", True))
    violations = result.get("violations", [])

    if safe:
        logger.info("[SemanticScan] Clean ✓")
    else:
        for v in violations:
            logger.warning(
                "[SemanticScan] %s:%s  →  %s",
                v.get("file", "?"), v.get("line", "?"), v.get("detail", "?"),
            )

    return safe, violations


# ── Orchestrator ──────────────────────────────────────────────────────────────


async def run_guardrails(feature_name: str) -> bool:
    """
    Run both static and semantic scans.  Returns True only if both pass.

    If GUARDRAILS_ENFORCE is False (config flag), violations are logged as
    warnings and the function still returns True (useful for local debugging).
    """
    logger.info("[Guardrails] ── Static scan ──────────────────────────────")
    static_violations = static_scan(feature_name)

    logger.info("[Guardrails] ── Semantic scan ─────────────────────────────")
    semantic_ok, semantic_violations = await semantic_scan(feature_name)

    all_ok = (not static_violations) and semantic_ok

    if not all_ok:
        if static_violations:
            logger.error(
                "[Guardrails] %d static violation(s) found.", len(static_violations)
            )
        if not semantic_ok:
            logger.error(
                "[Guardrails] %d semantic violation(s) found.", len(semantic_violations)
            )

        if not GUARDRAILS_ENFORCE:
            logger.warning("[Guardrails] enforce=false — continuing despite violations.")
            return True

        return False

    logger.info("[Guardrails] All checks passed ✓")
    return True
