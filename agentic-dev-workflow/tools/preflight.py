"""
preflight.py — Pre-flight health checks run before any LLM call.

Checks performed by run_preflight_checks():
  1. llama.cpp server is reachable (HTTP GET to LLAMA_HOST/v1/models).
  2. At least one model is loaded and available.
  3. All required Python dependencies are importable.
  4. The OUTPUT_DIR (generated/) is writable.

Failed checks either abort the pipeline (severity='error') or emit a warning
(severity='warning').  All failures are also returned as FailureEvent objects
so the Wizard diagnostic layer can incorporate them.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import urllib.error
import urllib.request
import json
from dataclasses import dataclass
from typing import Literal

# config is imported at module level so tests can patch tools.preflight.OUTPUT_DIR.
from tools.config import OUTPUT_DIR  # noqa: F401 (re-exported for test patching)
# wizard is imported here only for FailureEvent (no LLM side-effects).
from tools.wizard import FailureEvent

# ── Required dependencies ─────────────────────────────────────────────────────

_REQUIRED_PACKAGES: list[tuple[str, str]] = [
    ("openai",    "openai"),
    ("yaml",      "pyyaml"),
    ("dotenv",    "python-dotenv"),
    ("rich",      "rich"),
    ("pytest",    "pytest"),
]

# ── Result type ───────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    """Result of a single pre-flight check."""

    name: str
    passed: bool
    message: str
    severity: Literal["error", "warning"]
    failure_event: FailureEvent | None = None


# ── Colour helpers (reuse wizard approach) ────────────────────────────────────

_USE_COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR", "")


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text


def _green(t: str) -> str:
    return _c(t, "32")


def _red(t: str) -> str:
    return _c(t, "31")


def _yellow(t: str) -> str:
    return _c(t, "33")


def _bold(t: str) -> str:
    return _c(t, "1")


# ── Individual checks ─────────────────────────────────────────────────────────


def _check_server_reachable() -> CheckResult:
    """Check 1 — llama.cpp server responds to HTTP GET /v1/models."""
    from tools.config import LLAMA_HOST  # noqa: PLC0415

    url = f"{LLAMA_HOST.rstrip('/')}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            _ = resp.read()
        return CheckResult(
            name="llama.cpp server reachable",
            passed=True,
            message=f"Server at {LLAMA_HOST} responded OK.",
            severity="error",
        )
    except (urllib.error.URLError, OSError) as exc:
        event = FailureEvent(
            stage="preflight:server_reachable",
            exc_type=type(exc).__name__,
            exc_message=str(exc)[:300],
            traceback_short="",
        )
        return CheckResult(
            name="llama.cpp server reachable",
            passed=False,
            message=(
                f"Cannot reach llama.cpp server at {LLAMA_HOST}.\n"
                f"  → Start it:  llama serve --fim-qwen-3b-default -ngl 99 --port 8012\n"
                f"  → Or update LLAMA_HOST in .env.\n"
                f"  Original error: {exc}"
            ),
            severity="error",
            failure_event=event,
        )


def _check_model_available() -> CheckResult:
    """Check 2 — at least one model is loaded on the llama.cpp server."""
    from tools.config import LLAMA_HOST  # noqa: PLC0415

    url = f"{LLAMA_HOST.rstrip('/')}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read())
        models = data.get("data") or data.get("models") or []
        if models:
            names = [m.get("id") or m.get("name", "?") for m in models]
            return CheckResult(
                name="model available",
                passed=True,
                message=f"{len(models)} model(s) loaded: {names}",
                severity="error",
            )
        event = FailureEvent(
            stage="preflight:model_available",
            exc_type="RuntimeError",
            exc_message="No models loaded on the llama.cpp server.",
            traceback_short="",
        )
        return CheckResult(
            name="model available",
            passed=False,
            message=(
                "llama.cpp server is running but no models are loaded.\n"
                "  → Start the server with a model:  "
                "llama serve --fim-qwen-3b-default -ngl 99 --port 8012"
            ),
            severity="error",
            failure_event=event,
        )
    except Exception:
        # Server unreachable — already reported by _check_server_reachable
        return CheckResult(
            name="model available",
            passed=False,
            message="Skipped — server unreachable (see check 1).",
            severity="warning",
        )


def _check_dependencies() -> CheckResult:
    """Check 3 — all required Python packages are importable."""
    missing: list[str] = []
    for import_name, pip_name in _REQUIRED_PACKAGES:
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pip_name)

    if not missing:
        return CheckResult(
            name="Python dependencies",
            passed=True,
            message="All required packages are importable.",
            severity="error",
        )

    event = FailureEvent(
        stage="preflight:dependencies",
        exc_type="ImportError",
        exc_message=f"Missing packages: {', '.join(missing)}",
        traceback_short="",
    )
    return CheckResult(
        name="Python dependencies",
        passed=False,
        message=(
            f"Missing packages: {', '.join(missing)}\n"
            f"  → Install them:  pip install -r requirements.txt"
        ),
        severity="error",
        failure_event=event,
    )


def _check_output_writable() -> CheckResult:
    """Check 4 — OUTPUT_DIR exists (or can be created) and is writable."""
    # OUTPUT_DIR is imported at module level; use module reference so tests can patch it.
    import tools.preflight as _self  # noqa: PLC0415
    out_dir = _self.OUTPUT_DIR

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        # Write a canary file to prove write access
        with tempfile.NamedTemporaryFile(dir=out_dir, delete=True):
            pass
        return CheckResult(
            name="output directory writable",
            passed=True,
            message=f"OUTPUT_DIR {out_dir} is writable.",
            severity="error",
        )
    except (OSError, PermissionError) as exc:
        event = FailureEvent(
            stage="preflight:output_writable",
            exc_type=type(exc).__name__,
            exc_message=str(exc)[:300],
            traceback_short="",
        )
        return CheckResult(
            name="output directory writable",
            passed=False,
            message=(
                f"Cannot write to OUTPUT_DIR {out_dir}.\n"
                f"  → Fix permissions:  chmod -R u+w {out_dir}\n"
                f"  Original error: {exc}"
            ),
            severity="error",
            failure_event=event,
        )


# ── Orchestrator ──────────────────────────────────────────────────────────────


def run_preflight_checks(abort_on_error: bool = True) -> list[FailureEvent]:
    """
    Run all pre-flight checks and print a results table.

    Parameters
    ----------
    abort_on_error:
        If True (default) and any error-severity check fails, print a summary
        and call ``sys.exit(1)``.  Set to False in tests / non-interactive use.

    Returns
    -------
    list[FailureEvent]
        FailureEvents for every failed check (empty on full success).
    """
    checks = [
        _check_server_reachable,
        _check_model_available,
        _check_dependencies,
        _check_output_writable,
    ]

    print(_bold("\n── Pre-flight checks ─────────────────────────────────────"))
    results: list[CheckResult] = []
    for fn in checks:
        r = fn()
        results.append(r)
        icon = _green("✓") if r.passed else (
            _red("✗") if r.severity == "error" else _yellow("⚠")
        )
        label = _bold(r.name)
        status = _green("OK") if r.passed else (
            _red("FAIL") if r.severity == "error" else _yellow("WARN")
        )
        print(f"  {icon}  {label:<35} {status}")
        if not r.passed:
            for line in r.message.splitlines():
                print(f"     {line}")

    print()

    failures = [r.failure_event for r in results if not r.passed and r.failure_event]
    hard_failures = [r for r in results if not r.passed and r.severity == "error"]

    if hard_failures and abort_on_error:
        print(_red(_bold(
            f"  {len(hard_failures)} pre-flight check(s) failed. "
            "Fix the issues above and re-run."
        )))
        sys.exit(1)

    return failures
