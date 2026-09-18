# Contributing

Thanks for considering a contribution. schemagate is small on purpose; changes that keep the
governance guarantees easy to reason about are the most welcome.

## Development setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

Tests must not need network access or API keys. Use `FakeProvider` for anything that involves a
model response.

## Invariants a change must not break

These are enforced in code and covered by tests. A pull request that weakens one needs a design
discussion first.

1. Model proposals are clamped to `ConfidencePolicy.model_cap`, and the policy refuses a cap that is
   not strictly below every deterministic tier.
2. `GovernanceService` is the only write path for governance tables, and every state change appends
   to the audit log in the same transaction.
3. Materialization re-derives approval from approval rows and the audit log (`verify_approved`);
   it never trusts a status column alone.
4. Drift detection never changes a spec; remediation is a separate command that creates a new pending
   version.

## Pull requests

- Keep commits focused and describe the behavior change, not the diff.
- Add or update tests for behavior changes; add an entry to `CHANGELOG.md` under "Unreleased".
- If you change the prompt template, bump its revision (`map_columns_v2.txt`) instead of editing
  the existing file, so recorded `prompt_version` values stay meaningful.
- Benchmark numbers in the README must come from a run you did, with the command shown.
