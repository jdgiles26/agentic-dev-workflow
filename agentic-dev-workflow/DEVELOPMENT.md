# DEVELOPMENT.md — Setup, workflow, testing, linting, and contribution guide

---

## Table of contents

1. [Prerequisites](#1-prerequisites)
2. [Full environment setup](#2-full-environment-setup)
3. [Project structure for developers](#3-project-structure-for-developers)
4. [Running the pipeline — all modes](#4-running-the-pipeline--all-modes)
5. [Running the GUI server](#5-running-the-gui-server)
6. [Running tests](#6-running-tests)
7. [Linting and formatting](#7-linting-and-formatting)
8. [Working with the LLM backend](#8-working-with-the-llm-backend)
9. [Modifying agent instructions](#9-modifying-agent-instructions)
10. [Modifying guardrail rules](#10-modifying-guardrail-rules)
11. [Environment variables reference](#11-environment-variables-reference)
12. [Common development tasks](#12-common-development-tasks)
13. [CI checklist](#13-ci-checklist)

---

## 1. Prerequisites

### Required

- **Python 3.11+** — `python --version` to check
- **pip** — comes with Python
- **llama.cpp** — built from source or installed via Homebrew/package manager
- **A GGUF model file** — see [§8](#8-working-with-the-llm-backend) for
  download instructions

### Optional but recommended

- **pyenv** or **mise** for Python version management
- **git** for version control
- **ruff** for linting (installed globally or into the venv)
- A terminal that supports Unicode and ANSI colours (iTerm2, Windows Terminal,
  any modern Linux terminal)

---

## 2. Full environment setup

```bash
# 1. Clone and enter the project
git clone <repo-url>
cd agentic-dev-workflow

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows PowerShell

# 3. Install all dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Install the package in editable mode
# (allows `from tools.xxx import` without sys.path hacks)
pip install -e .

# 5. Set up environment config
cp .env.example .env
# Edit .env with your preferred editor

# 6. Verify the setup
python -c "from tools.config import LLAMA_HOST; print('Config OK. Host:', LLAMA_HOST)"

# 7. Start your llama.cpp server (separate terminal)
llama serve --fim-qwen-3b-default -ngl 99 --port 8012

# 8. Run pre-flight checks
python -c "from tools.preflight import run_preflight_checks; run_preflight_checks()"

# 9. Run the example feature
python -m tools.agent_runner example_feature
```

### Editable install explained

`pip install -e .` reads `pyproject.toml` and creates a `.pth` file in the
venv's `site-packages` pointing back to the repo root. This means
`from tools.config import LLAMA_HOST` resolves correctly whether you run from
the repo root or any subdirectory. Without it, `tools` is not on `sys.path`
unless you set `PYTHONPATH=.` manually.

---

## 3. Project structure for developers

```
agentic-dev-workflow/
│
├── tools/                     ← all pipeline code lives here
│   ├── __init__.py
│   ├── agent_runner.py        ← start here to understand the pipeline
│   ├── config.py              ← all config constants
│   ├── developer_agent.py
│   ├── guardrail_enforcer.py
│   ├── gui.html               ← single-file GUI (HTML+CSS+JS, no build step)
│   ├── gui_server.py
│   ├── llm_client.py
│   ├── llm_json.py
│   ├── model_manager.py
│   ├── preflight.py
│   ├── reviewer_agent.py
│   ├── spec_validator.py
│   └── wizard.py
│
├── tests/                     ← unit tests (no integration tests — no mock LLM)
│   ├── conftest.py
│   ├── test_preflight.py
│   └── test_wizard.py
│
├── specs/                     ← feature specs (commit these)
│   ├── template.prd.md
│   ├── template.tdd.yaml
│   └── example_feature/
│
├── agent_instructions/        ← LLM system prompts (commit these)
├── guardrails/                ← security rules (commit these)
│
├── generated/                 ← pipeline output (gitignored — do not commit)
└── logs/                      ← runtime logs (gitignored — do not commit)
```

The `generated/` and `logs/` directories are created automatically on first
run. Do not commit them — they contain LLM output and logs that are
environment-specific.

---

## 4. Running the pipeline — all modes

### Basic run

```bash
python -m tools.agent_runner <feature_name>
```

### With Wizard mode (recommended when debugging failures)

```bash
python -m tools.agent_runner <feature_name> --wizard
```

In Wizard mode, every failure prints a structured diagnostic card and offers
an interactive recovery menu. Use this when:
- The pipeline keeps failing on the same stage
- You want to see the root cause diagnosis
- You want to attempt auto-fix for JSON parse failures

### Skip pre-flight (CI or air-gapped environments)

```bash
python -m tools.agent_runner <feature_name> --skip-preflight
```

Pre-flight checks make HTTP requests to the LLM server. Skip them in CI
pipelines where you've already verified the environment separately.

### Combine flags

```bash
python -m tools.agent_runner <feature_name> --wizard --skip-preflight
```

### Debug logging

```bash
LOG_LEVEL=DEBUG python -m tools.agent_runner <feature_name>
```

Debug level logs every LLM call's input character count, raw output (first
300 chars), and all intermediate parse attempts.

### Run a specific feature from the GUI

```bash
python -m tools.gui_server
# Navigate to Step 3 in the browser
```

---

## 5. Running the GUI server

```bash
# Default: port 8099, opens browser automatically
python -m tools.gui_server

# Custom port
python -m tools.gui_server --port 9000

# Headless (server only, no browser open)
python -m tools.gui_server --no-browser

# Stop the server
# Ctrl-C in the terminal where it's running
```

### Killing a stuck server

```bash
# Find and kill the process using port 8099
lsof -ti tcp:8099 | xargs kill -9

# Or find by name
pkill -f gui_server
```

### GUI endpoint reference

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Serve `gui.html` |
| `/api/config` | GET | Read `.agentconfig.yaml` + `.env` as JSON |
| `/api/config` | POST | Write fields back to `.agentconfig.yaml` / `.env` |
| `/api/specs` | GET | List feature directories under `specs/` |
| `/api/spec?f=<name>` | GET | Read `prd.md` + `tdd.yaml` for feature |
| `/api/spec` | POST | Write `prd.md` + `tdd.yaml` for feature |
| `/api/preflight` | GET | Run all four preflight checks, return JSON |
| `/api/run?f=<name>[&wizard=1]` | GET | SSE stream of real pipeline output |
| `/api/log?n=<N>` | GET | Last N lines of `logs/agent_runner.log` |
| `/api/runs` | GET | Last 20 entries from `logs/pipeline_runs.jsonl` |

---

## 6. Running tests

```bash
# Run all tests
pytest tests/ -v

# With coverage
pytest tests/ -v --cov=tools --cov-report=term-missing

# Only wizard tests
pytest tests/test_wizard.py -v

# Only preflight tests
pytest tests/test_preflight.py -v

# A single test
pytest tests/test_wizard.py::TestAnalyse::test_json_decode_error_is_auto_fixable -v

# Stop on first failure
pytest tests/ -v -x
```

### Test design principles

- **No LLM calls in tests.** All LLM interactions are mocked via
  `unittest.mock.patch`.
- **No network calls.** Preflight tests mock `urllib.request.urlopen`.
- **No file system side effects in production paths.** Tests that write files
  use `tmp_path` fixtures or patch `tools.wizard.LOG_DIR`.
- **Async tests** use `anyio.run()` directly (not `pytest-asyncio`) to avoid
  requiring an additional dev dependency.

### Adding a new test

```python
# tests/test_my_module.py

from __future__ import annotations
from unittest.mock import patch
import pytest
from tools.my_module import my_function


class TestMyFunction:
    """Group related tests in a class."""

    def test_happy_path(self) -> None:
        result = my_function("input")
        assert result == "expected"

    def test_failure_mode(self) -> None:
        with pytest.raises(ValueError, match="specific message"):
            my_function("bad_input")

    def test_with_mock(self) -> None:
        with patch("tools.my_module.some_dependency") as mock_dep:
            mock_dep.return_value = "mocked"
            result = my_function("input")
        assert result == "processed mocked"
```

---

## 7. Linting and formatting

The project uses **ruff** for linting. No formatter is enforced (the code
uses consistent manual formatting following PEP 8).

```bash
# Lint all Python files
python -m ruff check tools/ tests/

# Lint a specific file
python -m ruff check tools/wizard.py

# Fix auto-fixable issues (unused imports, etc.)
python -m ruff check tools/ --fix

# Check with verbose output
python -m ruff check tools/ --show-source
```

### Rules enforced

Ruff is configured by `pyproject.toml`. Currently no custom config is set —
default rules apply. Key violations to avoid:

- `F401` — unused imports
- `E501` — line too long (default 88 chars)
- `F811` — redefinition of unused name
- `N802` — function name should be lowercase (used with `# noqa: N802` for
  `do_GET`/`do_POST` which must match Python's HTTP server convention)
- `PLC0415` — imports not at top of file (used with `# noqa` for deferred
  imports that are intentional — see `guardrail_enforcer.py`)

### Pre-commit check

Before every commit, run:

```bash
python -m ruff check tools/ tests/ && pytest tests/ -v --tb=short
```

Both must pass with zero errors.

---

## 8. Working with the LLM backend

### llama.cpp server startup

```bash
# Basic startup (replace with your model path)
llama-server --model /path/to/model.gguf --port 8012 --ctx-size 4096 --n-gpu-layers 99

# Or using the llama wrapper command
llama serve --port 8012

# Verify it is running
curl http://127.0.0.1:8012/v1/models
# Should return: {"object":"list","data":[{"id":"model-name",...}]}
```

### Recommended models

| Role | Recommended model | Size | Notes |
|---|---|---|---|
| Developer | Qwen2.5-Coder-7B-Q4_K_M | ~4.5GB | Best code generation for the size |
| Developer | Qwen2.5-Coder-3B-Q8_0 | ~3.2GB | Minimum viable; may need more retries |
| Reviewer | Llama-3.1-8B-Q4_K_M | ~4.7GB | Good general reasoning |
| Security | Phi-3-mini-Q4_K | ~2.2GB | Fast; adequate for pattern detection |
| Validator | Any 3B+ model | — | Simple yes/no task, small models work |

### Using a single model for all roles

If you only have one model loaded, set `mode: auto` — `ModelManager` will
assign the same model to all four roles. This works fine; the differences
between roles are in the system prompt, not the model weights.

### Changing the server port

1. Update `.env`: `LLAMA_HOST=http://127.0.0.1:<new_port>`
2. Or update `.agentconfig.yaml`: `llama.host: "http://127.0.0.1:<new_port>"`
3. Restart your llama.cpp server on the new port.

### Increasing timeout for large models

Larger models (13B+) can take 60–180 seconds to generate a response. Set:

```bash
# .env
LLAMA_TIMEOUT=300
```

---

## 9. Modifying agent instructions

The system prompts are plain Markdown files in `agent_instructions/`. They are
read at runtime — no rebuild needed.

### Guidelines

- **Keep the output schema section exact.** The code parses specific JSON
  keys. If you change the schema in the instructions, update the parsing code
  in the corresponding module.
- **Be explicit about "no other text".** Small models tend to add preamble
  unless instructed otherwise.
- **Test your changes** by running a full pipeline after editing — LLM
  behaviour is sensitive to prompt wording.
- **Keep instructions < 2000 tokens** for the user prompt + instructions
  combined to avoid context window issues with 3B models.

### Example: adding a new security pattern to the Security Guard

1. Edit `agent_instructions/security_guard.md` — add the pattern to the table.
2. Edit `guardrails/forbidden_actions.yaml` — add the static scan pattern.
3. Run `python -m tools.agent_runner example_feature` to verify nothing breaks.

---

## 10. Modifying guardrail rules

### Static scan patterns (`guardrails/forbidden_actions.yaml`)

Add a new forbidden pattern:

```yaml
forbidden:
  # ... existing patterns ...
  - "your_new_pattern"   # optional comment
```

The static scanner does a plain string `in` check per line — patterns are not
regex. YAML comment annotations like `"(unless in allowed list)"` are stripped
(everything after `(` is ignored).

### Permission boundaries (`guardrails/permission_boundaries.json`)

```json
{
  "read_permitted":  ["specs/", "guardrails/", "agent_instructions/"],
  "write_permitted": ["generated/"],
  "network_access":  false,
  "allowed_imports": ["math", "json", ...],
  "max_file_size_kb": 512
}
```

This file is referenced in agent instructions but is not currently enforced
programmatically (the static scan is the enforcement mechanism). Future
versions may use it for automated boundary checking.

### Disabling enforcement (dev only)

```yaml
# .agentconfig.yaml
guardrails:
  enforce: false
```

With `enforce: false`, violations are logged as warnings but do not fail the
pipeline. **Never use this in production** — it exists only for debugging
guardrail false positives.

---

## 11. Environment variables reference

| Variable | Default | Description |
|---|---|---|
| `LLAMA_HOST` | `http://127.0.0.1:8012` | llama.cpp server base URL |
| `LLAMA_TIMEOUT` | `120` | Seconds before an LLM call times out |
| `LLAMA_DEVELOPER_MODEL` | *(auto)* | Pin developer role to this model name |
| `LLAMA_REVIEWER_MODEL` | *(auto)* | Pin reviewer role to this model name |
| `LLAMA_SECURITY_MODEL` | *(auto)* | Pin security role to this model name |
| `LLAMA_VALIDATOR_MODEL` | *(auto)* | Pin validator role to this model name |
| `LOG_LEVEL` | `INFO` | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `NO_COLOR` | *(unset)* | Set to any value to disable ANSI colour output |

Environment variables take highest priority over `.agentconfig.yaml` values.
Copy `.env.example` to `.env` and edit; never commit `.env` (it is gitignored).

---

## 12. Common development tasks

### Add a new feature spec and run it

```bash
mkdir specs/my_feature
cp specs/template.prd.md   specs/my_feature/prd.md
cp specs/template.tdd.yaml specs/my_feature/tdd.yaml

# Edit the spec files
$EDITOR specs/my_feature/prd.md specs/my_feature/tdd.yaml

# Run the pipeline
python -m tools.agent_runner my_feature --wizard
```

### Inspect the last pipeline run

```bash
# View the run log
tail -1 logs/pipeline_runs.jsonl | python -m json.tool

# View agent_runner.log
tail -50 logs/agent_runner.log
```

### Re-run the test suite after making changes

```bash
python -m ruff check tools/ tests/ && pytest tests/ -v
```

### Reset generated output for a feature

```bash
rm -rf generated/my_feature/
# Next pipeline run will regenerate it from scratch
```

### Inspect generated code

```bash
# After a successful run
cat generated/example_feature/src/converter.py
cat generated/example_feature/tests/test_converter.py

# Run the generated tests directly
pytest generated/example_feature/tests/ -v
```

### Check which model is being used

```bash
LOG_LEVEL=DEBUG python -m tools.agent_runner example_feature 2>&1 | grep ModelManager
# Output: [ModelManager] developer → ggml-org/Qwen2.5-Coder-3B-Q8_0-GGUF
```

---

## 13. CI checklist

Before merging any change to the pipeline:

```bash
# 1. Ruff lint — must be zero errors
python -m ruff check tools/ tests/

# 2. Unit tests — must all pass
pytest tests/ -v --tb=short

# 3. Example feature — must APPROVE
python -m tools.agent_runner example_feature --skip-preflight

# 4. Manual check: no new files in generated/ or logs/ committed
git status
```

For changes to agent instructions or guardrail rules, also run:

```bash
# Verify the changed instructions work end-to-end
python -m tools.agent_runner example_feature
# Verify the guardrail scanner still catches a known pattern
python -c "
from tools.guardrail_enforcer import static_scan
# Should be empty (example_feature passes guardrails)
print(static_scan('example_feature'))
"
```
