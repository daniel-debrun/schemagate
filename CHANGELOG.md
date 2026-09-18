# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses semantic versioning.

## [Unreleased]

### Added
- Results on all 551 pairs of the Valentine benchmark (`benchmarks/results/valentine.md`), with
  per-group reporting in `benchmarks/valentine.py`, which now ignores macOS metadata in the archive.
- Results with Claude Haiku 4.5 as the model tier on 20 synthetic variants and 28 Valentine pairs
  (`benchmarks/results/haiku.md`): 88.5% model-tier precision on Valentine against 52.9% for the
  heuristic, and 6 overconfident wrong mappings kept out of the batch-approval queue by the cap.
- "Where it fits" section in the README comparing schemagate with matchers, importers and data-contract
  tools.

### Fixed
- Actor names are compared case-insensitively with whitespace collapsed, so a proposer can no longer
  approve their own spec, or one person count twice, by respelling their name. Blank actors are refused.
- Materialization re-verifies the audit hash chain and refuses specs with unreviewed model or rename
  proposals, so an approval forged directly in the database is no longer accepted.
- Numbers with decimal commas (`2,5`, `1.234,50`) loaded as wrong values (25, 1.2345); they now load
  as NULL in every dialect.
- A header named like a lineage column (`_source_file`) crashed ingest and left the feed stuck;
  headers are now renamed to avoid lineage columns and each other, and a half-finished ingest is
  completed on re-ingest.
- UTF-16 CSVs with a byte-order mark are read; binary files give a clean error.

## [0.1.0] - 2026-09-16

### Added
- CSV (encoding and delimiter sniffing) and XLSX (multi-sheet) ingest with header-row detection,
  SHA-256 content hashing for incremental skip, lineage columns and column profiling.
- YAML target schemas with aliases, allowed values, patterns, units and schema-level synonyms.
- Resolver chain: exact, dictionary, synonym, then model; value-compatibility veto and downgrade;
  collision handling with extension columns.
- Model providers: offline heuristic, Anthropic, OpenAI, and a fake for tests; versioned prompt
  template; strict response validation; confidence cap enforced below every deterministic tier.
- Governance store (SQLite; Postgres via extra) with versioned specs, proposals, approvals, drift
  events and a hash-chained append-only audit log; approval policy with distinct non-proposer
  approvers and a two-approver rule for shared or published tables.
- Drift detection (added, removed, renamed, type and date-format changes, null-rate shift, PSI) and
  a separate Expand remediation with gated Switch.
- Approval-gated materialization: executed for SQLite and DuckDB; SQL text for Postgres, Snowflake
  and Databricks.
- `schemagate` CLI, end-to-end walkthrough on synthetic data, synthetic benchmark and a Valentine
  dataset loader stub.
