# Agentic Dev Workflow 🤖

Fully local, Ollama-powered, specification-driven AI development loop.
**Zero cloud costs. Zero mocks. Pure agentic mastery.**

## How it works

```
specs/<feature>/prd.md  +  tdd.yaml
        ↓
  [Spec Validator]  — LLM checks PRD ↔ TDD consistency
        ↓
  [Developer Agent] — best coding model generates src/ + tests/
        ↓
  [Guardrail Enforcer] — static + semantic security scan
        ↓
  [Reviewer Agent]  — runs pytest, checks coverage, evaluates criteria
        ↓
  APPROVED ✅  or  retry loop (up to max_retries)
```

## Pass / Fail criteria

| Gate | Criteria |
|---|---|
| PRD | All acceptance criteria satisfied (LLM-verified) |
| TDD | All tests pass + coverage ≥ threshold (default 80%) |
| Guardrails | No forbidden patterns, writes inside `generated/` only |

## Quick start

```bash
# 1. Pull some models
ollama pull deepseek-coder-v2:16b   # dev tasks
ollama pull llama3.1:8b             # review
ollama pull phi3:mini               # security/guard

# 2. Install deps
pip install -r requirements.txt

# 3. (Optional) copy and edit env
cp .env.example .env

# 4. Run the example feature
python tools/agent_runner.py example_feature
```

## Model auto-selection

The `ModelManager` scans your local Ollama server, scores every pulled model
by keyword hints and parameter size, and assigns the best one per role:

- **developer** → prefers `codellama`, `deepseek-coder`, `qwen2.5-coder`, etc.
- **reviewer**  → prefers mid-size general models (`llama3.1:8b`, `mistral:7b`)
- **security**  → prefers small/fast models (`phi3:mini`, `gemma2:2b`)

Override anytime via `.env` or by setting `mode: manual` in `.agentconfig.yaml`.

## Creating a new feature

```bash
mkdir specs/my_feature
cp specs/template.prd.md   specs/my_feature/prd.md
cp specs/template.tdd.yaml specs/my_feature/tdd.yaml
# edit both files, then:
python tools/agent_runner.py my_feature
```

## Architecture

| File | Purpose |
|---|---|
| `tools/config.py` | Loads YAML config + env vars |
| `tools/model_manager.py` | Discovers local Ollama models, scores + assigns by role |
| `tools/llm_client.py` | Async Ollama chat wrapper, role-aware |
| `tools/spec_validator.py` | PRD ↔ TDD consistency check via LLM |
| `tools/developer_agent.py` | Generates src/ and tests/ from specs |
| `tools/reviewer_agent.py` | Runs pytest + coverage, LLM verdict |
| `tools/guardrail_enforcer.py` | Static scan + LLM semantic security scan |
| `tools/agent_runner.py` | Master orchestrator with retry loop |

## License

MIT
