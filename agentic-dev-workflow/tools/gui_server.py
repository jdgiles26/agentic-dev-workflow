"""
gui_server.py — Stdlib HTTP server that exposes the agentic pipeline as a
                web GUI.  No third-party web framework required.

Endpoints
─────────
  GET  /               → serve gui.html
  GET  /api/config     → read .agentconfig.yaml + .env and return JSON
  POST /api/config     → write fields back to .agentconfig.yaml / .env
  GET  /api/specs      → list feature directories under specs/
  GET  /api/spec?f=X   → read prd.md + tdd.yaml for feature X
  POST /api/spec       → write prd.md + tdd.yaml for feature X
  GET  /api/preflight  → run preflight checks, return JSON results
  GET  /api/run?f=X[&wizard=1]
                       → SSE stream — runs the full pipeline for feature X
                         and emits real-time log lines as `data:` events
  GET  /api/log        → return last N lines of logs/agent_runner.log
  GET  /api/runs       → return last 20 entries from logs/pipeline_runs.jsonl

Usage
─────
  python -m tools.gui_server          # opens browser automatically
  python -m tools.gui_server --port 8099 --no-browser
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

# ── Paths ─────────────────────────────────────────────────────────────────────

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_GUI_HTML = _HERE / "gui.html"
_AGENTCONFIG = _ROOT / ".agentconfig.yaml"
_ENV_FILE = _ROOT / ".env"
_SPECS_DIR = _ROOT / "specs"
_LOGS_DIR = _ROOT / "logs"
_PIPELINE_RUNS_LOG = _LOGS_DIR / "pipeline_runs.jsonl"
_AGENT_LOG = _LOGS_DIR / "agent_runner.log"

logger = logging.getLogger("gui_server")

# ── Helpers ───────────────────────────────────────────────────────────────────


def _load_agentconfig() -> dict[str, Any]:
    if not _AGENTCONFIG.exists():
        return {}
    with _AGENTCONFIG.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _save_agentconfig(data: dict) -> None:
    with _AGENTCONFIG.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, allow_unicode=True)


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return env
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def _save_env(updates: dict[str, str]) -> None:
    """Write key=value pairs to .env, preserving comments and existing keys."""
    lines: list[str] = []
    if _ENV_FILE.exists():
        lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()

    touched: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                touched.add(key)
                continue
        new_lines.append(line)

    for key, val in updates.items():
        if key not in touched:
            new_lines.append(f"{key}={val}")

    _ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _list_features() -> list[str]:
    if not _SPECS_DIR.exists():
        return []
    return sorted(
        d.name
        for d in _SPECS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )


def _read_spec(feature: str) -> dict[str, str]:
    prd = (_SPECS_DIR / feature / "prd.md")
    tdd = (_SPECS_DIR / feature / "tdd.yaml")
    return {
        "prd": prd.read_text(encoding="utf-8") if prd.exists() else "",
        "tdd": tdd.read_text(encoding="utf-8") if tdd.exists() else "",
    }


def _write_spec(feature: str, prd: str, tdd: str) -> None:
    feat_dir = _SPECS_DIR / feature
    feat_dir.mkdir(parents=True, exist_ok=True)
    (feat_dir / "prd.md").write_text(prd, encoding="utf-8")
    (feat_dir / "tdd.yaml").write_text(tdd, encoding="utf-8")


def _run_preflight() -> list[dict]:
    """Run preflight checks and return results as a list of dicts (JSON-safe)."""
    # Import lazily so the server can start even if tools aren't on path yet
    sys.path.insert(0, str(_ROOT))
    from tools.preflight import (  # noqa: PLC0415
        _check_server_reachable,
        _check_model_available,
        _check_dependencies,
        _check_output_writable,
    )
    checks = [
        _check_server_reachable,
        _check_model_available,
        _check_dependencies,
        _check_output_writable,
    ]
    results = []
    for fn in checks:
        r = fn()
        results.append({
            "name": r.name,
            "passed": r.passed,
            "message": r.message,
            "severity": r.severity,
        })
    return results


def _tail_log(n: int = 200) -> list[str]:
    if not _AGENT_LOG.exists():
        return []
    lines = _AGENT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-n:]


def _recent_runs(n: int = 20) -> list[dict]:
    if not _PIPELINE_RUNS_LOG.exists():
        return []
    lines = _PIPELINE_RUNS_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    runs = []
    for line in lines[-n:]:
        line = line.strip()
        if line:
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return list(reversed(runs))


# ── Pipeline SSE runner ───────────────────────────────────────────────────────


def _stream_pipeline(feature: str, wizard: bool, event_queue: "queue.Queue[str | None]") -> None:
    """
    Run the real pipeline in a subprocess, capturing stdout+stderr line by line
    and pushing each line onto *event_queue* as an SSE `data:` string.

    Pushes ``None`` as a sentinel when the subprocess finishes.
    """
    cmd = [
        sys.executable, "-m", "tools.agent_runner",
        feature,
        "--skip-preflight",
    ]
    if wizard:
        cmd.append("--wizard")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(_ROOT),
            env={**os.environ, "NO_COLOR": "1", "PYTHONPATH": str(_ROOT)},
        )
    except (OSError, FileNotFoundError) as exc:
        event_queue.put(f"ERROR: Could not start pipeline process: {exc}")
        event_queue.put(None)
        return

    assert proc.stdout is not None
    for line in proc.stdout:
        event_queue.put(line.rstrip("\n"))

    proc.wait()
    exit_code = proc.returncode
    event_queue.put(f"__EXIT__{exit_code}")
    event_queue.put(None)  # sentinel


# ── HTTP handler ──────────────────────────────────────────────────────────────


class _ReuseServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with SO_REUSEADDR always enabled."""

    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    """Handle all GUI server requests."""

    # ── Routing ───────────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        routes_get = {
            "/":              self._serve_html,
            "/gui.html":      self._serve_html,
            "/api/config":    self._api_config_get,
            "/api/specs":     self._api_specs,
            "/api/preflight": self._api_preflight,
            "/api/log":       self._api_log,
            "/api/runs":      self._api_runs,
        }

        if path in routes_get:
            routes_get[path](qs)
        elif path == "/api/spec":
            self._api_spec_get(qs)
        elif path == "/api/run":
            self._api_run_sse(qs)
        else:
            self._send_json({"error": f"Not found: {path}"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        body = self._read_body()

        if path == "/api/config":
            self._api_config_post(body)
        elif path == "/api/spec":
            self._api_spec_post(body)
        else:
            self._send_json({"error": f"Not found: {path}"}, status=404)

    # ── Route handlers ────────────────────────────────────────────────────────

    def _serve_html(self, _qs: dict) -> None:
        if not _GUI_HTML.exists():
            self._send_json({"error": "gui.html not found"}, status=404)
            return
        content = _GUI_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _api_config_get(self, _qs: dict) -> None:
        cfg = _load_agentconfig()
        env = _load_env()
        self._send_json({"agentconfig": cfg, "env": env})

    def _api_config_post(self, body: bytes) -> None:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return

        if "agentconfig" in payload:
            _save_agentconfig(payload["agentconfig"])
        if "env" in payload:
            _save_env(payload["env"])
        self._send_json({"ok": True})

    def _api_specs(self, _qs: dict) -> None:
        self._send_json({"features": _list_features()})

    def _api_spec_get(self, qs: dict) -> None:
        feature = (qs.get("f") or [""])[0].strip()
        if not feature:
            self._send_json({"error": "Missing ?f=<feature>"}, status=400)
            return
        self._send_json(_read_spec(feature))

    def _api_spec_post(self, body: bytes) -> None:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        feature = payload.get("feature", "").strip()
        if not feature:
            self._send_json({"error": "Missing 'feature' field"}, status=400)
            return
        _write_spec(feature, payload.get("prd", ""), payload.get("tdd", ""))
        self._send_json({"ok": True})

    def _api_preflight(self, _qs: dict) -> None:
        try:
            results = _run_preflight()
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)
            return
        self._send_json({"checks": results})

    def _api_run_sse(self, qs: dict) -> None:
        feature = (qs.get("f") or [""])[0].strip()
        if not feature:
            self.send_response(400)
            self.end_headers()
            return
        wizard = (qs.get("wizard") or ["0"])[0] == "1"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q: queue.Queue[str | None] = queue.Queue()
        t = threading.Thread(target=_stream_pipeline, args=(feature, wizard, q), daemon=True)
        t.start()

        try:
            while True:
                item = q.get(timeout=180)
                if item is None:
                    self._sse_write("__DONE__")
                    break
                self._sse_write(item)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            t.join(timeout=5)

    def _api_log(self, qs: dict) -> None:
        n = int((qs.get("n") or ["200"])[0])
        self._send_json({"lines": _tail_log(n)})

    def _api_runs(self, _qs: dict) -> None:
        self._send_json({"runs": _recent_runs()})

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _sse_write(self, text: str) -> None:
        line = f"data: {text}\n\n".encode("utf-8")
        self.wfile.write(line)
        self.wfile.flush()

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: D102
        logger.debug(fmt, *args)


# ── Entry-point ───────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Agentic Dev Workflow — GUI server")
    p.add_argument("--port", type=int, default=8099, help="TCP port (default 8099)")
    p.add_argument("--no-browser", action="store_true", help="Don't open browser on start")
    return p.parse_args()


def main() -> None:
    """Start the GUI server."""
    args = _parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    # Ensure repo root is importable
    sys.path.insert(0, str(_ROOT))

    server = _ReuseServer(("127.0.0.1", args.port), _Handler)
    url = f"http://127.0.0.1:{args.port}"
    logger.info("GUI server running at %s  (Ctrl-C to stop)", url)

    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=[url]).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopped.")


if __name__ == "__main__":
    main()
