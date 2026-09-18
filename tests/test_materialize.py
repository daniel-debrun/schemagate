from __future__ import annotations

import sqlite3

import pytest

from schemagate.errors import DriftBlockedError, UnapprovedSpecError
from schemagate.materialize import build_plan, get_dialect, load_raw_table, materialize, render_sql
from schemagate.materialize.plan import raw_table_name

from .conftest import INVOICE_COLUMNS, invoice_rows, make_table, propose_spec


def _setup(service, schema, conn, rows=None, approve=True):
    rows = rows or invoice_rows()
    spec_id, obj = propose_spec(service, schema, "northwind", rows=rows)
    table = make_table(INVOICE_COLUMNS, rows, digest="northwind" + "x" * 55)
    raw = raw_table_name("northwind", obj)
    load_raw_table(conn, raw, table)
    service.set_raw_table(obj, raw)
    if approve:
        service.approve_spec(spec_id, "bob")
    return spec_id, obj


def test_unapproved_spec_is_refused_before_touching_the_warehouse(service, invoice_schema):
    conn = sqlite3.connect(":memory:")
    spec_id, obj = _setup(service, invoice_schema, conn, approve=False)
    with pytest.raises(UnapprovedSpecError):
        materialize(service, conn, spec_id, obj, "bob")
    for dialect in ("sqlite", "postgres", "snowflake", "databricks", "duckdb"):
        with pytest.raises(UnapprovedSpecError):
            render_sql(service, spec_id, obj, dialect)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "invoice_lines" not in tables
    assert not service.audit.entries(action="spec.materialized")


def test_sqlite_rows_are_typed_and_lineage_is_kept(service, invoice_schema):
    conn = sqlite3.connect(":memory:")
    rows = invoice_rows()
    rows.append(["", "1", "2026-07-09", "V-1", "3", "$1.00", "$3.00", "USD", "TOR"])
    rows.append(["INV-2", "2", "2026-07-10", "V-1", "not a number", "(12.00)", "$0", "USD", "MTL"])
    spec_id, obj = _setup(service, invoice_schema, conn, rows=rows)
    result = materialize(service, conn, spec_id, obj, "carol")
    assert result.rows_staged == 6
    assert result.rows_rejected_null_key == 1
    assert result.rows_written == 4
    got = conn.execute(
        "SELECT invoice_number, line_number, invoice_date, supplier_id, quantity, unit_price,"
        " line_total, currency, ext_warehouse, _row_number, _feed, _spec_id FROM invoice_lines"
        " ORDER BY invoice_number, line_number").fetchall()
    assert got[0] == ("INV-1", 1, "2026-07-01", "V-1", 1.0, 1250.5, 1250.5, "USD", "TOR", 2, "northwind", spec_id)
    last = got[-1]
    assert last[:2] == ("INV-2", 2)
    assert last[4] is None
    assert last[5] == -12.0
    assert last[8] == "MTL"
    assert last[9] == 7
    assert service.audit.entries(action="spec.materialized")[0]["payload"]["rows"] == 4


def test_upsert_is_idempotent_and_updates(service, invoice_schema):
    conn = sqlite3.connect(":memory:")
    spec_id, obj = _setup(service, invoice_schema, conn)
    materialize(service, conn, spec_id, obj, "carol")
    materialize(service, conn, spec_id, obj, "carol")
    assert conn.execute("SELECT COUNT(*) FROM invoice_lines").fetchone()[0] == 4
    conn.execute(f'UPDATE "{raw_table_name("northwind", obj)}" SET "Qty" = \'99\' WHERE "_row_number" = 2')
    materialize(service, conn, spec_id, obj, "carol")
    assert conn.execute("SELECT COUNT(*) FROM invoice_lines").fetchone()[0] == 4
    assert conn.execute("SELECT quantity FROM invoice_lines WHERE _row_number = 2").fetchone()[0] == 99


