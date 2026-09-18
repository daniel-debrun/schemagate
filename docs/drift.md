# Drift: detection, then remediation

Senders change their exports without notice. schemagate treats that as two separate problems with
two separate commands, because the people and the risk are different: detecting a change is cheap
and safe to automate; changing how data lands in a shared table is not.

## Detection (`schemagate drift`, `schemagate.drift.detect_drift`)

A new source object is compared with the source profile stored on the approved spec it would be
materialized with. Detection only writes `schema_drift_event` rows and one audit entry; it never
touches a spec.

| event | how it is detected | severity |
|---|---|---|
| `added` | column not present in the spec's source profile | warning (data would land nowhere) |
| `removed` | spec column missing from the new file | breaking if it was mapped to a target field, else warning |
| `renamed` | a removed/added pair scoring at least 0.6 on: 0.5 x profile similarity (type, pattern signatures, value overlap) + 0.3 x header trigram similarity + 0.3 if the deterministic resolvers map the new header to the old column's target | breaking if the old column was mapped |
| `type_changed` | inferred value type differs | breaking if the new values fail the target field's compatibility check, else warning |
| `format_changed` | date layout differs (for example `mdy/` to `ymd-`) | breaking if mapped (casts are generated from the approved layout) |
| `null_rate_shift` | null rate moved by more than 0.2 | warning |
| `distribution_shift` | PSI above 0.25 against the baseline's numeric quantile bins or category frequencies | warning |

PSI is skipped when either side has fewer than 50 non-null values, for date columns, and for
identifier-like columns (distinct values above half the non-null count), where it is noise.

`schemagate materialize` runs detection automatically for objects that were not the spec's own
source and refuses to write an object with open breaking events or missing mapped columns
(`DriftBlockedError`).

## Remediation: Expand, Backfill, Switch, Contract

Remediation follows the expand/contract pattern used for online schema migrations, applied to a
mapping spec and the table it feeds.

1. **Expand** (implemented: `schemagate drift --feed F --remediate`). Proposes a new spec version with
   `change_kind = expand` and `parent_spec_id` set:
   - columns that kept their header and still pass value checks are carried over (tier `carryover`);
   - detected renames are proposed with their evidence (tier `rename`) and, like model proposals,
     require an explicit accept, override or demote;
   - new columns go through the normal resolver chain, with carried-over targets already claimed;
   - target columns whose source disappeared stay in the table as nullable columns and are listed in
     the `drift.expand_proposed` audit entry. Nothing is dropped. New extension columns are added with
     additive `ALTER TABLE ... ADD COLUMN` only.

   The new version goes through the same approval policy as any other spec.

2. **Backfill** (manual in v0.1). If historical files should be re-landed under the new version,
   re-run `schemagate materialize --spec NEW --object ID --force` for objects whose layout matches the
   new spec. The upsert key makes this idempotent. There is no automated backfill planner yet.

3. **Switch** (implemented: `schemagate switch SPEC`). Points the feed at the new version. Gated in
   `GovernanceService.switch_active`:
   - the spec must pass `verify_approved` (approval rows from enough distinct non-proposers and a
     `spec.approved` audit entry);
   - superseded or older versions cannot be switched to;
   - the previous version becomes `superseded`, open drift events of the feed against older specs are
     marked `remediated`, and a `spec.activated` audit entry records the gate.

   Approval alone does not switch. Until the switch, the feed keeps materializing with the old
   version and keeps refusing files that drifted.

4. **Contract** (not implemented). Dropping target columns that no sender populates any more, or
   tightening nullability, is deliberately left to a human-run migration: it is destructive for every
   consumer of a shared table and deserves its own review outside this tool.
