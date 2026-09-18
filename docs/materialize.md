# Materialization and SQL dialects

`build_plan(service, spec_id, source_object_id)` is the only way to obtain a plan. It calls
`verify_approved` first (raising `UnapprovedSpecError`), then refuses objects with open breaking drift,
missing mapped columns, or a spec that does not map the schema's key fields (`DriftBlockedError`).

A plan has one output column per schema field (NULL when unmapped), one text column per extension
column, the lineage columns (`_source_file`, `_sheet`, `_row_number`, `_content_hash`, `_ingested_at`)
and `_feed`, `_spec_id`.

The generated SQL:

1. `CREATE TABLE IF NOT EXISTS` with typed columns;
2. additive `ALTER TABLE ... ADD COLUMN` for extension columns;
3. a staged select from the raw text table that applies typed casts, drops rows whose key casts to
   NULL, and keeps the last row per key (`ROW_NUMBER() OVER (PARTITION BY key ORDER BY _row_number DESC)`);
4. an upsert on the schema key.

Casts are generated from the approved source profile: currency symbols, ISO codes, thousands
separators and `%` are stripped (percent values stay in percent units), accounting negatives `(12.00)`
become `-12.00`, dates use the approved layout (`ymd-`, `mdy/`, `dmy.`, `dmy-mon`, ...), booleans accept
true/t/yes/y/1 and false/f/no/n/0. Values that do not parse become NULL rather than failing the load.

| dialect | upsert | casts | status |
|---|---|---|---|
| SQLite | `INSERT ... ON CONFLICT DO UPDATE` + unique index | GLOB-guarded CAST, substr/printf date assembly | executed in tests and the walkthrough |
| DuckDB | `MERGE INTO` | `TRY_CAST`, `TRY_STRPTIME` | executed in tests (requires `duckdb` 1.4+ for MERGE) |
| Postgres | `INSERT ... ON CONFLICT DO UPDATE` + unique index | regex-guarded CAST, `TO_DATE` | text generation only |
| Snowflake | `MERGE INTO` | `TRY_TO_NUMBER`, `TRY_TO_DATE` | text generation only |
| Databricks / Delta | `MERGE INTO`, `USING DELTA` | `try_cast`, `try_to_timestamp` | text generation only; not run against a live workspace |

Identifiers are double-quoted (backticks on Databricks). On Snowflake that makes them case-sensitive;
keep target field names lower-case and query them quoted, or post-process the SQL. The staging table
(`raw_<feed>_<object id>`, all text columns plus lineage) must exist in the target warehouse; for
SQLite the CLI creates it at ingest.
