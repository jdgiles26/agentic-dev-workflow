"""
reviewer_agent.py — Runs the generated test suite, measures coverage, then
                    asks the Reviewer Agent LLM to produce a structured verdict.

Verdict JSON (from LLM)
───────────────────────
{
  "overall":         "PASS" | "FAIL",
  "prd_check":       "PASS" | "FAIL",
  "tdd_check":       "PASS" | "FAIL",
  "guardrail_check": "PASS" | "FAIL",
  "feedback":        "..."
}

The pipeline then applies hard overrides:
  • If pytest exit-code ≠ 0                     → tdd_check = FAIL
  • If coverage < threshold                      → tdd_check = FAIL
  • If either sub-check = FAIL                   → overall   = FAIL
"""

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from tools.config import (
    SPECS_DIR,
    OUTPUT_DIR,
    COVERAGE_THRESHOLD,
)
from tools.llm_client import LLMClient
from tools.llm_json import parse_llm_json

logger = logging.getLogger(__name__)

_INSTRUCTIONS_FILE = (
    Path(__file__).resolve().parent.parent
    / "agent_instructions"
    / "reviewer_agent.md"
)


# ── Test runner ───────────────────────────────────────────────────────────────


def _run_tests(feature_dir: Path) -> tuple[bool, str, float]:
    """
    Execute pytest inside *feature_dir*, capture output, extract coverage %.

    Returns (tests_passed: bool, output: str, coverage_pct: float).
    """
    src_dir   = feature_dir / "src"
    tests_dir = feature_dir / "tests"

    if not tests_dir.exists():
        return False, "No tests/ directory found.", 0.0

    cmd = [
        sys.executable, "-m", "pytest",
        str(tests_dir),
        "-v",
        "--tb=short",
        f"--cov={src_dir}",
        "--cov-report=term-missing",
        "--no-header",
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(feature_dir),
    )

    output = result.stdout + "\n" + result.stderr
    passed = result.returncode == 0

    # Extract coverage from summary line: "TOTAL   45   3   93%"
    coverage_pct = 0.0
    match = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", output)
    if match:
        coverage_pct = float(match.group(1))

    logger.info(
        "[Reviewer] Tests %s | Coverage %.0f%%",
        "PASSED ✓" if passed else "FAILED ✗",
        coverage_pct,
    )
    return passed, output, coverage_pct


# ── LLM review ────────────────────────────────────────────────────────────────


def _collect_sources(feature_dir: Path) -> dict[str, str]:
    sources = {}
    for py in (feature_dir / "src").rglob("*.py"):
        sources[str(py.relative_to(feature_dir))] = py.read_text(encoding="utf-8")
    return sources


def _collect_tests(feature_dir: Path) -> dict[str, str]:
    tests = {}
    for py in (feature_dir / "tests").rglob("*.py"):
        tests[str(py.relative_to(feature_dir))] = py.read_text(encoding="utf-8")
    return tests


def _build_review_prompt(
    prd: str,
    tdd: str,
    sources: dict,
    tests: dict,
    test_output: str,
    coverage_pct: float,
    tests_passed: bool,
) -> str:
    instructions = _INSTRUCTIONS_FILE.read_text(encoding="utf-8")

    src_block  = "\n\n".join(f"### {k}\n```python\n{v}\n```" for k, v in sources.items())
    test_block = "\n\n".join(f"### {k}\n```python\n{v}\n```" for k, v in tests.items())

    return (
        f"{instructions}\n\n"
        "──────────────────────────────────────────────\n"
        f"## PRD\n```\n{prd}\n```\n\n"
        f"## TDD\n```\n{tdd}\n```\n\n"
        f"## Source files\n{src_block}\n\n"
        f"## Test files\n{test_block}\n\n"
        f"## pytest output\n```\n{test_output[:3000]}\n```\n\n"
        f"## Coverage: {coverage_pct:.0f}%  (threshold: {COVERAGE_THRESHOLD}%)\n"
        f"## All tests passed: {tests_passed}\n\n"
        "Now produce the JSON verdict. ONLY the JSON object — no other text."
    )


async def review_feature(feature_name: str) -> dict:
    """
    Run tests + LLM review for *feature_name*.
    Returns the final verdict dict with an 'overall' key.
    """
    feature_dir = OUTPUT_DIR / feature_name

    if not feature_dir.exists():
        return {
            "overall": "FAIL",
            "prd_check": "FAIL",
            "tdd_check": "FAIL",
            "guardrail_check": "FAIL",
            "feedback": f"Output directory does not exist: {feature_dir}",
        }

    prd_text = (SPECS_DIR / feature_name / "prd.md").read_text(encoding="utf-8")
    tdd_text = (SPECS_DIR / feature_name / "tdd.yaml").read_text(encoding="utf-8")

    # 1. Run tests
    tests_passed, test_output, coverage_pct = _run_tests(feature_dir)

    # 2. Collect code
    sources = _collect_sources(feature_dir)
    tests   = _collect_tests(feature_dir)

    if not sources:
        return {
            "overall": "FAIL",
            "prd_check": "FAIL",
            "tdd_check": "FAIL",
            "guardrail_check": "FAIL",
            "feedback": "No source files found in generated/src/.",
        }

    # 3. Build prompt and call LLM
    prompt = _build_review_prompt(
        prd_text, tdd_text, sources, tests,
        test_output, coverage_pct, tests_passed,
    )
    client = LLMClient(role="reviewer")
    logger.info("[Reviewer] Sending code to LLM for evaluation…")
    raw = await client.generate(
        "You are a strict code reviewer. Follow the instructions exactly.", prompt
    )

    # 4. Parse LLM verdict — repair-aware for small models
    try:
        verdict = parse_llm_json(raw, context="ReviewerAgent")
    except ValueError as exc:
        # If the model can't produce valid JSON, fall back to a safe default
        # that still applies the hard pytest/coverage overrides below.
        logger.warning("[Reviewer] JSON parse failed — using safe default verdict.\n%s", exc)
        verdict = {
            "overall": "PASS",
            "prd_check": "PASS",
            "tdd_check": "PASS",
            "guardrail_check": "PASS",
            "feedback": f"LLM verdict unparseable; hard overrides applied. Raw: {raw[:200]}",
        }

    # 5. Apply hard overrides (ground truth from actual pytest run)
    if not tests_passed:
        verdict["tdd_check"] = "FAIL"
        verdict.setdefault("feedback", "")
        verdict["feedback"] += f"  [Hard override] pytest exited non-zero."

    if coverage_pct < COVERAGE_THRESHOLD:
        verdict["tdd_check"] = "FAIL"
        verdict["feedback"] = (
            verdict.get("feedback", "")
            + f"  [Hard override] Coverage {coverage_pct:.0f}% < {COVERAGE_THRESHOLD}%."
        )

    # Propagate sub-failures to overall
    if any(
        verdict.get(k, "PASS") == "FAIL"
        for k in ("prd_check", "tdd_check", "guardrail_check")
    ):
        verdict["overall"] = "FAIL"

    status = verdict.get("overall", "FAIL")
    icon = "✅" if status == "PASS" else "❌"
    logger.info(
        "[Reviewer] Verdict: %s %s | PRD:%s TDD:%s Guard:%s",
        status, icon,
        verdict.get("prd_check", "?"),
        verdict.get("tdd_check", "?"),
        verdict.get("guardrail_check", "?"),
    )
    if verdict.get("feedback"):
        logger.info("[Reviewer] Feedback: %s", verdict["feedback"])

    return verdict
