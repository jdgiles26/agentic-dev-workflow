"""
tests/test_wizard.py — Unit tests for tools/wizard.py

Tests cover:
  • FailureEvent construction and serialisation.
  • analyse() decision-tree rules for every documented symptom.
  • diagnose_and_report() in non-interactive (piped) mode.
  • append_run_log() writes a valid JSONL entry.
  • pipeline_stage() context manager captures exceptions as FailureEvents.
"""

from __future__ import annotations

import json
import sys
import types
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import anyio

from tools.wizard import (
    FailureEvent,
    Diagnosis,
    analyse,
    append_run_log,
    pipeline_stage,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_event(
    exc_type: str = "ValueError",
    exc_message: str = "test error",
    stage: str = "test_stage",
    attempt: int = 1,
    raw_llm_response: str = "",
) -> FailureEvent:
    return FailureEvent(
        stage=stage,
        exc_type=exc_type,
        exc_message=exc_message,
        traceback_short="  File test.py, line 1\n    raise ValueError()",
        raw_llm_response=raw_llm_response,
        attempt=attempt,
    )


# ── FailureEvent ──────────────────────────────────────────────────────────────


class TestFailureEvent:
    """FailureEvent construction and serialisation."""

    def test_basic_fields(self) -> None:
        event = _make_event()
        assert event.stage == "test_stage"
        assert event.exc_type == "ValueError"
        assert event.exc_message == "test error"
        assert event.attempt == 1

    def test_to_dict_is_json_serialisable(self) -> None:
        event = _make_event()
        d = event.to_dict()
        assert isinstance(d, dict)
        # Must round-trip through JSON
        assert json.loads(json.dumps(d)) == d

    def test_to_dict_contains_all_fields(self) -> None:
        event = _make_event(raw_llm_response='{"key": "val"}')
        d = event.to_dict()
        for field in ("stage", "exc_type", "exc_message", "traceback_short",
                      "raw_llm_response", "attempt", "extra"):
            assert field in d, f"Missing field: {field}"

    def test_extra_defaults_to_empty_dict(self) -> None:
        event = _make_event()
        assert event.extra == {}


# ── analyse() — decision tree rules ──────────────────────────────────────────


class TestAnalyse:
    """Rule-based decision tree covers every documented symptom."""

    def test_json_decode_error_is_auto_fixable(self) -> None:
        event = _make_event(exc_type="JSONDecodeError", exc_message="json decode error")
        d = analyse(event)
        assert d.auto_fixable is True
        assert d.confidence >= 0.80
        assert "llm_json" in " ".join(d.suggested_fixes).lower() or \
               "parse_llm_json" in " ".join(d.suggested_fixes)

    def test_value_error_with_json_message_triggers_json_rule(self) -> None:
        event = _make_event(exc_type="ValueError", exc_message="could not parse json")
        d = analyse(event)
        assert d.fix_tag == "json_repair"

    def test_key_error_injects_defaults(self) -> None:
        event = _make_event(exc_type="KeyError", exc_message="'files'")
        d = analyse(event)
        assert d.auto_fixable is True
        assert d.fix_tag == "inject_defaults"
        assert any("default" in fix.lower() for fix in d.suggested_fixes)

    def test_file_not_found_for_generated_dir(self) -> None:
        event = _make_event(
            exc_type="FileNotFoundError",
            exc_message="generated/my_feature/src/main.py",
        )
        d = analyse(event)
        assert d.auto_fixable is False
        assert any("re-run" in fix.lower() or "code-gen" in fix.lower()
                   for fix in d.suggested_fixes)

    def test_file_not_found_for_spec(self) -> None:
        # "specs/my_feature/prd.md" does NOT contain "generated/output/feature"
        # so the rule falls through to the generic FileNotFoundError branch.
        event = _make_event(
            exc_type="FileNotFoundError",
            exc_message="specs/my_feature/prd.md not found",
        )
        d = analyse(event)
        assert d.auto_fixable is False
        # Generic FileNotFoundError branch → confidence < 0.90
        assert d.confidence <= 0.90
        # At least one fix must mention paths or re-running
        assert len(d.suggested_fixes) >= 1

    def test_connection_error_suggests_start_server(self) -> None:
        event = _make_event(
            exc_type="ConnectionRefusedError",
            exc_message="connection refused to 127.0.0.1:8012",
        )
        d = analyse(event)
        assert d.auto_fixable is False
        assert any("llama" in fix.lower() or "serve" in fix.lower()
                   for fix in d.suggested_fixes)

    def test_timeout_error_suggests_timeout_increase(self) -> None:
        event = _make_event(exc_type="TimeoutError", exc_message="timed out")
        d = analyse(event)
        assert d.auto_fixable is False
        assert any("timeout" in fix.lower() for fix in d.suggested_fixes)

    def test_runtime_error_with_ollama_message(self) -> None:
        event = _make_event(
            exc_type="RuntimeError",
            exc_message="Cannot reach llama.cpp server at http://127.0.0.1:8012",
        )
        d = analyse(event)
        assert d.auto_fixable is False

    def test_repeated_review_failures(self) -> None:
        event = _make_event(stage="review", attempt=3)
        d = analyse(event)
        assert d.auto_fixable is False
        assert any("threshold" in fix.lower() or "model" in fix.lower()
                   for fix in d.suggested_fixes)

    def test_guardrail_stage_failure(self) -> None:
        # Use a neutral exception type/message that won't match the connection rule
        event = _make_event(
            stage="guardrail_check",
            exc_type="AssertionError",
            exc_message="guardrail scan raised unexpected assertion",
        )
        d = analyse(event)
        assert d.auto_fixable is False
        combined = " ".join(d.suggested_fixes).lower()
        assert "guardrail" in combined or "forbidden" in combined or "permission" in combined

    def test_fallback_for_unknown_error(self) -> None:
        event = _make_event(exc_type="ZeroDivisionError", exc_message="division by zero")
        d = analyse(event)
        assert isinstance(d, Diagnosis)
        assert d.confidence < 0.60
        assert len(d.suggested_fixes) >= 1

    def test_diagnosis_confidence_in_range(self) -> None:
        for exc_type in ("JSONDecodeError", "KeyError", "FileNotFoundError",
                         "ConnectionError", "TimeoutError", "ZeroDivisionError"):
            d = analyse(_make_event(exc_type=exc_type))
            assert 0.0 <= d.confidence <= 1.0, f"Out of range for {exc_type}"


# ── append_run_log() ──────────────────────────────────────────────────────────


class TestAppendRunLog:
    """append_run_log() writes valid JSONL entries."""

    def test_creates_log_file_if_missing(self, tmp_path: Path) -> None:
        log_path = tmp_path / "pipeline_runs.jsonl"
        with patch("tools.wizard.LOG_DIR", tmp_path):
            append_run_log("my_feature", ["spec_validation"], [], "APPROVED")
        assert log_path.exists()

    def test_entry_is_valid_json(self, tmp_path: Path) -> None:
        with patch("tools.wizard.LOG_DIR", tmp_path):
            append_run_log("feat", ["s1", "s2"], [], "REJECTED")
        lines = (tmp_path / "pipeline_runs.jsonl").read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["feature_name"] == "feat"
        assert entry["final_status"] == "REJECTED"
        assert entry["stages_completed"] == ["s1", "s2"]

    def test_multiple_entries_appended(self, tmp_path: Path) -> None:
        with patch("tools.wizard.LOG_DIR", tmp_path):
            append_run_log("f1", [], [], "APPROVED")
            append_run_log("f2", [], [], "REJECTED")
        lines = (tmp_path / "pipeline_runs.jsonl").read_text().splitlines()
        assert len(lines) == 2

    def test_failure_events_serialised(self, tmp_path: Path) -> None:
        evt = _make_event()
        with patch("tools.wizard.LOG_DIR", tmp_path):
            append_run_log("feat", [], [evt], "REJECTED")
        entry = json.loads((tmp_path / "pipeline_runs.jsonl").read_text())
        assert len(entry["failures"]) == 1
        assert entry["failures"][0]["exc_type"] == "ValueError"

    def test_timestamp_present_and_iso_format(self, tmp_path: Path) -> None:
        from datetime import datetime
        with patch("tools.wizard.LOG_DIR", tmp_path):
            append_run_log("feat", [], [], "APPROVED")
        entry = json.loads((tmp_path / "pipeline_runs.jsonl").read_text())
        # Should not raise
        datetime.fromisoformat(entry["timestamp"])


# ── pipeline_stage() context manager ─────────────────────────────────────────


class TestPipelineStage:
    """pipeline_stage() wraps exceptions into FailureEvents."""

    def test_no_exception_yields_empty_failures(self) -> None:
        async def _run():
            async with pipeline_stage("test") as failures:
                pass
            assert failures == []

        anyio.run(_run)

    def test_exception_creates_failure_event(self) -> None:
        failures_out: list = []

        async def _run():
            with pytest.raises(ValueError):
                async with pipeline_stage("my_stage") as failures:
                    raise ValueError("boom")
            failures_out.extend(failures)

        anyio.run(_run)
        assert len(failures_out) == 1
        assert failures_out[0].stage == "my_stage"
        assert failures_out[0].exc_type == "ValueError"
        assert "boom" in failures_out[0].exc_message

    def test_exception_re_raised(self) -> None:
        async def _run():
            with pytest.raises(KeyError):
                async with pipeline_stage("s"):
                    raise KeyError("missing_key")

        anyio.run(_run)

    def test_raw_llm_ref_captured(self) -> None:
        failures_out: list = []

        async def _run():
            raw_ref: list[str] = [""]
            with pytest.raises(RuntimeError):
                async with pipeline_stage("s", raw_llm_response_ref=raw_ref) as failures:
                    raw_ref[0] = '{"partial": true'
                    raise RuntimeError("oops")
            failures_out.extend(failures)

        anyio.run(_run)
        assert failures_out[0].raw_llm_response == '{"partial": true'

    def test_wizard_mode_calls_diagnose(self) -> None:
        async def _run():
            with patch("tools.wizard.diagnose_and_report") as mock_report:
                with pytest.raises(ValueError):
                    async with pipeline_stage("s", wizard_mode=True):
                        raise ValueError("test")
                mock_report.assert_called_once()

        anyio.run(_run)


# ── diagnose_and_report() — smoke test ───────────────────────────────────────


class TestDiagnoseAndReport:
    """diagnose_and_report() prints the card without crashing (no-colour, non-interactive)."""

    def test_prints_without_crashing(self, capsys: pytest.CaptureFixture) -> None:
        event = _make_event(exc_type="JSONDecodeError", exc_message="json error")
        # Provide "n" to the auto-fix prompt, then "3" to abort the menu
        with patch("builtins.input", side_effect=["n", "3"]):
            with patch.dict("os.environ", {"NO_COLOR": "1"}):
                from tools.wizard import diagnose_and_report as _dar
                _dar(event)
        out = capsys.readouterr().out
        assert "WIZARD DIAGNOSTIC" in out
        assert "JSONDecodeError" in out

    def test_auto_fix_triggered_on_y_answer(self, capsys: pytest.CaptureFixture) -> None:
        event = _make_event(
            exc_type="JSONDecodeError",
            exc_message="json decode error",
            raw_llm_response='{"files": []}',
        )
        with patch("builtins.input", side_effect=["y", "3"]):
            with patch.dict("os.environ", {"NO_COLOR": "1"}):
                from tools.wizard import diagnose_and_report as _dar
                _dar(event)
        out = capsys.readouterr().out
        # Auto-fix was attempted (JSON repaired successfully)
        assert "✓" in out or "repaired" in out.lower()