@pytest.mark.parametrize(("fmt_rows", "expected"), [
    (["03/15/2026", "4/1/2026"], ["2026-03-15", "2026-04-01"]),
    (["15.03.2026", "01.04.2026"], ["2026-03-15", "2026-04-01"]),
    (["15-Mar-2026", "01-Apr-2026"], ["2026-03-15", "2026-04-01"]),
])
def test_sqlite_date_casts(service, invoice_schema, fmt_rows, expected):
    conn = sqlite3.connect(":memory:")
    rows = invoice_rows(2)
    for r, d in zip(rows, fmt_rows):
        r[2] = d
    spec_id, obj = _setup(service, invoice_schema, conn, rows=rows)
    materialize(service, conn, spec_id, obj, "carol")
    got = [r[0] for r in conn.execute("SELECT invoice_date FROM invoice_lines ORDER BY _row_number")]
    assert got == expected


def test_missing_mapped_column_blocks(service, invoice_schema):
    conn = sqlite3.connect(":memory:")
    spec_id, _ = _setup(service, invoice_schema, conn)
    spec = service.spec(spec_id)
    cols = [c for c in INVOICE_COLUMNS if c != "Qty"]
    rows = [[v for c, v in zip(INVOICE_COLUMNS, r) if c != "Qty"] for r in invoice_rows()]
    obj2, _ = service.record_source_object(spec.feed_id, make_table(cols, rows, digest="9" * 64), "alice")
    with pytest.raises(DriftBlockedError, match="Qty"):
        build_plan(service, spec_id, obj2)


def test_open_breaking_drift_blocks(service, invoice_schema):
    conn = sqlite3.connect(":memory:")
    spec_id, obj = _setup(service, invoice_schema, conn)
    spec = service.spec(spec_id)
    service.record_drift_events(spec.feed_id, spec_id, obj, [
        {"kind": "format_changed", "severity": "breaking", "source_column": "Invoice Date"}], "alice")
    with pytest.raises(DriftBlockedError, match="format_changed"):
        materialize(service, conn, spec_id, obj, "carol")


def test_duckdb_execution_matches_sqlite(service, invoice_schema):
    duckdb = pytest.importorskip("duckdb")
    from schemagate.materialize.executor import execute_plan

    conn = duckdb.connect()
    spec_id, obj = _setup(service, invoice_schema, sqlite3.connect(":memory:"))
    table = make_table(INVOICE_COLUMNS, invoice_rows(), digest="northwind" + "x" * 55)
    load_raw_table(conn, raw_table_name("northwind", obj), table)
    plan = build_plan(service, spec_id, obj)
    execute_plan(conn, plan, engine="duckdb")
    execute_plan(conn, plan, engine="duckdb")
    got = conn.execute("SELECT invoice_number, line_number, CAST(invoice_date AS VARCHAR), quantity,"
                       " unit_price, ext_warehouse FROM invoice_lines ORDER BY _row_number").fetchall()
    assert len(got) == 4
    assert got[0][:3] == ("INV-1", 1, "2026-07-01")
    assert float(got[0][4]) == 1250.5
    assert got[0][5] == "TOR"


@pytest.mark.parametrize(("dialect", "needles"), [
    ("postgres", ["ON CONFLICT", "TO_DATE", "NUMERIC(38,6)", "ADD COLUMN IF NOT EXISTS"]),
    ("snowflake", ["MERGE INTO", "TRY_TO_NUMBER", "TRY_TO_DATE", "not executed"]),
    ("databricks", ["MERGE INTO `invoice_lines`", "USING DELTA", "try_to_timestamp", "ADD COLUMNS"]),
    ("duckdb", ["MERGE INTO", "TRY_STRPTIME", "TRY_CAST"]),
    ("sqlite", ["ON CONFLICT", "CREATE UNIQUE INDEX"]),
])
def test_text_dialects(service, invoice_schema, dialect, needles):
    conn = sqlite3.connect(":memory:")
    spec_id, obj = _setup(service, invoice_schema, conn)
    script = render_sql(service, spec_id, obj, dialect).script()
    for needle in needles:
        assert needle in script
    assert "ROW_NUMBER() OVER" in script
    assert get_dialect(dialect).name == dialect
