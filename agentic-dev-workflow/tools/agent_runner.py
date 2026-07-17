"""
agent_runner.py — Master orchestrator for the agentic development pipeline.

Workflow per attempt
────────────────────
  1. Spec validation   — PRD ↔ TDD consistency check (LLM)
  2. Code generation   — Developer Agent writes src/ + tests/
  3. Guardrail scan    — Static + semantic security check
  4. Review + test     — Run pytest, check coverage, LLM verdict
  5. Approve or retry  — Up to max_retries with feedback injected

Usage
─────
  python tools/agent_runner.py <feature_name>
  python tools/agent_runner.py example_feature
  python tools/agent_runner.py example_feature --wizard
"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from tools.config import (
    SPECS_DIR, OUTPUT_DIR, LOG_DIR,
    MAX_RETRIES, RETRY_DELAY, LOG_LEVEL,
)
from tools.spec_validator   import validate_specs
from tools.developer_agent  import generate_feature, write_generated_files
from tools.guardrail_enforcer import run_guardrails
from tools.reviewer_agent   import review_feature
from tools.wizard import (
    FailureEvent,
    append_run_log,
    diagnose_and_report,
    pipeline_stage,
)
from tools.preflight import run_preflight_checks

# ── Logging setup ─────────────────────────────────────────────────────────────

LOG_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "agent_runner.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("agent_runner")
console = Console()


# ── Pipeline ──────────────────────────────────────────────────────────────────


async def run_pipeline(feature_name: str, wizard_mode: bool = False) -> bool:
    """
    Execute the full agentic pipeline for *feature_name*.

    Parameters
    ----------
    feature_name:
        Name of the feature to generate (maps to specs/<feature_name>/).
    wizard_mode:
        When True, pipeline failures produce a diagnostic card instead of
        crashing; the wizard also writes a persistent run log entry.

    Returns True if the feature is approved, False otherwise.
    """
    prd_path = SPECS_DIR / feature_name / "prd.md"
    tdd_path = SPECS_DIR / feature_name / "tdd.yaml"

    all_failures: list[FailureEvent] = []
    stages_completed: list[str] = []
    final_status = "REJECTED"

    # Pre-flight check — file existence (fast, no LLM)
    for p in (prd_path, tdd_path):
        if not p.exists():
            console.print(
                Panel(
                    f"[red]Spec file not found:[/red] {p}\n"
                    f"Create specs/{feature_name}/prd.md and tdd.yaml first.",
                    title="❌ Pre-flight failed",
                    border_style="red",
                )
            )
            append_run_log(feature_name, stages_completed, all_failures, "ABORTED")
            return False

    _banner(feature_name)

    # ── Step 0: Spec validation ────────────────────────────────────────────
    _section("Step 0 — Spec validation")
    try:
        async with pipeline_stage(
            "spec_validation", attempt=1, wizard_mode=wizard_mode
        ) as stage_failures:
            spec_ok = await validate_specs(prd_path, tdd_path)
        all_failures.extend(stage_failures)
    except Exception:
        all_failures.extend(stage_failures)  # type: ignore[possibly-undefined]
        append_run_log(feature_name, stages_completed, all_failures, "ABORTED")
        return False

    if not spec_ok:
        console.print(
            "[red]❌ PRD and TDD are inconsistent. "
            "Fix tdd.yaml to cover all acceptance criteria, then retry.[/red]"
        )
        append_run_log(feature_name, stages_completed, all_failures, "ABORTED")
        return False

    console.print("[green]✓ Specs are consistent[/green]")
    stages_completed.append("spec_validation")

    last_feedback: str = ""

    for attempt in range(1, MAX_RETRIES + 1):
        _section(f"Attempt {attempt} / {MAX_RETRIES}")
        t_start = time.monotonic()

        # ── Step 1: Code generation ────────────────────────────────────────
        _step("Developer Agent — generating code")
        raw_ref: list[str] = [""]
        try:
            async with pipeline_stage(
                "code_generation",
                raw_llm_response_ref=raw_ref,
                attempt=attempt,
                wizard_mode=wizard_mode,
            ) as stage_failures:
                if last_feedback:
                    console.print(
                        f"[dim]Injecting reviewer feedback into prompt:[/dim] "
                        f"{last_feedback[:200]}"
                    )
                data = await generate_feature(feature_name)
            all_failures.extend(stage_failures)
        except (ValueError, FileNotFoundError):
            all_failures.extend(stage_failures)  # type: ignore[possibly-undefined]
            await asyncio.sleep(RETRY_DELAY)
            continue
        except Exception:
            all_failures.extend(stage_failures)  # type: ignore[possibly-undefined]
            await asyncio.sleep(RETRY_DELAY)
            continue

        files = data.get("files", [])
        await write_generated_files(feature_name, files)
        console.print(f"[green]✓ Generated {len(files)} file(s)[/green]")
        stages_completed.append(f"code_generation:attempt_{attempt}")

        # ── Step 2: Guardrails ─────────────────────────────────────────────
        _step("Guardrail Enforcer — security scan")
        try:
            async with pipeline_stage(
                "guardrail_check",
                attempt=attempt,
                wizard_mode=wizard_mode,
            ) as stage_failures:
                guard_ok = await run_guardrails(feature_name)
            all_failures.extend(stage_failures)
        except Exception:
            all_failures.extend(stage_failures)  # type: ignore[possibly-undefined]
            guard_ok = False

        if not guard_ok:
            console.print("[red]❌ Guardrail check failed — retrying[/red]")
            last_feedback = (
                "Previous output failed the security guardrail scan. "
                "Remove all forbidden operations."
            )
            await asyncio.sleep(RETRY_DELAY)
            continue

        console.print("[green]✓ Guardrails passed[/green]")
        stages_completed.append(f"guardrail_check:attempt_{attempt}")

        # ── Step 3: Review + tests ─────────────────────────────────────────
        _step("Reviewer Agent — running tests + LLM review")
        try:
            async with pipeline_stage(
                "review",
                attempt=attempt,
                wizard_mode=wizard_mode,
            ) as stage_failures:
                verdict = await review_feature(feature_name)
            all_failures.extend(stage_failures)
        except Exception:
            all_failures.extend(stage_failures)  # type: ignore[possibly-undefined]
            await asyncio.sleep(RETRY_DELAY)
            continue

        elapsed = time.monotonic() - t_start
        _print_verdict(verdict, elapsed)

        if verdict.get("overall") == "PASS":
            console.print(
                Panel(
                    f"[bold green]Feature '{feature_name}' APPROVED ✅[/bold green]\n"
                    f"Elapsed: {elapsed:.1f}s",
                    border_style="green",
                )
            )
            stages_completed.append(f"review:attempt_{attempt}")
            final_status = "APPROVED"
            append_run_log(feature_name, stages_completed, all_failures, final_status)
            return True

        stages_completed.append(f"review:attempt_{attempt}")
        # Inject feedback for next attempt
        last_feedback = verdict.get("feedback", "")
        console.print(f"[yellow]↻ Retrying in {RETRY_DELAY}s…[/yellow]")
        await asyncio.sleep(RETRY_DELAY)

    console.print(
        Panel(
            f"[bold red]Feature '{feature_name}' REJECTED ❌[/bold red]\n"
            f"Exceeded {MAX_RETRIES} retries.",
            border_style="red",
        )
    )
    append_run_log(feature_name, stages_completed, all_failures, final_status)
    return False


# ── UI helpers ────────────────────────────────────────────────────────────────


def _banner(feature_name: str) -> None:
    console.print(
        Panel(
            f"[bold cyan]Agentic Dev Workflow[/bold cyan]\n"
            f"Feature: [bold]{feature_name}[/bold]",
            box=box.DOUBLE_EDGE,
        )
    )


def _section(title: str) -> None:
    console.rule(f"[bold blue]{title}[/bold blue]")


def _step(title: str) -> None:
    console.print(f"\n[bold]▶ {title}[/bold]")


def _print_verdict(verdict: dict, elapsed: float) -> None:
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("Check",    style="cyan")
    table.add_column("Result",   justify="center")
    for key in ("prd_check", "tdd_check", "guardrail_check"):
        val = verdict.get(key, "?")
        colour = "green" if val == "PASS" else "red"
        label  = "✅ PASS" if val == "PASS" else "❌ FAIL"
        table.add_row(key.replace("_check", "").upper(), f"[{colour}]{label}[/{colour}]")
    console.print(table)
    if verdict.get("feedback"):
        console.print(f"[dim]Feedback:[/dim] {verdict['feedback']}")


# ── Entry-point ───────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Agentic Dev Workflow — orchestrate the full pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "feature_name",
        help="Name of the feature to build (must match specs/<feature_name>/).",
    )
    parser.add_argument(
        "--wizard",
        action="store_true",
        default=False,
        help=(
            "Enable Wizard mode: on any failure, print an interactive diagnostic "
            "card instead of crashing, and offer auto-fix / retry options."
        ),
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        default=False,
        help="Skip pre-flight health checks (useful in CI/offline environments).",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point: parse args, run preflight, then execute the pipeline."""
    args = _parse_args()

    if not args.skip_preflight:
        run_preflight_checks(abort_on_error=not args.wizard)

    approved = asyncio.run(run_pipeline(args.feature_name, wizard_mode=args.wizard))
    sys.exit(0 if approved else 1)


if __name__ == "__main__":
    main()
