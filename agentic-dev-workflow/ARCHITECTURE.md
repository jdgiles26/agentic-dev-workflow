# ARCHITECTURE.md — System design, module map, and data flows

---

## Table of contents

1. [High-level architecture](#1-high-level-architecture)
2. [Component map](#2-component-map)
3. [Pipeline sequence](#3-pipeline-sequence)
4. [Module dependency graph](#4-module-dependency-graph)
5. [Data contracts](#5-data-contracts)
6. [Configuration loading](#6-configuration-loading)
7. [LLM call path](#7-llm-call-path)
8. [Guardrail layer design](#8-guardrail-layer-design)
9. [Wizard and run logging](#9-wizard-and-run-logging)
10. [GUI server design](#10-gui-server-design)
11. [Design decisions and trade-offs](#11-design-decisions-and-trade-offs)

---

## 1. High-level architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          User interface layer                        │
│                                                                     │
│   CLI: python -m tools.agent_runner <feature> [--wizard]           │
│   GUI: python -m tools.gui_server  →  browser at localhost:8099    │
└────────────────────┬────────────────────────────────────────────────┘
                     │ drives
                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    tools/agent_runner.py (orchestrator)             │
│                                                                     │
│  preflight checks → spec validation → [retry loop]                 │
│                         ↳ code generation                           │
│                         ↳ guardrail scan                            │
│                         ↳ review + pytest                           │
│                         ↳ approve or inject feedback                │
└────────────────────┬────────────────────────────────────────────────┘
                     │ calls
       ┌─────────────┼──────────────────┬──────────────────┐
       ▼             ▼                  ▼                  ▼
  spec_validator  developer_agent  guardrail_enforcer  reviewer_agent
       │             │                  │                  │
       └─────────────┴──────────────────┴──────────────────┘
                     │ all use
                     ▼
             tools/llm_client.py
             tools/model_manager.py
                     │ HTTP POST
                     ▼
        llama.cpp server (OpenAI-compatible API)
        http://127.0.0.1:8012/v1/chat/completions
                     │
                     ▼
              GGUF model on disk
```

---

## 2. Component map

| Component | File | Responsibility |
|---|---|---|
| Orchestrator | `tools/agent_runner.py` | Pipeline state machine, retry loop, CLI/args |
| Config loader | `tools/config.py` | Merge YAML + env vars into typed constants |
| Model manager | `tools/model_manager.py` | Discover + score models, cache role assignments |
| LLM client | `tools/llm_client.py` | Async OpenAI-compatible HTTP wrapper |
| JSON repair | `tools/llm_json.py` | Extract + repair truncated JSON from LLM output |
| Spec validator | `tools/spec_validator.py` | PRD ↔ TDD consistency via LLM (2-strategy) |
| Developer agent | `tools/developer_agent.py` | Generate `src/` + `tests/` from specs |
| Guardrail enforcer | `tools/guardrail_enforcer.py` | Static pattern scan + LLM semantic scan |
| Reviewer agent | `tools/reviewer_agent.py` | Run pytest, measure coverage, LLM verdict |
| Wizard | `tools/wizard.py` | Failure capture, rule-based diagnosis, run logging |
| Preflight | `tools/preflight.py` | Environment health checks before pipeline start |
| GUI server | `tools/gui_server.py` | Stdlib HTTP+SSE server, serves gui.html |
| GUI front-end | `tools/gui.html` | Single-page browser interface |

---

## 3. Pipeline sequence

The orchestrator implements a state machine with five stages. Stages 1–3
loop for up to `max_retries` attempts.

```
agent_runner.run_pipeline(feature_name)
│
├── preflight.run_preflight_checks()                     [once]
│   ├── _check_server_reachable()
│   ├── _check_model_available()
│   ├── _check_dependencies()
│   └── _check_output_writable()
│
├── [abort if prd.md or tdd.yaml missing]
│
├── Stage 0: spec_validator.validate_specs()             [once]
│   ├── Strategy 1: LLM JSON response
│   └── Strategy 2: LLM yes/no fallback
│   └── [abort if inconsistent]
│
└── for attempt in 1..max_retries:
    │
    ├── Stage 1: developer_agent.generate_feature()
    │   ├── LLM call → JSON with "files" list
    │   ├── parse_llm_json() with repair
    │   └── write_generated_files() → generated/<feature>/
    │
    ├── Stage 2: guardrail_enforcer.run_guardrails()
    │   ├── static_scan() → regex on every .py file
    │   └── semantic_scan() → LLM Security Guard
    │   └── [if failed: inject feedback, continue loop]
    │
    ├── Stage 3: reviewer_agent.review_feature()
    │   ├── _run_tests() → subprocess pytest
    │   ├── extract coverage from output
    │   ├── LLM Reviewer call → verdict JSON
    │   └── apply hard overrides (pytest exit code, coverage)
    │
    └── if verdict.overall == "PASS":
            append_run_log(..., "APPROVED")
            return True
        else:
            last_feedback = verdict.feedback
            sleep(retry_delay)
            continue

append_run_log(..., "REJECTED")
return False
```

Each stage is wrapped in `pipeline_stage()` (async context manager from
`wizard.py`) which captures any exception as a `FailureEvent`. In `--wizard`
mode, the diagnostic card is printed before the exception propagates.

---

## 4. Module dependency graph

```
agent_runner
├── config
├── spec_validator
│   ├── llm_client
│   │   ├── config
│   │   └── model_manager ── config
│   └── llm_json
├── developer_agent
│   ├── config
│   ├── llm_client
│   └── llm_json
├── guardrail_enforcer
│   ├── config
│   ├── llm_json
│   └── llm_client (deferred import — only for semantic scan)
├── reviewer_agent
│   ├── config
│   ├── llm_client
│   └── llm_json
├── wizard
│   └── config  (LOG_DIR only)
└── preflight
    ├── config  (OUTPUT_DIR, LLAMA_HOST)
    └── wizard  (FailureEvent)

gui_server (standalone — does not import pipeline at module level)
├── yaml  (stdlib-adjacent: pyyaml)
└── tools.preflight  (deferred import — only for /api/preflight endpoint)
```

`guardrail_enforcer` deliberately defers its `LLMClient` import so the static
scan can be used in tests without pulling in the `openai` dependency.

`gui_server` defers all pipeline imports so the GUI can start even if some
tool modules have import errors — it will report those errors through the
`/api/preflight` endpoint rather than crashing at startup.

---

## 5. Data contracts

### Spec files

```
specs/<feature>/prd.md      — Markdown, must contain ## Acceptance Criteria
                              section with numbered items (1. 2. 3. ...)
specs/<feature>/tdd.yaml    — YAML, must have top-level "tests:" list;
                              each entry: id, name, description, inputs,
                              expected_result, expected_exception
```

### Developer Agent output (LLM → JSON)

```json
{
  "files": [
    {"path": "src/<module>.py",       "content": "<source>"},
    {"path": "tests/test_<module>.py","content": "<tests>"}
  ],
  "test_command": "pytest tests/ -v --cov=src --cov-report=term"
}
```

Paths are relative to `generated/<feature_name>/`.
The `test_command` field is recorded but not currently executed (pytest is
invoked directly by `reviewer_agent.py`).

### Security Guard output (LLM → JSON)

```json
{"safe": true, "violations": []}
```
or:
```json
{
  "safe": false,
  "violations": [
    {"file": "src/x.py", "line": 12, "pattern": "os.system", "detail": "..."}
  ]
}
```

### Reviewer Agent output (LLM → JSON)

```json
{
  "overall": "PASS",
  "prd_check": "PASS",
  "tdd_check": "PASS",
  "guardrail_check": "PASS",
  "feedback": "All criteria met."
}
```

### FailureEvent (wizard.py)

```json
{
  "stage": "code_generation",
  "exc_type": "JSONDecodeError",
  "exc_message": "Expecting ',' delimiter: line 1 column 312",
  "traceback_short": "  File tools/developer_agent.py ...\n  ...",
  "raw_llm_response": "{\"files\": [{\"path\": \"src/...",
  "attempt": 1,
  "extra": {}
}
```

### Pipeline run log entry (`logs/pipeline_runs.jsonl`)

```json
{
  "timestamp": "2026-07-20T12:00:00.000000+00:00",
  "feature_name": "example_feature",
  "stages_completed": [
    "spec_validation",
    "code_generation:attempt_1",
    "guardrail_check:attempt_1",
    "review:attempt_1"
  ],
  "failures": [],
  "final_status": "APPROVED"
}
```

---

## 6. Configuration loading

`tools/config.py` loads in priority order (highest wins):

```
1. Environment variables (LLAMA_HOST, LLAMA_DEVELOPER_MODEL, etc.)
2. .env file  (loaded via python-dotenv at module import time)
3. .agentconfig.yaml  (base configuration)
4. Hardcoded defaults in config.py
```

All config values are module-level constants, typed, and available via
`from tools.config import LLAMA_HOST, OUTPUT_DIR, ...`. No class or singleton
is needed — the module itself is the singleton.

YAML is re-read at process startup and cached for the process lifetime.
Changes to `.agentconfig.yaml` during a run do not take effect until restart
(or until the GUI saves and you re-run).

---

## 7. LLM call path

```
LLMClient.generate(system_prompt, user_prompt)
│
├── _resolve_model()          → calls ModelManager.get_model(role) in executor
│                               (thread pool — ModelManager._refresh() is sync)
│
└── AsyncOpenAI.chat.completions.create(
        model=model,
        messages=[system, user],
        temperature=0.15,
        max_tokens=4096
    )
    wrapped in asyncio.wait_for(timeout=LLAMA_TIMEOUT)
    │
    └── HTTP POST /v1/chat/completions to llama.cpp
        Content-Type: application/json
        Authorization: Bearer local   (dummy key)
```

All pipeline stages are `async` functions called with `asyncio.run()` from
`main()`. The event loop is created once per pipeline invocation.

---

## 8. Guardrail layer design

Two independent layers with different failure characteristics:

```
Static scan (Layer 1)
─────────────────────
Input:  every .py file in generated/<feature>/
Method: line-by-line string contains() search against forbidden_actions.yaml
Output: list[Violation(file, line, pattern, detail)]
Speed:  < 100ms for typical feature size
False negatives: obfuscated patterns (handled by Layer 2)
False positives: none (exact string match)

Semantic scan (Layer 2)
────────────────────────
Input:  all generated code concatenated into one prompt
Method: LLM Security Guard with structured JSON output
Output: {safe: bool, violations: [...]}
Speed:  5–60s depending on model size and code length
False negatives: possible (LLM may miss subtle issues)
False positives: possible (LLM may flag safe patterns)
Failure mode: unparseable JSON → treated as safe (pass-through)
```

The design deliberately passes through on semantic scan JSON failure rather
than blocking, because a 3B model that cannot produce valid JSON is very
unlikely to have accurately detected real violations. The static scan remains
the authoritative gate.

Both layers are run regardless of the static scan result — the semantic scan
may catch violations the static scan missed (e.g. multi-line string
construction of a forbidden call).

---

## 9. Wizard and run logging

The Wizard is not a separate process or LLM agent — it is a library of
functions used by `agent_runner.py`. Its key design properties:

**`pipeline_stage()` — async context manager**

Wraps any pipeline stage call. On exception:
1. Builds a `FailureEvent` from the exception and traceback.
2. Captures the raw LLM response if a `raw_llm_response_ref` list was passed.
3. If `wizard_mode=True`, calls `diagnose_and_report()` before re-raising.
4. Appends the event to the caller's `stage_failures` list.

The caller accumulates `stage_failures` into `all_failures`, which is passed
to `append_run_log()` at the end of the run regardless of outcome.

**`analyse()` — pure function, no side effects**

Takes a `FailureEvent`, returns a `Diagnosis`. Implemented as a linear
priority scan of six rules. No mutable state. Fully unit-testable with no
mocks required.

**`append_run_log()` — file append, no in-memory state**

Opens `logs/pipeline_runs.jsonl` in append mode, writes one JSON line, closes
immediately. Thread-safe for single-process use (no concurrent writes from
the same process). Not designed for multi-process concurrent use.

---

## 10. GUI server design

**File:** `tools/gui_server.py`

```
Browser                    gui_server.py              pipeline
   │                            │                        │
   │  GET /                     │                        │
   │ ─────────────────────────▶ │                        │
   │ ◀─────── gui.html ──────── │                        │
   │                            │                        │
   │  GET /api/preflight        │                        │
   │ ─────────────────────────▶ │                        │
   │                            │  run_preflight_checks()│
   │                            │ ──────────────────────▶│
   │                            │ ◀── list[CheckResult] ─│
   │ ◀──── JSON results ─────── │                        │
   │                            │                        │
   │  GET /api/run?f=example    │                        │
   │ ─────────────────────────▶ │                        │
   │  ← SSE stream opens        │  subprocess Popen      │
   │                            │ ──────────────────────▶│
   │  data: ▶ Developer Agent…  │     stdout line        │
   │  data: ✓ Generated 2 files │ ◀──────────────────────│
   │  data: ▶ Guardrail scan…   │     stdout line        │
   │  data: __EXIT__0           │ ◀──────────────────────│
   │  (stream closes)           │                        │
```

**Threading model:**

- Main thread: `ThreadingHTTPServer` — one thread per connection.
- Per `/api/run` request: spawns a daemon thread running `_stream_pipeline()`
  which reads the subprocess stdout into a `queue.Queue[str | None]`.
- The handler thread drains the queue and writes SSE frames.
- Sentinel `None` signals completion; `__EXIT__N` carries the exit code.

**Why subprocess, not in-process?**

Running the pipeline in-process would pollute the server's global state
(logging handlers, module-level config singletons, sys.path). The subprocess
boundary provides clean isolation: each pipeline run gets a fresh Python
interpreter. The trade-off is ~0.3s startup overhead per run, acceptable for
a development tool.

---

## 11. Design decisions and trade-offs

### Why llama.cpp over Ollama?

The project was developed to work with **both**. The `LLMClient` uses the
OpenAI-compatible API which both llama.cpp and Ollama expose. The model names
in `.agentconfig.yaml` use the llama.cpp format
(`ggml-org/Qwen2.5-Coder-3B-Q8_0-GGUF`); for Ollama substitute the Ollama
model tag format (`qwen2.5-coder:7b`).

### Why `temperature=0.15` not `0.0`?

Zero temperature on some small models causes repetitive loops or stuck output.
`0.15` keeps output near-deterministic while avoiding degenerate generation.
For larger models (≥13B), `0.0` is safe and produces more consistent JSON.

### Why two Spec Validator strategies?

3B models frequently refuse to produce valid JSON when the prompt is long.
The yes/no fallback uses a shorter prompt (extracted criteria + test names
only) which reliably fits in context and is easier for a small model to answer.
The trade-off: binary yes/no loses the list of missing criteria. The pipeline
still aborts on "no", so the gate is preserved.

### Why not stream LLM output?

Streaming would improve perceived responsiveness but complicates JSON repair:
`parse_llm_json()` needs the complete string to detect and close truncations.
The non-streaming approach also simplifies timeout handling — one
`asyncio.wait_for()` covers the entire call.

### Why a file-append run log vs a database?

The pipeline is a local development tool. A flat JSONL file requires no schema
migration, is trivially readable with any text editor, and survives process
crashes without corruption. The GUI server reads it on demand. If you need
querying, `jq` works on JSONL files directly.

### Why stdlib HTTP server for the GUI?

Adding Flask/FastAPI would require an additional install step and introduce
version conflicts in environments where those packages are already present.
`http.server.ThreadingHTTPServer` is sufficient for a single-user local tool
handling one pipeline run at a time. The only non-stdlib dependency in
`gui_server.py` is `pyyaml`, which is already in `requirements.txt`.
