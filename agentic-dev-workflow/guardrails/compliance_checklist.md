# Compliance Checklist

Run through this before approving any generated feature for merge.

## Automated gates (enforced by pipeline)

- [ ] Guardrail static scan: 0 violations
- [ ] Guardrail semantic (LLM) scan: `safe: true`
- [ ] All pytest tests: PASSED
- [ ] Code coverage: ≥ threshold (see `.agentconfig.yaml`)
- [ ] PRD acceptance criteria: all satisfied (Reviewer Agent PASS)
- [ ] TDD test cases: all present and passing

## Human review gates (optional but recommended for production)

- [ ] Reviewed generated `src/` code for logic correctness
- [ ] Verified no hardcoded secrets or credentials
- [ ] Confirmed file writes stay inside `generated/`
- [ ] Checked that no network calls are present
- [ ] Ensured only allowed imports are used
- [ ] Read the Reviewer Agent feedback for any caveats

## Sign-off

| Role | Name | Date |
|---|---|---|
| Developer | | |
| Reviewer  | | |
| Security  | | |
