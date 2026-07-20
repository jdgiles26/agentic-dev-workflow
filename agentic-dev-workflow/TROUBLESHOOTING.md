# TROUBLESHOOTING.md — Failure modes, diagnostics, and exact fixes

Every known failure mode for the agentic dev workflow pipeline, with the exact
error message, root cause, and step-by-step fix.

---

## Table of contents

1. [Server and connectivity failures](#1-server-and-connectivity-failures)
2. [Model failures](#2-model-failures)
3. [JSON parse failures](#3-json-parse-failures)
4. [Spec validation failures](#4-spec-validation-failures)
5. [Code generation failures](#5-code-generation-failures)
6. [Guardrail failures](#6-guardrail-failures)
7. [Test and coverage failures](#7-test-and-coverage-failures)
8. [Review failures](#8-review-failures)
9. [Pre-flight failures](#9-pre-flight-failures)
10. [GUI server failures](#10-gui-server-failures)
11. [Python environment failures](#11-python-environment-failures)
12. [General debugging workflow](#12-general-debugging-workflow)

---

## 1. Server and connectivity failures

### `Cannot reach llama.cpp server at http://127.0.0.1:8012`

**Trigger:** Pre-flight check 1 fails, or `ModelManager._refresh()` raises.

**Root cause:** The llama.cpp server is not running, is running on a different
port, or a firewall is blocking the connection.

**Fix:**
```bash
# Start the server
llama serve --fim-qwen-3b-default -ngl 99 --port 8012

# Verify it is responding
curl http://127.0.0.1:8012/v1/models
```

If you changed the port, update `.env`:
```bash
LLAMA_HOST=http://127.0.0.1:<your_port>
```

---

### `ConnectionRefusedError: [Errno 61] Connection refused`

**Trigger:** Any LLM call stage.

**Root cause:** Server was running at pre-flight time but crashed or was killed
before the pipeline reached the LLM call stage.

**Fix:** Restart the server. Check `llama serve` output for OOM errors or
model loading failures.

---

### LLM call times out after N seconds

**Error message:**
```
RuntimeError: llama.cpp server timed out after 120s (model: ...).
Increase LLAMA_TIMEOUT in .env or try a smaller context.
```

**Root cause:** The model is too large for your hardware and takes more than
`LLAMA_TIMEOUT` seconds to respond.

**Fix (in order of preference):**
1. Increase timeout: `LLAMA_TIMEOUT=300` in `.env`
2. Use a smaller quantised model: `Q4_K_M` instead of `Q8_0`
3. Enable GPU layers: `llama serve --n-gpu-layers 99 --port 8012`
4. Reduce `max_tokens` (edit `tools/llm_client.py` line with `max_tokens=4096`)

---

## 2. Model failures

### `llama.cpp server returned no models`

**Trigger:** Pre-flight check 2 fails.

**Root cause:** The server is reachable but no model has been loaded.

**Fix:** Start the server with an explicit model:
```bash
llama-server --model /path/to/model.gguf --port 8012
```

Verify the model appears:
```bash
curl http://127.0.0.1:8012/v1/models | python -m json.tool
```

---

### `Configured model 'X' not found. Falling back to auto-selection.`

**Root cause:** `mode: manual` is set in `.agentconfig.yaml` but the model
name under `llama.models` does not match what the server reports.

**Fix:** Either:
- Switch to `mode: auto` to let `ModelManager` pick from available models
- Match the model name exactly to what `/v1/models` returns:
```bash
curl http://127.0.0.1:8012/v1/models | python -c \
  "import json,sys; [print(m['id']) for m in json.load(sys.stdin)['data']]"
```

---

## 3. JSON parse failures

### `JSONDecodeError` or `Could not parse JSON even after repair`

**Trigger:** Any LLM-dependent stage (spec validator, developer agent,
security guard, reviewer agent).

**Root cause:** The LLM truncated its output at `max_tokens`, producing
incomplete JSON.

**Diagnosis:** Run with `--wizard` — the Wizard will offer auto-repair:
```bash
python -m tools.agent_runner my_feature --wizard
```

**Fixes (in order):**

1. **Increase `max_tokens`** — edit `tools/llm_client.py`:
   ```python
   max_tokens=8192   # was 4096
   ```

2. **Simplify the spec** — shorter `prd.md` and fewer TDD test cases reduce
   output length.

3. **Switch to a larger model** — 7B models produce much more reliable JSON
   than 3B models. Qwen2.5-Coder-7B-Q4_K_M is recommended for developer role.

4. **Set `context-size` on the server** — the model needs enough context to
   hold the prompt + full response:
   ```bash
   llama-server --ctx-size 8192 --model /path/to/model.gguf --port 8012
   ```

---

### `No JSON object found in LLM response`

**Root cause:** The LLM returned plain text instead of JSON — it either
misunderstood the prompt or produced a refusal message.

**Fix:**
1. Enable `LOG_LEVEL=DEBUG` and inspect the raw response:
   ```bash
   LOG_LEVEL=DEBUG python -m tools.agent_runner my_feature 2>&1 | grep "Raw response"
   ```
2. If the model is producing English prose, add stronger constraints to the
   system prompt in `agent_instructions/`.
3. Switch to a model with better instruction-following.

---

## 4. Spec validation failures

### `PRD and TDD are inconsistent`

**Root cause:** The Spec Validator determined that one or more numbered
acceptance criteria in `prd.md` have no corresponding test case in `tdd.yaml`.

**Fix:**
1. Read the validator output — it lists the missing criteria:
   ```
   [SpecValidator] 2 uncovered criterion(ia):
     • criterion 3
     • criterion 5
   ```
2. Add TDD test cases that cover those criteria.
3. Re-run.

**If the model is wrong** (false negative):
- The yes/no fallback is tried automatically. If it also fails, the pipeline
  blocks.
- Override temporarily by re-ordering or rewording the acceptance criteria to
  make the semantic match more obvious.
- For chronic false negatives: switch the validator role to a larger model
  with `LLAMA_VALIDATOR_MODEL=<bigger-model>`.

---

### Spec validation passes but the code does not meet the criteria

**Root cause:** The Spec Validator uses LLM judgment which is imperfect for
3B models. The validator may produce a false positive ("consistent") when the
TDD is actually under-specified.

**Fix:** Write explicit, unambiguous TDD test cases. Each acceptance criterion
in the PRD should have at least one TDD test case with a matching name that
references the criterion number or keyword.

---

## 5. Code generation failures

### `[DeveloperAgent] JSON missing 'files' list`

**Root cause:** The LLM returned valid JSON but omitted the `"files"` key, or
returned `"files": null` / `"files": {}`.

**Fix:** Add `--wizard` and apply the `inject_defaults` auto-fix, then retry.
Long-term: add to `agent_instructions/developer_agent.md`:
```
The "files" key is MANDATORY. Omitting it is a critical error.
```

---

### `[DeveloperAgent] LLM returned empty 'files' list`

**Root cause:** The LLM returned `{"files": [], "test_command": "..."}` — it
produced the schema correctly but no file content.

**Fix:** This is usually a context-length issue. The prompt plus a full
response does not fit in the model's context window. Reduce prompt size
(shorten prd.md/tdd.yaml) or increase `--ctx-size` on the server.

---

### Generated code is syntactically invalid Python

**Root cause:** LLM produced Python with syntax errors. This causes the
pytest stage to fail with `SyntaxError`.

**Symptoms:** pytest output contains `SyntaxError` or `ModuleNotFoundError`.

**Fix:**
1. Inspect the generated file:
   ```bash
   cat generated/<feature>/src/*.py
   python -m py_compile generated/<feature>/src/*.py
   ```
2. Retry — on the next attempt the Developer Agent will receive the review
   feedback `"tests failed"` which often causes it to fix the syntax.
3. Use a larger/better coding model.

---

## 6. Guardrail failures

### `[Guardrails] N static violation(s) found`

**Root cause:** The Developer Agent generated code containing one or more
patterns from `guardrails/forbidden_actions.yaml`.

**Example output:**
```
[StaticScan] src/module.py:14  →  os.system
[Guardrails] 1 static violation(s) found.
```

**Fix:**
1. The pipeline retries automatically with feedback injected.
2. If the same violation appears on every retry, the model is ignoring
   constraints. Add a more explicit instruction to
   `agent_instructions/developer_agent.md`:
   ```
   NEVER use os.system under any circumstances. This is a hard requirement.
   ```
3. Check whether the pattern match is a false positive (e.g. the pattern
   appears in a comment or string, not executable code). If so, refine the
   pattern in `forbidden_actions.yaml` — consider adding a check that the
   pattern is not inside a string literal (requires a code change to
   `guardrail_enforcer.py`).

---

### `[SemanticScan] Could not parse LLM response — assuming safe`

**Root cause:** The Security Guard LLM produced unparseable output.

**This is a warning, not a failure.** The semantic scan passes through in this
case — the static scan remains the authoritative gate.

**If you need reliable semantic scanning:** Use a larger model (7B+) for the
security role:
```bash
LLAMA_SECURITY_MODEL=phi-3-medium-128k-instruct-Q4_K_M
```

---

## 7. Test and coverage failures

### `No tests/ directory found`

**Root cause:** The Developer Agent did not generate a `tests/` subdirectory
inside the feature output.

**Fix:** Retry — the model likely truncated its output. With `--wizard`, the
`code_generation` stage failure will be diagnosed. Increasing `max_tokens` or
using a larger model usually resolves this.

---

### `Coverage 0%` or coverage below threshold

**Root cause (Coverage 0%):** `conftest.py` is missing or malformed, so
pytest cannot find `src/` and coverage measurement fails.

**Check:**
```bash
cat generated/<feature>/conftest.py
# Should contain:
# import sys, pathlib
# sys.path.insert(0, str(pathlib.Path(__file__).parent))
```

If the file is missing, regenerate it:
```bash
python -c "
from pathlib import Path
f = Path('generated/<feature>/conftest.py')
f.write_text('import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n')
"
```

**Root cause (coverage below threshold):** The generated tests do not cover
enough of the source code.

**Fix:**
1. The pipeline retries with feedback about coverage. This usually self-corrects.
2. Lower the threshold if the feature is inherently difficult to cover:
   ```yaml
   # .agentconfig.yaml
   review:
     coverage_threshold: 70
   ```
3. Add more explicit test requirements to `tdd.yaml`.

---

### `pytest: error: unrecognized arguments`

**Root cause:** The pytest version in the venv is too old.

**Fix:**
```bash
pip install --upgrade pytest pytest-cov
```

---

### Tests pass locally but fail in pipeline

**Root cause:** The pipeline runs pytest in a subprocess with the feature
directory as `cwd`. If `src/` is not on `sys.path`, imports fail even if they
work when running pytest manually from the repo root.

**Check:** Confirm `conftest.py` exists and contains the `sys.path.insert` call.

---

## 8. Review failures

### `No source files found in generated/src/`

**Root cause:** The Developer Agent wrote code to a different path than
`src/<module>.py`, or the files list used a non-standard path.

**Fix:**
```bash
ls -la generated/<feature>/
```
If files are in the root instead of `src/`, the Developer Agent ignored the
path constraint. Add to `agent_instructions/developer_agent.md`:
```
All source files MUST be placed under src/ — e.g. src/module.py.
The path in the "files" list must start with "src/".
```

---

### LLM verdict is `PASS` but pipeline still shows `FAIL`

**Root cause:** Hard overrides are applied after the LLM verdict. If pytest
failed or coverage was below threshold, those override the LLM's `PASS`.

**Check:** Look for `[Hard override]` in the feedback field:
```
[Hard override] pytest exited non-zero.
[Hard override] Coverage 45% < 80%.
```

Fix the underlying test/coverage issue — these overrides cannot be bypassed
without changing `coverage_threshold` or fixing the generated code.

---

### Feature keeps failing after `max_retries` attempts

**Root cause:** The model is unable to generate code that meets all gates
within the retry budget.

**Diagnosis using `--wizard`:** After 3+ review failures, the Wizard will
diagnose `"Model has failed the review check N times"` and suggest:

**Fixes:**
1. Increase `max_retries` (gives more chances to converge):
   ```yaml
   max_retries: 5
   ```
2. Lower `coverage_threshold`:
   ```yaml
   review:
     coverage_threshold: 70
   ```
3. Simplify the feature spec — reduce the number of acceptance criteria and
   TDD test cases to fit smaller models.
4. Use a larger / better model for the developer role.

---

## 9. Pre-flight failures

### `Missing packages: openai, pyyaml`

**Fix:**
```bash
pip install -r requirements.txt
```

If using a venv, ensure it is activated:
```bash
source .venv/bin/activate
pip install -r requirements.txt
```

---

### `Cannot write to OUTPUT_DIR generated/`

**Root cause:** A permissions issue prevents creating or writing to the
`generated/` directory.

**Fix:**
```bash
# Fix permissions
chmod -R u+w generated/

# Or recreate the directory
rm -rf generated/
mkdir generated/
```

---

## 10. GUI server failures

### `OSError: [Errno 48] Address already in use`

**Root cause:** Another process is already bound to port 8099, or a previous
`gui_server` process did not shut down cleanly.

**Fix:**
```bash
# Kill whatever is using the port
lsof -ti tcp:8099 | xargs kill -9

# Or use a different port
python -m tools.gui_server --port 9000
```

---

### GUI loads but pipeline output does not stream

**Root cause (most likely):** The browser's EventSource connection was dropped,
or the server process died mid-run.

**Diagnosis:**
1. Open browser developer tools → Network tab → find the `/api/run?f=...`
   request.
2. Check if it shows `ERR_CONNECTION_REFUSED` or `ERR_INCOMPLETE_CHUNKED_ENCODING`.
3. Check the terminal where `gui_server` is running for Python tracebacks.

**Fix:**
- Reload the page and try again.
- If the server crashed, restart it: `python -m tools.gui_server`
- If output stops mid-run, the pipeline subprocess may have crashed — check
  `logs/agent_runner.log` for the error.

---

### `gui.html not found`

**Root cause:** `tools/gui.html` is missing — it may have been accidentally
deleted or the repo was cloned without that file.

**Fix:**
```bash
git checkout tools/gui.html
```

---

### Configuration save does not persist

**Root cause:** The `.agentconfig.yaml` file may be read-only, or the GUI
server was started from a directory where it cannot find the project root.

**Fix:**
```bash
# Ensure the file is writable
chmod u+w .agentconfig.yaml

# Always start gui_server from the project root
cd /path/to/agentic-dev-workflow
python -m tools.gui_server
```

---

## 11. Python environment failures

### `ModuleNotFoundError: No module named 'tools'`

**Root cause:** The package is not installed in editable mode, and `tools` is
not on `sys.path`.

**Fix:**
```bash
# From the repo root
pip install -e .
# Or set PYTHONPATH
PYTHONPATH=. python -m tools.agent_runner example_feature
```

---

### `FileNotFoundError: Config file not found: .agentconfig.yaml`

**Root cause:** `tools/config.py` looks for `.agentconfig.yaml` relative to
the package root (`tools/../.agentconfig.yaml`). If you moved or renamed the
file, the loader will fail.

**Fix:**
```bash
# Ensure the file exists at the project root
ls -la .agentconfig.yaml

# If missing, restore from git
git checkout .agentconfig.yaml
```

---

### `YAML safe_load` error on `.agentconfig.yaml`

**Root cause:** The YAML file has invalid syntax — likely introduced by a
manual edit or a partial GUI save.

**Fix:**
```bash
# Validate the YAML
python -c "import yaml; yaml.safe_load(open('.agentconfig.yaml'))"

# Restore from git if corrupted
git checkout .agentconfig.yaml
```

---

## 12. General debugging workflow

When a pipeline run fails and the cause is not obvious:

### Step 1 — Run with Wizard mode

```bash
python -m tools.agent_runner <feature> --wizard
```

The Wizard prints a structured diagnostic card for every failure with a likely
cause and ordered fix suggestions.

### Step 2 — Enable DEBUG logging

```bash
LOG_LEVEL=DEBUG python -m tools.agent_runner <feature> 2>&1 | tee /tmp/debug.log
```

Debug level captures:
- Every LLM call's input character count
- Raw LLM response (first 300 chars) for every call
- All JSON parse attempts and repair steps
- ModelManager scoring details

### Step 3 — Inspect the run log

```bash
tail -1 logs/pipeline_runs.jsonl | python -m json.tool
```

The run log records every `FailureEvent` with `exc_type`, `exc_message`, and
`traceback_short`. The `stages_completed` list shows exactly where the
pipeline stopped.

### Step 4 — Inspect the agent log

```bash
# Last 100 lines
tail -100 logs/agent_runner.log

# Search for errors
grep -i "error\|fail\|warn" logs/agent_runner.log
```

### Step 5 — Inspect generated code

```bash
# List what the Developer Agent produced
ls -la generated/<feature>/src/
ls -la generated/<feature>/tests/

# Run tests manually to see the raw pytest output
cd generated/<feature>
pytest tests/ -v --tb=long

# Check static scan manually
python -c "
from tools.guardrail_enforcer import static_scan
violations = static_scan('<feature>')
for v in violations:
    print(v)
"
```

### Step 6 — Test the LLM server directly

```bash
# Test that the server responds to chat completions
curl http://127.0.0.1:8012/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "auto",
    "messages": [{"role": "user", "content": "Say hello in JSON: {\"msg\": \"...\"}"}],
    "max_tokens": 50
  }'
```

If this returns valid JSON, the server is healthy. If it times out or returns
an error, the issue is with the server, not the pipeline.
