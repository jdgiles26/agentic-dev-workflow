"""
config.py — Load .agentconfig.yaml and environment variables into a single
            config object consumed by every tool in this package.

Backend: llama.cpp server (OpenAI-compatible /v1/ API).
"""

import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _load_yaml() -> dict:
    cfg_path = BASE_DIR / ".agentconfig.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    with open(cfg_path, "r") as fh:
        return yaml.safe_load(fh)


_cfg = _load_yaml()

# ── llama.cpp server ──────────────────────────────────────────────────────────
_llama = _cfg.get("llama", {})

LLAMA_HOST: str = os.getenv(
    "LLAMA_HOST", _llama.get("host", "http://127.0.0.1:8012")
)
LLAMA_TIMEOUT: int = int(
    os.getenv("LLAMA_TIMEOUT", str(_llama.get("timeout", 120)))
)
LLAMA_MODE: str = _llama.get("mode", "auto")

# Base model assignments from YAML
LLAMA_MODELS: dict[str, str] = dict(_llama.get("models", {}))

# Env var overrides take highest priority
for _role, _env in [
    ("developer", "LLAMA_DEVELOPER_MODEL"),
    ("reviewer",  "LLAMA_REVIEWER_MODEL"),
    ("security",  "LLAMA_SECURITY_MODEL"),
    ("validator", "LLAMA_VALIDATOR_MODEL"),
]:
    _val = os.getenv(_env, "").strip()
    if _val:
        LLAMA_MODELS[_role] = _val

LLAMA_HINTS: dict[str, list[str]] = _llama.get("hints", {})

# ── Orchestration ─────────────────────────────────────────────────────────────
MAX_RETRIES: int = int(_cfg.get("max_retries", 3))
RETRY_DELAY: float = float(_cfg.get("retry_delay", 2))

# ── Paths ─────────────────────────────────────────────────────────────────────
_paths = _cfg.get("paths", {})
SPECS_DIR:  Path = BASE_DIR / _paths.get("specs",  "specs/")
OUTPUT_DIR: Path = BASE_DIR / _paths.get("output", "generated/")
LOG_DIR:    Path = BASE_DIR / _paths.get("logs",   "logs/")

# ── Guardrails ────────────────────────────────────────────────────────────────
_gr = _cfg.get("guardrails", {})
GUARDRAILS_ENFORCE:     bool = bool(_gr.get("enforce", True))
FORBIDDEN_ACTIONS_FILE: Path = BASE_DIR / _gr.get(
    "forbidden_actions_file", "guardrails/forbidden_actions.yaml"
)
PERMISSIONS_FILE: Path = BASE_DIR / _gr.get(
    "permissions_file", "guardrails/permission_boundaries.json"
)
ALLOW_NETWORK: bool = bool(_gr.get("allow_network", False))

# ── Review ────────────────────────────────────────────────────────────────────
COVERAGE_THRESHOLD: int = int(_cfg.get("review", {}).get("coverage_threshold", 80))

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
