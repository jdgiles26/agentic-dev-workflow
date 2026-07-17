"""
wizard.py — Self-healing diagnostic layer for the agentic dev pipeline.

Provides:
  • FailureEvent  — structured capture of pipeline stage failures.
  • Diagnosis     — root-cause analysis result with remediation steps.
  • analyse()     — rule-based decision tree that maps FailureEvents to Diagnoses.
  • diagnose_and_report() — prints a coloured (or plain) diagnostic card and
                            offers interactive auto-fix / retry menus.
  • pipeline_stage() — async context manager that wraps a stage and converts
                       uncaught exceptions into FailureEvents.
  • append_run_log() — writes a JSON entry to logs/pipeline_runs.jsonl.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from tools.config import LOG_DIR

# ── Colour helpers (NO_COLOR-aware) ──────────────────────────────────────────

_USE_COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR", "")


def _c(text: str, code: str) -> str:
    """Wrap *text* in ANSI escape *code* if colour is enabled."""
    if not _USE_COLOUR:
        return text
    return f"\033[{code}m{text}\033[0m"


def _bold(t: str) -> str:
    return _c(t, "1")


def _red(t: str) -> str:
    return _c(t, "31")


def _yellow(t: str) -> str:
    return _c(t, "33")


def _green(t: str) -> str:
    return _c(t, "32")


def _cyan(t: str) -> str:
    return _c(t, "36")


# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class FailureEvent:
    """Captures everything needed to diagnose a pipeline stage failure."""

    stage: str
    """Name of the pipeline stage that failed (e.g. 'spec_validation')."""

    exc_type: str
    """Exception class name (e.g. 'JSONDecodeError')."""

    exc_message: str
    """str(exception)."""

    traceback_short: str
    """Last few lines of the stack trace, truncated to ≤ 800 chars."""

    raw_llm_response: str = ""
    """Raw LLM output at the time of failure, if available (may be empty)."""

    attempt: int = 1
    """Pipeline retry attempt number at time of failure."""

    extra: dict[str, Any] = field(default_factory=dict)
    """Optional stage-specific metadata."""

    def to_dict(self) -> dict:
        """Convert to a JSON-serialisable dict."""
        return asdict(self)


@dataclass
class Diagnosis:
    """Root-cause analysis result returned by analyse()."""

    likely_cause: str
    """Human-readable one-liner describing the probable root cause."""

    confidence: float
    """0.0 – 1.0 confidence score for the diagnosis."""

    suggested_fixes: list[str]
    """Ordered list of concrete remediation steps."""

    auto_fixable: bool
    """True if wizard.apply_fix() can attempt an automated repair."""

    fix_tag: str = ""
    """Internal tag used by apply_fix() to dispatch the right repair."""


# ── Rule-based decision tree ─────────────────────────────────────────────────

#: Number of consecutive review failures before triggering the "review loop" rule.
_REVIEW_LOOP_THRESHOLD = 3


def analyse(event: FailureEvent) -> Diagnosis:
    """
    Inspect *event* and return a :class:`Diagnosis` via a rule-based decision tree.

    Rules are evaluated in priority order; the first matching rule wins.
    No LLM calls are made — this is entirely deterministic.
    """
    etype = event.exc_type
    emsg = event.exc_message.lower()
    stage = event.stage.lower()

    # Rule 1 — JSON parse failure (truncated LLM output)
    if etype in ("JSONDecodeError", "ValueError") and (
        "json" in emsg or "parse" in emsg or "decode" in emsg
    ):
        return Diagnosis(
            likely_cause="LLM response was truncated or malformed — JSON could not be decoded.",
            confidence=0.90,
            suggested_fixes=[
                "Use parse_llm_json() from tools/llm_json.py — it repairs truncated output.",
                "Increase max_tokens in the LLM client call (currently 4096).",
                "Simplify the prompt to reduce output length.",
                "Switch to a larger model (≥7B) that produces more reliable JSON.",
            ],
            auto_fixable=True,
            fix_tag="json_repair",
        )

    # Rule 2 — Missing key in spec/generated data
    if etype == "KeyError":
        return Diagnosis(
            likely_cause="LLM skipped one or more required keys in its JSON response.",
            confidence=0.85,
            suggested_fixes=[
                "Inject default values for missing keys using dict.setdefault().",
                "Add explicit key requirements to the system prompt.",
                "Check agent_instructions/ files to ensure the schema is clearly stated.",
                "Use a model with better instruction-following (≥7B recommended).",
            ],
            auto_fixable=True,
            fix_tag="inject_defaults",
        )

    # Rule 3 — Generated file not found
    if etype == "FileNotFoundError":
        if "generated" in emsg or "output" in emsg or "feature" in emsg:
            return Diagnosis(
                likely_cause="Code-generation stage did not write the expected output files.",
                confidence=0.88,
                suggested_fixes=[
                    "Re-run the code-generation stage.",
                    "Check that OUTPUT_DIR is writable (see preflight checks).",
                    "Inspect agent_runner.log for errors from developer_agent.py.",
                    "Verify the LLM produced a 'files' list with at least one entry.",
                ],
                auto_fixable=False,
                fix_tag="",
            )
        return Diagnosis(
            likely_cause="A required file is missing — likely a spec or config file.",
            confidence=0.80,
            suggested_fixes=[
                "Verify specs/<feature_name>/prd.md and tdd.yaml both exist.",
                "Check paths in .agentconfig.yaml under the 'paths:' section.",
                "Run: python tools/agent_runner.py <feature_name> from the repo root.",
                "Check SPECS_DIR / OUTPUT_DIR settings in .agentconfig.yaml.",
            ],
            auto_fixable=False,
            fix_tag="",
        )

    # Rule 4 — Ollama / llama.cpp unreachable
    if etype in ("ConnectionError", "ConnectionRefusedError", "RuntimeError") or (
        "timeout" in emsg or "timed out" in emsg or
        "connection" in emsg or "cannot reach" in emsg or
        "llama" in emsg or "ollama" in emsg
    ):
        return Diagnosis(
            likely_cause="The llama.cpp server is not running or is unreachable.",
            confidence=0.92,
            suggested_fixes=[
                "Start llama.cpp:  llama serve --fim-qwen-3b-default -ngl 99 --port 8012",
                "Check LLAMA_HOST in .env matches the server address (default: http://127.0.0.1:8012).",
                "Increase LLAMA_TIMEOUT in .env if the model loads slowly.",
                "Run `curl http://127.0.0.1:8012/v1/models` to verify the server is up.",
            ],
            auto_fixable=False,
            fix_tag="",
        )

    if etype in ("TimeoutError", "asyncio.TimeoutError"):
        return Diagnosis(
            likely_cause="LLM call timed out — server is overloaded or model is too large.",
            confidence=0.88,
            suggested_fixes=[
                "Increase LLAMA_TIMEOUT in .env (current value is read from config).",
                "Use a smaller quantised model (Q4_K_M instead of Q8_0).",
                "Reduce prompt length or max_tokens.",
                "Ensure no other processes are competing for GPU/CPU resources.",
            ],
            auto_fixable=False,
            fix_tag="",
        )

    # Rule 5 — Repeated review failures
    if "review" in stage and event.attempt >= _REVIEW_LOOP_THRESHOLD:
        return Diagnosis(
            likely_cause=(
                f"Model has failed the review check {event.attempt} times in a row — "
                "it may be unable to meet the quality threshold."
            ),
            confidence=0.75,
            suggested_fixes=[
                "Lower coverage_threshold in .agentconfig.yaml (currently 80).",
                "Switch to a larger or more capable model for the 'reviewer' role.",
                "Simplify the feature spec (prd.md) to reduce implementation complexity.",
                "Increase max_retries in .agentconfig.yaml to give the model more attempts.",
            ],
            auto_fixable=False,
            fix_tag="",
        )

    # Rule 6 — Guardrail violation
    if "guardrail" in stage or "security" in stage:
        return Diagnosis(
            likely_cause="Generated code contains forbidden patterns flagged by the guardrail scanner.",
            confidence=0.82,
            suggested_fixes=[
                "Inspect guardrails/forbidden_actions.yaml for the violated patterns.",
                "Add explicit 'do NOT use <pattern>' to agent_instructions/developer_agent.md.",
                "Set guardrails.enforce=false in .agentconfig.yaml to bypass (dev-only).",
                "Check guardrails/permission_boundaries.json for allowed operation boundaries.",
            ],
            auto_fixable=False,
            fix_tag="",
        )

    # Fallback — unknown error
    return Diagnosis(
        likely_cause=f"Unexpected {etype} in stage '{event.stage}'.",
        confidence=0.40,
        suggested_fixes=[
            "Review the full stack trace with option 4 (show full stack trace).",
            f"Search agent_runner.log for '{event.exc_type}' for additional context.",
            "Check that all dependencies are installed: pip install -r requirements.txt.",
        ],
        auto_fixable=False,
        fix_tag="",
    )


# ── Auto-fix implementations ──────────────────────────────────────────────────


def apply_fix(event: FailureEvent, diagnosis: Diagnosis) -> bool:
    """
    Attempt to automatically apply the fix indicated by *diagnosis.fix_tag*.

    Returns True if the fix was applied, False otherwise.
    """
    if diagnosis.fix_tag == "json_repair":
        # Re-parse the raw LLM response through the repair utility.
        from tools.llm_json import parse_llm_json  # noqa: PLC0415

        if not event.raw_llm_response:
            print(_yellow("  ⚠  No raw LLM response captured — cannot attempt JSON repair."))
            return False
        try:
            result = parse_llm_json(event.raw_llm_response, context="WizardAutoFix")
            print(_green(f"  ✓  JSON repaired successfully. Keys found: {list(result.keys())}"))
            return True
        except ValueError as exc:
            print(_red(f"  ✗  JSON repair failed: {exc}"))
            return False

    if diagnosis.fix_tag == "inject_defaults":
        print(_yellow(
            "  ℹ  Default injection requires stage context not available here.\n"
            "     Re-run with --wizard; the pipeline will inject safe defaults on the next attempt."
        ))
        return False

    return False


# ── Diagnostic card printer ───────────────────────────────────────────────────

_WIDTH = 56  # inner width of the diagnostic box


def _box_line(content: str = "") -> str:
    pad = _WIDTH - len(content)
    return f"║ {content}{' ' * max(pad, 0)}"


def _separator(label: str = "") -> str:
    if label:
        inner = f"══ {label} " + "═" * max(_WIDTH - len(label) - 4, 0)
    else:
        inner = "═" * (_WIDTH + 1)
    return f"╠{inner}╣"


def diagnose_and_report(event: FailureEvent) -> None:
    """
    Print a formatted diagnostic card for *event*, offer interactive menus,
    and optionally apply an auto-fix.

    Respects NO_COLOR environment variable.
    """
    diagnosis = analyse(event)

    confidence_pct = int(diagnosis.confidence * 100)
    autofix_label = _green("Yes") if diagnosis.auto_fixable else _red("No")

    top = "╔══ " + _bold(_cyan("🧙 WIZARD DIAGNOSTIC")) + " " + "═" * max(_WIDTH - 22, 0) + "╗"
    bot = "╚" + "═" * (_WIDTH + 1) + "╝"

    lines = [
        top,
        _box_line(f"Stage      : {_bold(event.stage)}"),
        _box_line(f"Error      : {_red(event.exc_type)} — {event.exc_message[:45]}"),
        _box_line(f"Likely cause: {diagnosis.likely_cause[:_WIDTH - 14]}"),
        _box_line(f"             (confidence: {confidence_pct}%)"),
        _box_line(f"Auto-fix   : {autofix_label}"),
        _separator("Suggested fixes"),
    ]
    for i, fix in enumerate(diagnosis.suggested_fixes, 1):
        # Wrap long fix lines
        if len(fix) > _WIDTH - 5:
            lines.append(_box_line(f"  {i}. {fix[:_WIDTH - 7]}…"))
        else:
            lines.append(_box_line(f"  {i}. {fix}"))
    lines.append(bot)

    print("\n" + "\n".join(lines) + "\n")

    # Auto-fix prompt
    if diagnosis.auto_fixable:
        try:
            answer = input(_bold("Apply fix automatically? [y/N] ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer == "y":
            apply_fix(event, diagnosis)

    # Action menu
    _show_action_menu(event)


def _show_action_menu(event: FailureEvent) -> str:
    """
    Present a numbered menu of recovery actions and return the chosen action key.

    Returns one of: 'retry', 'skip', 'abort', 'trace'.
    """
    menu = [
        _bold("\nWhat would you like to do?"),
        "  1. Retry this stage",
        "  2. Skip this stage and continue",
        "  3. Abort the pipeline",
        "  4. Show full stack trace",
        "",
    ]
    print("\n".join(menu))

    try:
        choice = input("Choice [1-4]: ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = "3"

    if choice == "1":
        return "retry"
    if choice == "2":
        return "skip"
    if choice == "4":
        print(_bold("\n── Full stack trace ──────────────────────────────────────"))
        print(event.traceback_short)
        print("──────────────────────────────────────────────────────────\n")
        return "trace"
    # Default / "3" → abort
    return "abort"


# ── Stage wrapper ─────────────────────────────────────────────────────────────


@asynccontextmanager
async def pipeline_stage(
    stage_name: str,
    raw_llm_response_ref: list[str] | None = None,
    attempt: int = 1,
    wizard_mode: bool = False,
) -> AsyncIterator[list[FailureEvent]]:
    """
    Async context manager that wraps a pipeline stage.

    Usage::

        failures: list[FailureEvent] = []
        async with pipeline_stage("code_generation", attempt=attempt, wizard_mode=args.wizard) as failures:
            data = await generate_feature(feature_name)

    On exception:
      - Constructs a FailureEvent and appends it to *failures*.
      - If *wizard_mode* is True, prints the diagnostic card.
      - Re-raises the exception so callers can decide to continue/abort.

    *raw_llm_response_ref* is an optional single-element list; set
    ``raw_llm_response_ref[0]`` inside the ``with`` block to capture the
    raw LLM string before an exception is raised.
    """
    failures: list[FailureEvent] = []
    try:
        yield failures
    except Exception as exc:
        tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        tb_short = "".join(tb_lines[-6:])[:800]
        raw_resp = (raw_llm_response_ref[0] if raw_llm_response_ref else "")

        event = FailureEvent(
            stage=stage_name,
            exc_type=type(exc).__name__,
            exc_message=str(exc)[:300],
            traceback_short=tb_short,
            raw_llm_response=raw_resp,
            attempt=attempt,
        )
        failures.append(event)

        if wizard_mode:
            diagnose_and_report(event)

        raise


# ── Persistent run log ────────────────────────────────────────────────────────


def append_run_log(
    feature_name: str,
    stages_completed: list[str],
    failures: list[FailureEvent],
    final_status: str,
) -> None:
    """
    Append one JSON entry to ``logs/pipeline_runs.jsonl``.

    Each entry captures timestamp, feature name, completed stages, failures,
    and the final pipeline status.  The log file is created if it does not
    exist; the ``logs/`` directory is created as needed.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "pipeline_runs.jsonl"

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "feature_name": feature_name,
        "stages_completed": stages_completed,
        "failures": [f.to_dict() for f in failures],
        "final_status": final_status,
    }

    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
