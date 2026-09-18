# Governance model

## Entities

| table | purpose |
|---|---|
| `source_feed` | one sender stream bound to one target schema; holds `active_spec_id` |
| `source_object` | one ingested sheet: file name, sheet, SHA-256 content hash, header row, profile, raw staging table |
| `mapping_spec` | versioned mapping for a feed: status, `change_kind`, parent version, required approvals, source profile |
| `mapping_proposal` | one row per source column: target or extension column, tier, confidence, rationale, evidence, proposer, prompt version, model id, conflict record, review status |
| `mapping_approval` | one decision per (spec, person) |
| `mapping_audit` | append-only, hash-chained log of every state change |
| `schema_drift_event` | detected drift, open or remediated |
| `materialization` | which spec wrote which source object to which table |

## State machine

```
spec:      pending --approve (policy satisfied)--> approved --switch--> (active) --newer switch--> superseded
           pending --reject--> rejected
proposal:  proposed --accept--> accepted
           proposed --demote--> demoted        (target cleared, ext_ column assigned)
           proposed --override--> overridden   (tier=human, confidence 1.0)
```

Proposals are frozen once the spec leaves `pending`.

## Approval policy (`ApprovalPolicy`)

- The proposer of a spec cannot approve it, accept or override its proposals
  (`allow_self_approval=False` by default).
- The same person cannot approve a spec twice.
- A spec needs `new_table_approvers` (default 1) approvals if its target table is private to the feed,
  and `shared_table_approvers` (default 2) if the schema is marked `published: true` or another feed
  already has an approved spec for the table. The rule is re-evaluated at every approval, so a table
  that became shared while a spec was pending raises the bar.
- Model-tier and rename-tier proposals need an explicit accept, override or demote before any approval
  is accepted. Deterministic proposals are covered by the spec approval.

## Why the materializer does not trust `status`

`verify_approved` recomputes approval from `mapping_approval` rows (excluding the proposer) against the
spec's required count and requires a `spec.approved` entry in the audit log. Setting
`status='approved'` with SQL is not enough (see `tests/test_governance.py`).

## Audit chain

Each entry stores `prev_hash` and `hash = sha256(canonical_json(prev_hash, ts, actor, action,
entity_type, entity_id, payload))`. SQLite triggers (and a Postgres trigger function) reject UPDATE and
DELETE on the table. `schemagate audit verify` recomputes the chain and reports the first broken entry.

A chain cannot detect removal of its newest entries by itself. Record the head hash printed by
`audit verify` somewhere outside the database (a ticket, a commit, an object store with retention) and
pass it back with `--expect-head` to detect truncation.

What gets recorded for a model proposal: `proposer` (`model:<model id>`), `model_id`, `prompt_version`
(template name, revision and a hash of the template text), the rationale, cited evidence, the raw
confidence the model returned, and any validation issues (clamped confidence, rejected hallucinated
target).

## Identity

Actors are asserted strings (`--actor`, `SCHEMAGATE_ACTOR`, or the OS user). v0.1 has no
authentication; the policy guarantees distinct names, not distinct humans. In a deployment, put the CLI
or the service behind something that sets the actor from an authenticated identity.
