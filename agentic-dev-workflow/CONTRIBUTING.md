# CONTRIBUTING.md — How to extend, improve, and contribute to this project

---

## Table of contents

1. [Code style and standards](#1-code-style-and-standards)
2. [Before you start: design principles](#2-before-you-start-design-principles)
3. [Adding a new feature spec](#3-adding-a-new-feature-spec)
4. [Adding a new agent](#4-adding-a-new-agent)
5. [Adding guardrail rules](#5-adding-guardrail-rules)
6. [Modifying the GUI](#6-modifying-the-gui)
7. [Writing tests](#7-writing-tests)
8. [Pull request checklist](#8-pull-request-checklist)
9. [Spec format reference](#9-spec-format-reference)
10. [Compliance sign-off](#10-compliance-sign-off)

---

## 1. Code style and standards

- **Language:** Python 3.11+. Use `from __future__ import annotations` at
  the top of every module for forward-compatible type hints.
- **Linter:** `ruff`. Zero violations required before merge. Run:
  ```bash
  python -m ruff check tools/ tests/
  ```
- **Type hints:** All public functions and methods must have full type
  annotations on parameters and return values.
- **Docstrings:** Every module, class, and public function must have a
  docstring. Single-line for obvious helpers; multi-line for anything with
  non-trivial logic or parameters.
- **Line length:** 88 characters (ruff default). Exception: long string
  literals in prompts where wrapping would reduce readability.
- **Imports:** Standard library first, then third-party, then local. No
  circular imports. Deferred imports (`import X` inside a function) are
  acceptable only for breaking circular dependencies or deferring heavy
  side-effect imports — always add a `# noqa: PLC0415` comment with an
  explanation.
- **No new third-party dependencies** without discussion. The project
  deliberately has a small dependency surface. `requirements.txt` is the
  source of truth. `openai`, `pyyaml`, `python-dotenv`, `pytest`,
  `pytest-cov`, `rich`, `anyio` are currently the only runtime/test deps.

---

## 2. Before you start: design principles

These principles guide every design decision in the pipeline. New contributions
must respect them.

### No mocks in production code paths

Every function that runs during a real pipeline invocation must call real code.
Mocking is only for unit tests. If a pipeline stage would not work without a
running LLM server, that is by design — the pre-flight checks gate on it.

### Minimal change

Add only what is needed to solve the stated problem. Do not add features,
refactors, or abstractions beyond the scope of the change. Every added line
must trace directly to a concrete requirement.

### Fail fast, fail clearly

When something goes wrong, the error message must tell the user exactly what
to do. Generic `raise Exception("something went wrong")` is never acceptable.
Use specific exception types and include the failing value in the message.

### No hard-coded paths

All paths are derived from `tools/config.py` constants (`SPECS_DIR`,
`OUTPUT_DIR`, `LOG_DIR`, etc.) which are in turn derived from
`.agentconfig.yaml`. Never hard-code a directory name in a module other than
`config.py`.

### Determinism over creativity

Agent prompts use `temperature=0.15`. Structured output (JSON) generation must
be as deterministic as possible. Avoid techniques that increase output variance
(high temperature, chain-of-thought without a final structured answer) unless
there is a specific reason.

---

## 3. Adding a new feature spec

Feature specs live in `specs/<feature_name>/` and consist of exactly two files.
Both are tracked in git.

### `prd.md` requirements

- Must have a `## Acceptance Criteria` section.
- Criteria must be **numbered** starting at 1 (e.g. `1. Given...`).
- Each criterion must be testable — it must have a concrete input, action, and
  expected output or exception.
- Criteria must not reference external services, databases, or network
  resources (the pipeline generates offline-only code).

### `tdd.yaml` requirements

- Must have a top-level `tests:` list.
- Each entry must have: `id` (unique), `name`, `description`, `inputs`,
  `expected_result`, `expected_exception`.
- `expected_exception` must be `~` or a valid Python exception class name.
- `expected_result` must be `~` when `expected_exception` is set (and vice
  versa for non-exception cases).
- There must be at least one test case per PRD acceptance criterion.
- Test case names should reference the criterion they cover — the Spec
  Validator uses semantic matching, not exact string matching.

### Validation before running

```bash
# Check YAML syntax
python -c "import yaml; yaml.safe_load(open('specs/<feature>/tdd.yaml'))"

# Preview what the spec validator will extract
python -c "
from tools.spec_validator import _extract_criteria, _extract_test_names
prd = open('specs/<feature>/prd.md').read()
tdd = open('specs/<feature>/tdd.yaml').read()
print('Criteria:')
print(_extract_criteria(prd))
print()
print('Test names:')
print(_extract_test_names(tdd))
"
```

---

## 4. Adding a new agent

### Required steps

1. **Write the system prompt** in `agent_instructions/<agent_name>.md`.

   The file must contain:
   - Role description (one paragraph)
   - Input format description
   - Step-by-step evaluation tasks
   - Hard constraints (what the agent must never do)
   - Exact output JSON schema with field descriptions
   - Example of a passing and a failing output

2. **Create the agent module** in `tools/<agent_name>.py`.

   Required structure:
   ```python
   """
   <agent_name>.py — <one-line description>
   
   <longer description if needed>
   """
   
   import logging
   from pathlib import Path
   
   from tools.config import SPECS_DIR, OUTPUT_DIR
   from tools.llm_client import LLMClient
   from tools.llm_json import parse_llm_json
   
   logger = logging.getLogger(__name__)
   
   _INSTRUCTIONS_FILE = (
       Path(__file__).resolve().parent.parent
       / "agent_instructions"
       / "<agent_name>.md"
   )
   
   
   async def run_<agent_name>(feature_name: str) -> <return_type>:
       """<docstring>"""
       ...
   ```

3. **Register the role** in `tools/config.py`:
   - Add to the `for _role, _env in [...]` loop with the new env var name.
   - Document the env var in `.env.example`.

4. **Add model hints** in `.agentconfig.yaml` under `llama.hints`.

5. **Wire into `agent_runner.py`**:
   - Import the new function.
   - Add a new step in `run_pipeline()`, wrapped in `pipeline_stage()`.
   - Append the stage name to `stages_completed` on success.
   - Add the stage to the `STAGES` list in `tools/gui.html`.

6. **Write tests** in `tests/test_<agent_name>.py`.
   - Mock `LLMClient.generate` to return controlled responses.
   - Test the JSON parsing logic.
   - Test the fallback behaviour when JSON is malformed.
   - Test hard overrides if any apply.

7. **Run the full pipeline** to verify end-to-end:
   ```bash
   python -m tools.agent_runner example_feature --wizard
   ```

8. **Ruff + tests** must both pass:
   ```bash
   python -m ruff check tools/<agent_name>.py tests/test_<agent_name>.py
   pytest tests/test_<agent_name>.py -v
   ```

---

## 5. Adding guardrail rules

### Adding a static scan pattern

Edit `guardrails/forbidden_actions.yaml`:

```yaml
forbidden:
  # ── Existing patterns ─────────────────────────────────────
  - "os.remove"
  # ...

  # ── Your new pattern ──────────────────────────────────────
  - "your_pattern"    # Brief explanation of why it's forbidden
```

**Rules for patterns:**
- Patterns are matched as plain string `in` checks per line. They are not
  regex. YAML comment annotations (text after `(`) are stripped before matching.
- Patterns must not be so broad that they catch legitimate code (e.g. a
  pattern `"open"` would flag `str.open` and many standard uses).
- After adding, run the example feature to confirm it still passes:
  ```bash
  python -m tools.agent_runner example_feature
  ```

### Adding a semantic scan rule

The semantic scan prompt lives in `agent_instructions/security_guard.md`.
To add a new detection category:

1. Add a row to the table in `security_guard.md`.
2. Ensure the developer agent instructions in `agent_instructions/developer_agent.md`
   explicitly forbid the same pattern.
3. Test by temporarily inserting the pattern into a generated file and
   verifying the semantic scan catches it.

---

## 6. Modifying the GUI

The GUI is a single self-contained file: `tools/gui.html`. It contains all
HTML, CSS, and JavaScript inline — no build step, no bundler.

### Constraints

- **No external resources.** No CDN links, no external fonts, no remote
  scripts. The GUI must work fully offline.
- **No new JavaScript frameworks.** The current code uses vanilla DOM APIs.
- **No mock data.** Every button must call a real `/api/...` endpoint that
  drives real pipeline code. Simulated responses are not acceptable.
- **No event handlers on elements.** Use `addEventListener()` exclusively —
  inline `onclick=` attributes are stripped by the content sanitiser on save.

### Adding a new step to the GUI

1. Add a `<div class="nav-item" data-step="N">` to the sidebar.
2. Add a `<section id="step-N" class="step-section">` to the main content area.
3. Add the step's data-loading logic to the `showStep(n)` function.
4. If the step requires a new API endpoint, add it to `gui_server.py`.
5. Update the `STAGES` array in the pipeline runner section if the new step
   adds a pipeline stage.

### GUI endpoint contract

The GUI communicates with `gui_server.py` exclusively via:
- `fetch('/api/...')` for JSON request/response
- `new EventSource('/api/run?f=...')` for streaming pipeline output

The JavaScript must handle errors from every `fetch` call with a `.catch()`
or `try/catch`. The `toast()` function should be used to display errors.

---

## 7. Writing tests

### Principles

- **Test one thing per test function.** Each `test_` function should assert
  a single behaviour. Use descriptive names that explain what scenario is
  being tested.
- **No LLM calls.** Patch `LLMClient.generate` for all tests that would
  otherwise make a network call.
- **No production file system side effects.** Use `tmp_path` for any test
  that writes files. Patch `tools.wizard.LOG_DIR` or
  `tools.preflight.OUTPUT_DIR` for tests that would write to production paths.
- **Test failure paths as carefully as success paths.** For every function
  with error handling, test the error case explicitly.

### Test file structure

```python
"""
tests/test_module.py — Unit tests for tools/module.py

Tests cover:
  • Brief description of each coverage area.
"""

from __future__ import annotations

# Standard library imports
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

# Third-party
import pytest

# Local
from tools.module import MyClass, my_function


class TestMyFunction:
    """Group all tests for my_function."""

    def test_happy_path(self) -> None:
        """Description of what this tests."""
        result = my_function("valid_input")
        assert result == "expected_output"

    def test_raises_on_invalid_input(self) -> None:
        with pytest.raises(ValueError, match="specific error text"):
            my_function("invalid")

    def test_with_mocked_dependency(self) -> None:
        with patch("tools.module.some_external") as mock:
            mock.return_value = "mocked_value"
            result = my_function("input")
        assert result == "processed_mocked_value"
        mock.assert_called_once_with("input")
```

### Async tests

Use `anyio.run()` directly — do not use `pytest-asyncio` markers:

```python
import anyio

class TestAsyncThing:
    def test_async_function(self) -> None:
        result_holder: list = []

        async def _run():
            result = await my_async_function()
            result_holder.append(result)

        anyio.run(_run)
        assert result_holder[0] == "expected"
```

---

## 8. Pull request checklist

Before opening a PR, verify every item:

**Code quality**
- [ ] `python -m ruff check tools/ tests/` — zero errors
- [ ] `pytest tests/ -v` — all tests pass
- [ ] All new public functions/methods have type annotations
- [ ] All new modules have a module-level docstring
- [ ] No unused imports, no dead code

**Pipeline correctness**
- [ ] `python -m tools.agent_runner example_feature --skip-preflight` — APPROVED
- [ ] No regressions in the example feature
- [ ] If agent instructions were changed: verified end-to-end with a real LLM call

**Design principles**
- [ ] No mocks in production code paths
- [ ] No hard-coded file paths (all use config constants)
- [ ] No new third-party dependencies (unless explicitly approved)
- [ ] Minimal change — no scope creep beyond the stated task

**Documentation**
- [ ] `README.md` updated if public-facing behaviour changed
- [ ] `AGENTS.md` updated if an agent's prompt or data contract changed
- [ ] `ARCHITECTURE.md` updated if module dependencies or data flows changed
- [ ] `TROUBLESHOOTING.md` updated if a new known failure mode was addressed
- [ ] `DEVELOPMENT.md` updated if setup or workflow instructions changed

**Git hygiene**
- [ ] Commit messages are in the form: `type: short description`
  - `feat:` — new capability
  - `fix:` — bug fix
  - `chore:` — tooling, dependencies, gitignore
  - `docs:` — documentation only
  - `test:` — tests only
  - `refactor:` — no behaviour change
- [ ] No `.env`, `generated/`, or `logs/` files committed

---

## 9. Spec format reference

### prd.md — required sections

| Section heading | Required? | Notes |
|---|---|---|
| `# PRD: <Feature Name>` | Yes | Title |
| `## Overview` | Yes | One paragraph |
| `## Functional Requirements` | Yes | Bulleted `FR-NN:` list |
| `## Acceptance Criteria` | **Yes** | Numbered 1. 2. 3. — checked by Spec Validator and Reviewer |
| `## Constraints` | Yes | Allowed imports, offline requirement, etc. |
| `## Out of scope` | Recommended | Prevents model from over-building |
| `## References` | Recommended | Link to tdd.yaml |

### tdd.yaml — field reference

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | Yes | Unique identifier, e.g. `TC-01` |
| `name` | string | Yes | Short description; used by Spec Validator for matching |
| `description` | string | Yes | Longer explanation of what the test verifies |
| `inputs` | mapping | Yes | Key-value pairs passed to the function under test |
| `expected_result` | scalar/null | Yes* | Set to `~` when `expected_exception` is set |
| `expected_exception` | string/null | Yes* | Python exception class name, or `~` if none expected |

*One of `expected_result` or `expected_exception` must be non-null.

---

## 10. Compliance sign-off

Before approving any AI-generated feature for use in production (or for merge
into a production branch), complete the checklist in
`guardrails/compliance_checklist.md`.

The automated gates (pytest, coverage, guardrails, spec validation) are
enforced by the pipeline. The human review gates below them require manual
inspection:

- Read every line of the generated `src/` code.
- Verify no hardcoded secrets, tokens, or credentials.
- Verify all file writes stay inside `generated/`.
- Verify no network calls exist (even via standard library).
- Verify only allowed imports are used (see `permission_boundaries.json`).
- Read the Reviewer Agent feedback for caveats about edge cases.

Sign off with your name and date in the compliance checklist before the feature
is considered production-ready.
