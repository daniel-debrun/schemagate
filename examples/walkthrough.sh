#!/usr/bin/env bash
# Runs the walkthrough end to end in ./walkthrough-run (from the repository root).
set -uo pipefail
P=walkthrough-run
rm -rf "$P"
sg() { echo; printf '$ SCHEMAGATE_ACTOR=%s schemagate' "${SCHEMAGATE_ACTOR:-}"; printf ' %q' "$@"; echo; schemagate -C "$P" "$@"; echo "[exit $?]"; }

SCHEMAGATE_ACTOR=dana sg init --schema examples/schemas/supplier_invoice.yaml
export SCHEMAGATE_ACTOR=dana
sg ingest --feed northwind --schema supplier_invoice examples/data/northwind/invoices_2026-07.csv
sg ingest --feed bluepeak --schema supplier_invoice --sheet "Line Items" examples/data/bluepeak/billing_2026-07.xlsx
sg ingest --feed cedar --schema supplier_invoice examples/data/cedar/cedar_export_2026-07.csv
sg propose --feed northwind
sg propose --feed bluepeak
sg propose --feed cedar
sg review --spec 1
sg approve 1
sg materialize --feed northwind
export SCHEMAGATE_ACTOR=omar
sg approve 1
sg accept 11
sg approve 1
sg materialize --feed northwind
sg review --spec 3
sg accept 33 35 37 39 40 41 42
sg approve 3
sg approve 3
SCHEMAGATE_ACTOR=lee sg approve 3
sg review --spec 2
sg override 19 supplier_name --reason "sender's 'Supplier' column holds the trading name"
sg accept 24
sg approve 2
SCHEMAGATE_ACTOR=lee sg approve 2
sg materialize --feed bluepeak
sg materialize --feed cedar
export SCHEMAGATE_ACTOR=dana
sg ingest --feed northwind examples/data/northwind/invoices_2026-08.csv examples/data/northwind/invoices_2026-07.csv
sg ingest --feed bluepeak --sheet "Line Items" examples/data/bluepeak/billing_2026-08.xlsx
sg ingest --feed cedar examples/data/cedar/cedar_export_2026-08.csv
export SCHEMAGATE_ACTOR=omar
sg materialize --feed northwind
sg materialize --feed bluepeak
sg materialize --feed cedar
sg drift --feed cedar
SCHEMAGATE_ACTOR=dana sg drift --feed cedar --remediate
sg review --spec 4
sg accept 52 --comment "same values under a new header"
sg approve 4
SCHEMAGATE_ACTOR=lee sg approve 4
sg materialize --feed cedar
SCHEMAGATE_ACTOR=lee sg switch 4
sg materialize --feed cedar
sg materialize --feed cedar --object 6 --dialect postgres --out "$P/cedar_v2_postgres.sql"
sg status
sg audit show --entity-type mapping_spec --entity-id 4
sg audit verify
echo
echo "\$ sqlite3 $P/.schemagate/warehouse.db 'SELECT _feed, _spec_id, COUNT(*), ...'"
python - <<'PY'
import sqlite3
c = sqlite3.connect("walkthrough-run/.schemagate/warehouse.db")
rows = c.execute(
    "SELECT _feed, _spec_id, COUNT(*), ROUND(SUM(line_total), 2), MIN(invoice_date), MAX(invoice_date),"
    " SUM(unit_price IS NULL), COUNT(ext_warehouse), COUNT(ext_freight_charge)"
    " FROM invoice_lines GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
print("feed       spec  rows  sum_line_total  min_date    max_date    null_price  ext_warehouse  ext_freight")
for r in rows:
    print(f"{r[0]:<10} {r[1]:<5} {r[2]:<5} {r[3]:<15} {r[4]}  {r[5]}  {r[6]:<10}  {r[7]:<13}  {r[8]}")
PY
