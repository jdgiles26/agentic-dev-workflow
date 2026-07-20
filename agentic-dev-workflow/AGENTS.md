# AGENTS.md — Agent roles, system prompts, data contracts, and decision trees

This document describes every LLM agent in the pipeline: its role, the exact
system prompt it receives, what JSON it must produce, and how its output is
validated and used.

---

## Table of contents

1. [Agent overview](#1-agent-overview)
2. [Spec Validator](#2-spec-validator)
3. [Developer Agent](#3-developer-agent)
4. [Security Guard Agent](#4-security-guard-agent)
5. [Reviewer Agent](#5-reviewer-agent)
6. [Model selection and assignment](#6-model-selection-and-assignment)
7. [LLM client details](#7-llm-client-details)
8. [JSON repair strategy](#8-json-repair-strategy)
9. [Wizard diagnostic agent](#9-wizard-diagnostic-agent)
10. [Adding a new agent](#10-adding-a-new-agent)

---

## 1. Agent overview

| Agent | Module | Role | Calls LLM? |
|---|---|---|---|
| Spec Validator | `tools/spec_validator.py` | Checks PRD ↔ TDD consistency | Yes |
| Developer Agent | `tools/developer_agent.py` | Generates `src/` + `tests/` | Yes |
| Security Guard | `tools/guardrail_enforcer.py` | Semantic security scan | Yes |
| Reviewer Agent | `tools/reviewer_agent.py` | Test runner + code review verdict | Yes |
| Wizard | `tools/wizard.py` | Failure diagnosis + run logging | No — rule-based only |
| Preflight | `tools/preflight.py` | Environment health checks | No — HTTP + imports only |

All LLM agents share the same async `LLMClient` wrapper. Each is bound to a
**role** string (`"validator"`, `"developer"`, `"security"`, `"reviewer"`) that
`ModelManager` maps to the best available loaded model.

---

## 2. Spec Validator

**File:** `tools/spec_validator.py`
**Role string:** `"validator"`
**Triggered by:** `agent_runner.py` — Step 0, once per pipeline run

### Purpose

Verify that every numbered acceptance criterion in `prd.md` is semantically
covered by at least one test case in `tdd.yaml`. This prevents the pipeline
from generating code for under-specified or untestable requirements.

### Two-strategy approach

Small models (3B) frequently fail to produce valid JSON. The validator uses
a fallback chain:

```
Strategy 1: Ask for JSON response
  → parse with parse_llm_json()
  → if parsed: use result
  → if not parseable: fall through

Strategy 2: Ask yes/no question with shorter prompt
  → regex-search for \byes\b or \bno\b
  → if neither found: assume consistent (log warning, unblock pipeline)
```

### Strategy 1 — system prompt

```
You are a specification auditor.
Check whether every numbered acceptance criterion in the PRD is covered
by at least one test case in the TDD.
Reply with ONLY this JSON object, nothing else:
{"consistent":true,"missing":[]}
or if items are missing:
{"consistent":false,"missing":["criterion 1","criterion 2"]}
```

### Strategy 1 — user prompt structure

```
PRD:
<full contents of prd.md>

TDD:
<full contents of tdd.yaml>

Does the TDD cover all PRD acceptance criteria?
Reply ONLY with {"consistent":true,"missing":[]} or list the missing ones.
```

### Strategy 2 — system prompt

```
You are a specification auditor. Answer with a single word: yes or no.
```

### Strategy 2 — user prompt structure

```
PRD acceptance criteria:
<numbered criteria extracted from ## Acceptance Criteria section>

TDD test names:
<list of name: "..." values from tdd.yaml>

Does every acceptance criterion have a matching test? Answer yes or no.
```

### Expected output — Strategy 1

```json
{"consistent": true, "missing": []}
```
or:
```json
{"consistent": false, "missing": ["criterion 3", "criterion 5"]}
```

### Decision logic

| Result | Action |
|---|---|
| `consistent: true, missing: []` | Proceed to Developer Agent |
| `consistent: false, missing: [...]` | Print missing items, return `False`, pipeline aborted |
| `consistent: true, missing: [...]` | Trust the `missing` list (model contradicted itself), return `False` |
| `consistent: false, missing: []` | Ambiguous — fall through to Strategy 2 |
| JSON unparseable | Fall through to Strategy 2 |
| Strategy 2 → "yes" | Proceed to Developer Agent |
| Strategy 2 → "no" | Return `False`, pipeline aborted |
| Strategy 2 → neither | Assume consistent, log `WARNING`, proceed |

---

## 3. Developer Agent

**File:** `tools/developer_agent.py`
**Role string:** `"developer"`
**Triggered by:** `agent_runner.py` — Step 1, once per retry attempt

### Purpose

Generate production-quality Python source code and a matching pytest test suite
from the PRD and TDD plan. Output is written to `generated/<feature_name>/`.

### System prompt (verbatim from `agent_instructions/developer_agent.md`)

```
You are an expert software engineer. Your sole job is to implement a feature
exactly as described by the PRD and TDD plan provided below.

Steps you MUST follow:
1. Parse the PRD and extract every functional requirement and acceptance criterion.
2. Read the TDD plan — each test case has an id, name, expected inputs, and
   expected outputs or exception types.
3. Generate the minimum source code that satisfies all requirements.
4. Generate a pytest test file that covers every test case in the TDD plan exactly.
5. Make sure all tests would pass against your implementation before responding.

Hard constraints:
- Use ONLY the Python standard library unless the PRD explicitly lists allowed packages.
- Never call os.remove, os.system, subprocess, eval, exec, or any network API.
- All file writes must go to paths inside generated/<feature>/.
- No hardcoded secrets, passwords, or API keys.

Output format:
Respond with ONLY a valid JSON object — no markdown fences, no extra text.
```

### User prompt structure

```
<full contents of agent_instructions/developer_agent.md>

──────────────────────────────────────────────
PRD:
```
<full contents of prd.md>
```

TDD:
```
<full contents of tdd.yaml>
```

Now generate the implementation. Remember: respond with ONLY valid JSON.
```

### Required output schema

```json
{
  "files": [
    {
      "path": "src/module.py",
      "content": "<complete Python source>"
    },
    {
      "path": "tests/test_module.py",
      "content": "<complete pytest test source>"
    }
  ],
  "test_command": "pytest tests/ -v --cov=src --cov-report=term"
}
```

All `path` values are relative to `generated/<feature_name>/`.

### Validation

After parsing, `_parse_response()` enforces:
- `"files"` key exists and is a list
- The list is non-empty
- Each entry has `"path"` and `"content"` string fields

`KeyError` or an empty files list raises `ValueError` and triggers a retry.

### File writing

`write_generated_files()` wipes `generated/<feature_name>/` with
`shutil.rmtree` before writing, ensuring clean retries. It then writes each
file and injects a `conftest.py` that adds the feature root to `sys.path` so
pytest can find `src/`.

---

## 4. Security Guard Agent

**File:** `tools/guardrail_enforcer.py`
**Role string:** `"security"`
**Triggered by:** `agent_runner.py` — Step 2 (semantic scan only; static scan is deterministic)

### Two-layer architecture

#### Layer 1 — Static scan (no LLM)

Reads every `.py` file under `generated/<feature_name>/`, searches each line
for patterns from `guardrails/forbidden_actions.yaml`. A match is a
`Violation(file, line, pattern, detail)`.

Current forbidden patterns (from `guardrails/forbidden_actions.yaml`):

| Category | Patterns |
|---|---|
| Filesystem destruction | `os.remove`, `os.unlink`, `os.rmdir`, `shutil.rmtree`, `pathlib.Path.unlink`, `pathlib.Path.rmdir` |
| Shell execution | `os.system`, `os.popen`, `subprocess.call`, `subprocess.Popen`, `subprocess.run`, `subprocess.check_output` |
| Dynamic execution | `eval(`, `exec(`, `__import__(`, `importlib.import_module`, `compile(` |
| Deserialization | `pickle.loads`, `pickle.load`, `marshal.loads`, `yaml.load(` |
| Network access | `import requests`, `from requests`, `urllib.request`, `import socket`, `import http.client`, `import ftplib`, `import smtplib`, `import ssl` |

If any violation is found **and** `guardrails.enforce: true`, the pipeline
fails and retries. With `enforce: false`, violations are logged as warnings
and the pipeline continues (useful for local debugging only).

#### Layer 2 — Semantic scan (LLM)

Sends all generated code as a single prompt to the Security Guard LLM.
Catches obfuscated patterns the static scan cannot detect (e.g. indirect
`eval`, aliased `subprocess`, string-constructed imports).

**System prompt:**

```
You are a security sentinel performing static analysis on Python code.
[see agent_instructions/security_guard.md for full content]
```

**User prompt structure:**

```
<full contents of agent_instructions/security_guard.md>

──────────────────────────────────────────────
Code to audit:

### File: src/module.py
<file contents>

### File: tests/test_module.py
<file contents>

Return ONLY the JSON verdict.
```

**Required output schema:**

```json
{
  "safe": true,
  "violations": []
}
```
or:
```json
{
  "safe": false,
  "violations": [
    {
      "file": "src/module.py",
      "line": 12,
      "pattern": "os.system",
      "detail": "Direct shell execution is forbidden."
    }
  ]
}
```

If the LLM produces unparseable JSON, the semantic scan **passes through**
(assumes safe) and logs a warning. The static scan remains the authoritative
gate.

---

## 5. Reviewer Agent

**File:** `tools/reviewer_agent.py`
**Role string:** `"reviewer"`
**Triggered by:** `agent_runner.py` — Step 3, once per retry attempt

### Purpose

1. Run the generated test suite with `pytest` in a subprocess.
2. Extract coverage percentage from pytest output.
3. Send all code, specs, and test results to the Reviewer LLM for a verdict.
4. Apply hard overrides based on actual pytest results.

### Test runner

```python
cmd = [sys.executable, "-m", "pytest",
       str(tests_dir), "-v", "--tb=short",
       f"--cov={src_dir}", "--cov-report=term-missing", "--no-header"]
```

Coverage is extracted from the `TOTAL   N   N   N%` line in pytest output
with the regex `r"TOTAL\s+\d+\s+\d+\s+(\d+)%"`.

### System prompt

```
You are a strict code reviewer. Follow the instructions exactly.
```

### User prompt structure

```
<full contents of agent_instructions/reviewer_agent.md>

──────────────────────────────────────────────
## PRD
<full prd.md>

## TDD
<full tdd.yaml>

## Source files
### src/module.py
```python
<source>
```

## Test files
### tests/test_module.py
```python
<tests>
```

## pytest output
```
<first 3000 chars of stdout+stderr>
```

## Coverage: N%  (threshold: 80%)
## All tests passed: True/False

Now produce the JSON verdict. ONLY the JSON object — no other text.
```

### Required output schema

```json
{
  "overall":         "PASS",
  "prd_check":       "PASS",
  "tdd_check":       "PASS",
  "guardrail_check": "PASS",
  "feedback":        "All acceptance criteria met. Tests comprehensive."
}
```

### Hard overrides (applied after LLM verdict)

| Condition | Override |
|---|---|
| `pytest` exit code ≠ 0 | `tdd_check = "FAIL"`, append to feedback |
| `coverage_pct < coverage_threshold` | `tdd_check = "FAIL"`, append to feedback |
| Any sub-check is `"FAIL"` | `overall = "FAIL"` |

The hard overrides ensure that a hallucinating LLM cannot approve failing tests.

### Fallback on JSON parse failure

If `parse_llm_json()` raises, the verdict defaults to all-PASS, then the hard
overrides are applied. This means a small model that cannot produce valid JSON
still gets the correct pytest/coverage result.

---

## 6. Model selection and assignment

**File:** `tools/model_manager.py`

### Auto mode

1. `ModelManager._refresh()` — HTTP GET to `LLAMA_HOST/v1/models`.
2. Response must be `{"data": [{"id": "..."}]}` (OpenAI format) or
   `{"models": [{"name": "..."}]}` (some llama.cpp versions).
3. `ModelManager._pick(role)` — scores every model against `hints` list for
   the role. Earlier hints carry more weight (`score += 10.0 - idx * 0.5`).
   Tie-breaking: parameter count in name (`7b` adds 0.7, capped at 3.0).
4. Result is cached per role for the lifetime of the process.

### Manual mode

If `LLAMA_MODE = "manual"` (from YAML), the configured model name is used
directly if it appears in the available list. If not, falls back to auto.

### Environment variable overrides

`LLAMA_DEVELOPER_MODEL`, `LLAMA_REVIEWER_MODEL`, `LLAMA_SECURITY_MODEL`,
`LLAMA_VALIDATOR_MODEL` — set in `.env`. Highest priority. Bypass
`ModelManager` entirely for the pinned role.

---

## 7. LLM client details

**File:** `tools/llm_client.py`

Uses the official `openai` Python SDK pointed at the local llama.cpp server.
llama.cpp ignores the API key; a dummy `"local"` string is passed to satisfy
the SDK's non-empty requirement.

```python
client = AsyncOpenAI(
    api_key="local",
    base_url=f"{LLAMA_HOST}/v1",
)
response = await asyncio.wait_for(
    client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.15,
        max_tokens=4096,
    ),
    timeout=LLAMA_TIMEOUT,
)
```

`temperature=0.15` is intentionally low — agents need deterministic,
structured output, not creative generation.

If the call exceeds `LLAMA_TIMEOUT` seconds, `asyncio.TimeoutError` is raised.
The Wizard diagnostic layer catches this and provides `"start server"` remediation.

---

## 8. JSON repair strategy

**File:** `tools/llm_json.py`

Small models frequently truncate their output at `max_tokens`, producing broken
JSON. `parse_llm_json(raw, context)` attempts three repair strategies in order:

1. **Direct parse** after stripping markdown fences (` ```json ... ``` `).
2. **Extract outermost `{...}`** — find first `{` and last `}`, parse the
   slice.
3. **Truncation repair** — walk the fragment tracking string/brace/bracket
   state. If truncated mid-string inside a nested object, trim back to the last
   safe boundary (post-comma or post-opening-bracket), then append the minimum
   closing characters `]...}...`.

If all three attempts fail, `ValueError` is raised with context information.
The calling agent either retries or falls back to a default safe value.

---

## 9. Wizard diagnostic agent

**File:** `tools/wizard.py`
**Does NOT call LLM** — entirely deterministic rule-based system.

### FailureEvent schema

```python
@dataclass
class FailureEvent:
    stage: str            # e.g. "code_generation"
    exc_type: str         # e.g. "JSONDecodeError"
    exc_message: str      # str(exception), truncated to 300 chars
    traceback_short: str  # last 6 stack frames, max 800 chars
    raw_llm_response: str # raw LLM output if available
    attempt: int          # retry attempt number
    extra: dict           # stage-specific metadata
```

### Diagnosis schema

```python
@dataclass
class Diagnosis:
    likely_cause: str        # human-readable one-liner
    confidence: float        # 0.0–1.0
    suggested_fixes: list[str]  # ordered remediation steps
    auto_fixable: bool       # can wizard.apply_fix() repair this?
    fix_tag: str             # internal dispatch tag for apply_fix()
```

### Decision tree — rule priority order

| Priority | Trigger | Cause | Auto-fix? |
|---|---|---|---|
| 1 | `JSONDecodeError` or `ValueError` with "json"/"parse"/"decode" in message | LLM output truncated/malformed | Yes — `json_repair` |
| 2 | `KeyError` | LLM skipped required output keys | Yes — `inject_defaults` |
| 3 | `FileNotFoundError` with "generated"/"output"/"feature" in message | Code-gen did not write files | No |
| 3b | `FileNotFoundError` (other) | Missing spec/config file | No |
| 4 | `ConnectionError`, `ConnectionRefusedError`, `RuntimeError` or "timeout"/"connection"/"llama"/"ollama" in message | Server not running | No |
| 4b | `TimeoutError` | LLM call timed out | No |
| 5 | Stage contains "review" AND `attempt >= 3` | Model cannot meet quality bar | No |
| 6 | Stage contains "guardrail" or "security" | Forbidden patterns in generated code | No |
| fallback | Any other | Unknown — show full trace | No |

### Auto-fix implementations

- **`json_repair`** — calls `parse_llm_json(event.raw_llm_response)`. If
  successful, prints the recovered keys. The pipeline does not automatically
  re-inject this result; the repair is a diagnostic confirmation.
- **`inject_defaults`** — informs the user that default injection requires
  re-running the pipeline stage (cannot be applied mid-stream).

### Run log schema (`logs/pipeline_runs.jsonl`)

One JSON object per line, appended by `append_run_log()`:

```json
{
  "timestamp": "2026-07-20T12:00:00.000000+00:00",
  "feature_name": "my_feature",
  "stages_completed": [
    "spec_validation",
    "code_generation:attempt_1",
    "guardrail_check:attempt_1",
    "review:attempt_1"
  ],
  "failures": [
    {
      "stage": "code_generation",
      "exc_type": "JSONDecodeError",
      "exc_message": "Expecting ',' delimiter: line 1 column 312",
      "traceback_short": "  ...",
      "raw_llm_response": "...",
      "attempt": 1,
      "extra": {}
    }
  ],
  "final_status": "APPROVED"
}
```

---

## 10. Adding a new agent

1. **Create a system prompt file** in `agent_instructions/<agent_name>.md`.
   Follow the existing format: state the role, evaluation tasks, hard
   constraints, and an exact JSON output schema.

2. **Create the agent module** in `tools/<agent_name>.py`. Import `LLMClient`
   and call it with a role string. Use `parse_llm_json()` for all LLM output
   parsing.

3. **Register the role** in `.agentconfig.yaml` under `llama.models` and
   `llama.hints`. Add the env var name to `tools/config.py`.

4. **Wire into agent_runner.py** — add the stage to the pipeline sequence,
   wrap in `pipeline_stage()` for Wizard integration, append to
   `stages_completed`.

5. **Write tests** in `tests/test_<agent_name>.py`. Mock the LLM client; test
   the parsing logic, fallback behaviour, and hard overrides.

6. **Run ruff** — `python -m ruff check tools/<agent_name>.py` must pass with
   zero errors before committing.
