# Agentic Dev Workflow

A fully local, spec-driven AI development loop. Write a PRD and TDD plan — the
pipeline generates, tests, security-scans, and reviews the code automatically.
Everything runs on your machine via a local llama.cpp server. No cloud. No mocks.

```
specs/<feature>/prd.md  +  tdd.yaml
        │
        ▼
  ┌─────────────────┐
  │  Spec Validator │  LLM checks every PRD acceptance criterion
  └────────┬────────┘  is covered by at least one TDD test case
           │
           ▼
  ┌─────────────────┐
  │ Developer Agent │  LLM generates src/ + tests/ from the specs
  └────────┬────────┘
           │
           ▼
  ┌──────────────────────┐
  │  Guardrail Enforcer  │  Static pattern scan + LLM semantic scan
  └────────┬─────────────┘
           │
           ▼
  ┌─────────────────┐
  │ Reviewer Agent  │  Runs pytest, measures coverage, LLM verdict
  └────────┬────────┘
           │
     PASS? ┼─── YES ──▶  APPROVED ✅  →  generated/<feature>/
           │
          NO
           │
     attempt < max_retries ──▶ inject feedback, retry from Developer Agent
           │
          NO
           ▼
        REJECTED ✗
```

---

## Table of contents

1. [Requirements](#1-requirements)
2. [Installation](#2-installation)
3. [Quick start — CLI](#3-quick-start--cli)
4. [Quick start — GUI](#4-quick-start--gui)
5. [Configuration reference](#5-configuration-reference)
6. [Writing a feature spec](#6-writing-a-feature-spec)
7. [Running the pipeline](#7-running-the-pipeline)
8. [Wizard mode](#8-wizard-mode)
9. [Pre-flight checks](#9-pre-flight-checks)
10. [Test suite](#10-test-suite)
11. [Project layout](#11-project-layout)
12. [Pass/fail gates](#12-passfail-gates)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Requirements

| Dependency | Minimum | Notes |
|---|---|---|
| Python | 3.11 | 3.12+ recommended |
| llama.cpp | any recent build | needs `/v1/chat/completions` endpoint |
| A GGUF model | ≥ 3B params | Qwen2.5-Coder-7B-Q4 recommended |
| pip packages | see `requirements.txt` | install into a venv |

llama.cpp must expose an **OpenAI-compatible** HTTP API (`/v1/models`,
`/v1/chat/completions`). The official llama.cpp binary does this when started
with the `--server` flag or the `llama serve` wrapper.

---

## 2. Installation

```bash
# Clone (or cd into the repo)
cd agentic-dev-workflow

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy the environment template
cp .env.example .env
# Edit .env — at minimum set LLAMA_HOST if your server is not on port 8012
```

---

## 3. Quick start — CLI

```bash
# 1. Start your llama.cpp server (separate terminal)
llama serve --fim-qwen-3b-default -ngl 99 --port 8012

# 2. Run the included example feature (temperature converter)
python -m tools.agent_runner example_feature

# 3. Watch the pipeline run — output lands in generated/example_feature/
```

Expected terminal output:

```
── Pre-flight checks ─────────────────────────────────────
  ✓  llama.cpp server reachable           OK
  ✓  model available                      OK
  ✓  Python dependencies                  OK
  ✓  output directory writable            OK

══════════════════════════════════════════
  Agentic Dev Workflow  │  Feature: example_feature
══════════════════════════════════════════
─── Step 0 — Spec validation ────────────
✓ Specs are consistent

─── Attempt 1 / 3 ───────────────────────
▶ Developer Agent — generating code
✓ Generated 2 file(s)

▶ Guardrail Enforcer — security scan
✓ Guardrails passed

▶ Reviewer Agent — running tests + LLM review
  PRD   ✅ PASS
  TDD   ✅ PASS
  GUARD ✅ PASS

╔══════════════════════════════╗
║  Feature 'example_feature'   ║
║  APPROVED ✅   Elapsed: 92s  ║
╚══════════════════════════════╝
```

---

## 4. Quick start — GUI

The GUI server exposes every pipeline step as a point-and-click interface with
live streaming output. It runs entirely in your browser against a local server —
no internet connection required.

```bash
# Start the GUI (opens browser automatically)
python -m tools.gui_server

# Or specify a port / suppress browser open
python -m tools.gui_server --port 8099 --no-browser
```

Then navigate to **http://127.0.0.1:8099**.

The GUI provides six steps:

| Step | Panel | What it does |
|---|---|---|
| 0 | Pre-flight Checks | Runs all four health checks against your live environment |
| 1 | Configuration | Edit `.agentconfig.yaml` and `.env` in-browser; saves to disk |
| 2 | Feature Spec | Create or edit `prd.md` + `tdd.yaml` with syntax-highlighted editors |
| 3 | Run Pipeline | Launch the real pipeline; streams live output via Server-Sent Events |
| 4 | Run History | Browse `logs/pipeline_runs.jsonl` — every run's status and failures |
| 5 | Log Viewer | Tail `logs/agent_runner.log` in-browser |

---

## 5. Configuration reference

### `.agentconfig.yaml`

```yaml
llama:
  host: "http://127.0.0.1:8012"   # llama.cpp server URL
  timeout: 120                     # seconds per LLM call
  mode: auto                       # "auto" | "manual"
  models:                          # used when mode: manual
    developer: "ggml-org/Qwen2.5-Coder-3B-Q8_0-GGUF"
    reviewer:  "ggml-org/Qwen2.5-Coder-3B-Q8_0-GGUF"
    security:  "ggml-org/Qwen2.5-Coder-3B-Q8_0-GGUF"
    validator: "ggml-org/Qwen2.5-Coder-3B-Q8_0-GGUF"
  hints:                           # keyword scoring for auto mode
    developer: [coder, code, qwen, deepseek, llama]
    reviewer:  [llama, mistral, gemma, qwen]
    security:  [phi, gemma, qwen, llama]
    validator: [llama, mistral, qwen]

max_retries: 3          # pipeline retry attempts before REJECTED
retry_delay: 2          # seconds between retries

paths:
  specs:   specs/       # where prd.md / tdd.yaml live
  output:  generated/   # where generated code is written
  logs:    logs/        # agent_runner.log + pipeline_runs.jsonl

guardrails:
  enforce: true                             # false = warn-only (dev only)
  forbidden_actions_file: guardrails/forbidden_actions.yaml
  permissions_file:       guardrails/permission_boundaries.json
  allow_network: false

review:
  coverage_threshold: 80   # minimum % coverage to pass TDD gate
```

### `.env` overrides

Environment variables always win over YAML values.

```bash
LLAMA_HOST=http://127.0.0.1:8012
LLAMA_TIMEOUT=120
LLAMA_DEVELOPER_MODEL=          # leave blank for auto
LLAMA_REVIEWER_MODEL=
LLAMA_SECURITY_MODEL=
LLAMA_VALIDATOR_MODEL=
LOG_LEVEL=INFO                  # DEBUG | INFO | WARNING | ERROR
```

### Model auto-selection

When `mode: auto`, `ModelManager` queries `/v1/models`, scores each model name
against the keyword hints for the role, and breaks ties by parameter count
embedded in the name (`7b` > `3b`). The highest-scoring model wins.

Override for a single role without switching to full manual mode:

```bash
LLAMA_DEVELOPER_MODEL=my-custom-coder-7b   # only developer is pinned
```

---

## 6. Writing a feature spec

Every feature needs two files under `specs/<feature_name>/`:

### `prd.md` — Product Requirements Document

```markdown
# PRD: My Feature

## Overview
One paragraph explaining what the feature does and why.

## Functional Requirements
- **FR-01**: The module exposes a function `add(a, b)` returning a + b.

## Acceptance Criteria
<!-- IMPORTANT: number every criterion — the Spec Validator and Reviewer
     Agent both reference these numbers. -->
1. Given integers `a=2, b=3`, when `add(2, 3)` is called, the result is `5`.
2. Given floats, when `add(1.5, 2.5)` is called, the result is `4.0`.
3. Given a non-numeric argument, when `add("x", 1)` is called, a `TypeError` is raised.

## Constraints
- Pure Python standard library only.
- Functions must be pure (no I/O, no side effects).
```

### `tdd.yaml` — Test-Driven Design plan

```yaml
tests:
  - id: TC-01
    name: "Integer addition"
    description: "add(2, 3) returns 5."
    inputs:
      a: 2
      b: 3
    expected_result: 5
    expected_exception: ~

  - id: TC-02
    name: "Float addition"
    description: "add(1.5, 2.5) returns 4.0."
    inputs:
      a: 1.5
      b: 2.5
    expected_result: 4.0
    expected_exception: ~

  - id: TC-03
    name: "Non-numeric raises TypeError"
    description: "add('x', 1) raises TypeError."
    inputs:
      a: "x"
      b: 1
    expected_result: ~
    expected_exception: TypeError
```

**Rules for TDD files:**

- Every `id` must be unique.
- `expected_exception` must be `~` (null) if no exception is expected, or a
  Python exception class name (`ValueError`, `TypeError`, etc.).
- `expected_result` must be `~` when `expected_exception` is set.
- The Spec Validator checks that each numbered acceptance criterion in the PRD
  has a corresponding test case by name — they do not need to match exactly,
  but there must be a semantic match.

Start from the templates:

```bash
mkdir specs/my_feature
cp specs/template.prd.md   specs/my_feature/prd.md
cp specs/template.tdd.yaml specs/my_feature/tdd.yaml
$EDITOR specs/my_feature/prd.md specs/my_feature/tdd.yaml
```

---

## 7. Running the pipeline

### CLI

```bash
# Basic run
python -m tools.agent_runner <feature_name>

# With Wizard diagnostics on failure
python -m tools.agent_runner <feature_name> --wizard

# Skip pre-flight (CI / offline)
python -m tools.agent_runner <feature_name> --skip-preflight

# Combine
python -m tools.agent_runner <feature_name> --wizard --skip-preflight
```

### What gets written

After a successful run, `generated/<feature_name>/` contains:

```
generated/my_feature/
├── conftest.py          ← auto-inserted sys.path fix for pytest
├── src/
│   └── module.py        ← generated source code
└── tests/
    └── test_module.py   ← generated pytest test file
```

The pipeline wipes and rewrites `generated/<feature_name>/` on every attempt,
so there are no stale artefacts between retries.

### Run log

Every run (success or failure) is appended to `logs/pipeline_runs.jsonl`:

```jsonc
{
  "timestamp": "2026-07-20T12:00:00+00:00",
  "feature_name": "my_feature",
  "stages_completed": ["spec_validation", "code_generation:attempt_1", ...],
  "failures": [],          // list of FailureEvent dicts
  "final_status": "APPROVED"  // or "REJECTED" | "ABORTED"
}
```

---

## 8. Wizard mode

`--wizard` enables the self-healing diagnostic layer. On any pipeline failure
the Wizard:

1. Captures the exception type, message, truncated stack trace, and raw LLM
   response.
2. Runs a deterministic rule-based analyser that returns a `Diagnosis` with a
   likely cause, confidence score, and ordered fix suggestions.
3. Prints a formatted diagnostic card to the terminal.
4. If the failure is auto-fixable (e.g. truncated JSON), prompts to apply the
   fix automatically.
5. Offers a numbered menu: retry stage / skip stage / abort / show full trace.

```
╔══ 🧙 WIZARD DIAGNOSTIC ════════════════════════════════╗
║ Stage      : code_generation
║ Error      : JSONDecodeError — Expecting ',' delimiter
║ Likely cause: LLM response truncated — JSON malformed (90%)
║ Auto-fix   : Yes
╠══ Suggested fixes ════════════════════════════════════╣
║  1. Use parse_llm_json() — repairs truncated output.
║  2. Increase max_tokens (currently 4096).
║  3. Use a larger model (≥7B) for more reliable JSON.
╚════════════════════════════════════════════════════════╝

Apply fix automatically? [y/N]
```

Set `NO_COLOR=1` to disable ANSI colour output (plain ASCII only).

---

## 9. Pre-flight checks

Run automatically before every pipeline execution (skip with `--skip-preflight`).

| Check | What it verifies | Severity |
|---|---|---|
| Server reachable | `GET LLAMA_HOST/v1/models` returns HTTP 200 | error |
| Model available | Response contains at least one model entry | error |
| Dependencies | `openai`, `yaml`, `dotenv`, `rich`, `pytest` importable | error |
| Output writable | `generated/` can be created and written to | error |

All failures produce a `FailureEvent` that feeds into the Wizard diagnostic
layer. In `--wizard` mode, pre-flight failures are captured but do not abort
the pipeline immediately — they are reported at the end.

Run checks manually at any time:

```python
from tools.preflight import run_preflight_checks
failures = run_preflight_checks(abort_on_error=False)
```

---

## 10. Test suite

```bash
# Run all tests
pytest tests/ -v

# Run with coverage report
pytest tests/ -v --cov=tools --cov-report=term-missing

# Run a specific test class
pytest tests/test_wizard.py::TestAnalyse -v
```

The test suite covers:

| Module | Test file | Coverage areas |
|---|---|---|
| `wizard.py` | `tests/test_wizard.py` | `FailureEvent`, `analyse()` rules, `append_run_log()`, `pipeline_stage()`, diagnostic card |
| `preflight.py` | `tests/test_preflight.py` | all four checks, `run_preflight_checks()` |

Tests use only `unittest.mock` and `anyio` (already in the venv). No network
calls are made by the test suite.

---

## 11. Project layout

```
agentic-dev-workflow/
├── .agentconfig.yaml          ← master configuration
├── .env.example               ← environment variable template
├── .gitignore
├── pyproject.toml
├── requirements.txt
│
├── agent_instructions/        ← system prompts for each LLM role
│   ├── developer_agent.md
│   ├── reviewer_agent.md
│   └── security_guard.md
│
├── guardrails/
│   ├── forbidden_actions.yaml ← static scan patterns
│   ├── permission_boundaries.json
│   └── compliance_checklist.md
│
├── specs/                     ← feature specifications (tracked in git)
│   ├── template.prd.md
│   ├── template.tdd.yaml
│   └── <feature_name>/
│       ├── prd.md
│       └── tdd.yaml
│
├── generated/                 ← runtime output (gitignored)
│   └── <feature_name>/
│       ├── conftest.py
│       ├── src/
│       └── tests/
│
├── logs/                      ← runtime logs (gitignored)
│   ├── agent_runner.log
│   └── pipeline_runs.jsonl
│
├── tests/                     ← unit tests for the tools package
│   ├── conftest.py
│   ├── test_wizard.py
│   └── test_preflight.py
│
└── tools/                     ← the pipeline implementation
    ├── __init__.py
    ├── agent_runner.py        ← CLI entry-point / orchestrator
    ├── config.py              ← config loader
    ├── developer_agent.py     ← code generation
    ├── guardrail_enforcer.py  ← security scanning
    ├── gui.html               ← GUI front-end (served by gui_server.py)
    ├── gui_server.py          ← GUI HTTP+SSE server
    ├── llm_client.py          ← async LLM wrapper
    ├── llm_json.py            ← JSON extraction + repair for small models
    ├── model_manager.py       ← model discovery and role assignment
    ├── preflight.py           ← pre-flight health checks
    ├── reviewer_agent.py      ← test runner + LLM verdict
    ├── spec_validator.py      ← PRD ↔ TDD consistency check
    └── wizard.py              ← failure diagnosis and run logging
```

---

## 12. Pass/fail gates

| Gate | Condition | Source of truth |
|---|---|---|
| Spec validation | All PRD acceptance criteria covered by TDD tests | `spec_validator.py` LLM call |
| Code generation | LLM returns valid JSON with non-empty `files` list | `developer_agent.py` |
| Static guardrail | Zero forbidden patterns across all `.py` files | `guardrail_enforcer.py` regex scan |
| Semantic guardrail | LLM Security Guard returns `"safe": true` | `guardrail_enforcer.py` LLM call |
| Tests pass | `pytest` exit code = 0 | `reviewer_agent.py` subprocess |
| Coverage | `TOTAL … N%` ≥ `coverage_threshold` in pytest output | `reviewer_agent.py` regex |
| PRD review | LLM Reviewer returns `prd_check: PASS` | `reviewer_agent.py` LLM call |

The pytest and coverage gates are **hard overrides** — they are applied on top
of the LLM verdict regardless of what the model says.

---

## 13. Troubleshooting

See [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) for the full guide.

Quick reference:

| Symptom | Fix |
|---|---|
| `Cannot reach llama.cpp server` | Start: `llama serve --port 8012` |
| `No models loaded` | Add a model to your llama.cpp server startup command |
| `JSONDecodeError` in any stage | Run with `--wizard`; auto-fix available |
| `Coverage 0%` | Ensure generated `conftest.py` adds `src/` to `sys.path` |
| `Guardrail: forbidden pattern` | Add `do NOT use <pattern>` to `agent_instructions/developer_agent.md` |
| Port 8099 already in use | `lsof -ti tcp:8099 \| xargs kill -9` |
