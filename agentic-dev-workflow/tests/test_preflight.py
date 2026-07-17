"""
tests/test_preflight.py — Unit tests for tools/preflight.py

Tests cover:
  • _check_server_reachable() — success and failure paths.
  • _check_model_available() — models present vs. empty.
  • _check_dependencies() — all-present vs. missing package.
  • _check_output_writable() — writable vs. permission-denied.
  • run_preflight_checks() — integration: collects failures, aborts on error.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

import pytest


# ── _check_server_reachable() ─────────────────────────────────────────────────


class TestCheckServerReachable:
    def _import(self):
        from tools.preflight import _check_server_reachable
        return _check_server_reachable

    def test_returns_passed_on_success(self) -> None:
        fn = self._import()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"data": []}'

        with patch("tools.preflight.urllib.request.urlopen", return_value=mock_resp):
            result = fn()
        assert result.passed is True
        assert result.failure_event is None

    def test_returns_failed_on_url_error(self) -> None:
        fn = self._import()
        with patch(
            "tools.preflight.urllib.request.urlopen",
            side_effect=urllib.error.URLError("refused"),
        ):
            result = fn()
        assert result.passed is False
        assert result.severity == "error"
        assert result.failure_event is not None
        assert "preflight" in result.failure_event.stage


# ── _check_model_available() ──────────────────────────────────────────────────


class TestCheckModelAvailable:
    def _import(self):
        from tools.preflight import _check_model_available
        return _check_model_available

    def _mock_urlopen(self, body: bytes):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = body
        return mock_resp

    def test_models_present(self) -> None:
        fn = self._import()
        body = b'{"data": [{"id": "mymodel"}]}'
        with patch(
            "tools.preflight.urllib.request.urlopen",
            return_value=self._mock_urlopen(body),
        ):
            result = fn()
        assert result.passed is True

    def test_empty_model_list(self) -> None:
        fn = self._import()
        body = b'{"data": []}'
        with patch(
            "tools.preflight.urllib.request.urlopen",
            return_value=self._mock_urlopen(body),
        ):
            result = fn()
        assert result.passed is False
        assert result.failure_event is not None

    def test_server_unreachable_returns_warning(self) -> None:
        fn = self._import()
        with patch(
            "tools.preflight.urllib.request.urlopen",
            side_effect=Exception("unreachable"),
        ):
            result = fn()
        assert result.passed is False
        assert result.severity == "warning"


# ── _check_dependencies() ─────────────────────────────────────────────────────


class TestCheckDependencies:
    def _import(self):
        from tools.preflight import _check_dependencies
        return _check_dependencies

    def test_all_present(self) -> None:
        fn = self._import()
        with patch("tools.preflight.importlib.import_module", return_value=MagicMock()):
            result = fn()
        assert result.passed is True
        assert result.failure_event is None

    def test_missing_package(self) -> None:
        fn = self._import()

        def _fake_import(name: str) -> None:
            if name == "openai":
                raise ImportError("no module")
            return MagicMock()

        with patch("tools.preflight.importlib.import_module", side_effect=_fake_import):
            result = fn()
        assert result.passed is False
        assert "openai" in result.message
        assert result.failure_event is not None


# ── _check_output_writable() ──────────────────────────────────────────────────


class TestCheckOutputWritable:
    def _import(self):
        from tools.preflight import _check_output_writable
        return _check_output_writable

    def test_writable_dir(self, tmp_path: Path) -> None:
        fn = self._import()
        import tools.preflight as pf
        original = pf.OUTPUT_DIR
        try:
            pf.OUTPUT_DIR = tmp_path
            result = fn()
        finally:
            pf.OUTPUT_DIR = original
        assert result.passed is True

    def test_non_writable_dir(self, tmp_path: Path) -> None:
        fn = self._import()
        import tools.preflight as pf
        original = pf.OUTPUT_DIR
        try:
            pf.OUTPUT_DIR = tmp_path
            # Simulate a PermissionError when creating a temp file
            with patch(
                "tools.preflight.tempfile.NamedTemporaryFile",
                side_effect=PermissionError("read-only"),
            ):
                result = fn()
        finally:
            pf.OUTPUT_DIR = original
        assert result.passed is False
        assert result.severity == "error"
        assert result.failure_event is not None


# ── run_preflight_checks() — integration ─────────────────────────────────────


class TestRunPreflightChecks:
    """Integration tests for the orchestrator."""

    def _all_pass_patches(self):
        """Return a dict of patches that make every check pass."""
        from tools.preflight import CheckResult
        ok = CheckResult(
            name="x", passed=True, message="ok", severity="error", failure_event=None
        )

        return {
            "tools.preflight._check_server_reachable": lambda: ok,
            "tools.preflight._check_model_available": lambda: ok,
            "tools.preflight._check_dependencies": lambda: ok,
            "tools.preflight._check_output_writable": lambda: ok,
        }

    def test_returns_empty_list_on_success(self, capsys: pytest.CaptureFixture) -> None:
        from tools.preflight import run_preflight_checks, CheckResult

        ok = CheckResult("ok", True, "ok", "error")
        with patch("tools.preflight._check_server_reachable", return_value=ok), \
             patch("tools.preflight._check_model_available", return_value=ok), \
             patch("tools.preflight._check_dependencies", return_value=ok), \
             patch("tools.preflight._check_output_writable", return_value=ok):
            failures = run_preflight_checks(abort_on_error=False)
        assert failures == []

    def test_returns_failure_events_on_error(self, capsys: pytest.CaptureFixture) -> None:
        from tools.preflight import run_preflight_checks, CheckResult
        from tools.wizard import FailureEvent

        ok = CheckResult("ok", True, "ok", "error")
        bad_event = FailureEvent(
            stage="preflight:server_reachable",
            exc_type="URLError",
            exc_message="refused",
            traceback_short="",
        )
        bad = CheckResult("bad", False, "cannot connect", "error", failure_event=bad_event)

        with patch("tools.preflight._check_server_reachable", return_value=bad), \
             patch("tools.preflight._check_model_available", return_value=ok), \
             patch("tools.preflight._check_dependencies", return_value=ok), \
             patch("tools.preflight._check_output_writable", return_value=ok):
            failures = run_preflight_checks(abort_on_error=False)
        assert len(failures) == 1
        assert failures[0].exc_type == "URLError"

    def test_aborts_on_hard_failure(self, capsys: pytest.CaptureFixture) -> None:
        from tools.preflight import run_preflight_checks, CheckResult
        from tools.wizard import FailureEvent

        bad_event = FailureEvent("s", "E", "m", "")
        bad = CheckResult("bad", False, "fail", "error", failure_event=bad_event)
        ok = CheckResult("ok", True, "ok", "error")

        with patch("tools.preflight._check_server_reachable", return_value=bad), \
             patch("tools.preflight._check_model_available", return_value=ok), \
             patch("tools.preflight._check_dependencies", return_value=ok), \
             patch("tools.preflight._check_output_writable", return_value=ok):
            with pytest.raises(SystemExit) as exc_info:
                run_preflight_checks(abort_on_error=True)
        assert exc_info.value.code == 1
