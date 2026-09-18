"""Regression tests for inputs found by adversarial testing."""

from __future__ import annotations

import sqlite3

import pytest

from schemagate.errors import ApprovalPolicyError, IngestError, UnapprovedSpecError
from schemagate.ingest.loader import LINEAGE_COLUMNS, dedupe_headers, ingest_file
from schemagate.materialize.dialects import SQLiteDialect

from .conftest import propose_spec


@pytest.mark.parametrize("variant", ["Alice", " alice", "ALICE ", "alice\t"])
def test_proposer_cannot_review_under_a_respelled_name(service, invoice_schema, variant):
    spec_id, _ = propose_spec(service, invoice_schema, "northwind")
    with pytest.raises(ApprovalPolicyError, match="cannot review"):
        service.approve_spec(spec_id, variant)


def test_respelled_approver_counts_once(service, invoice_schema):
    published = invoice_schema.model_copy(update={"published": True, "name": "pub", "table": "pub_lines"})
    spec_id, _ = propose_spec(service, published, "f1")
    assert service.approve_spec(spec_id, "bob") == "pending"
    with pytest.raises(ApprovalPolicyError, match="already approved"):
        service.approve_spec(spec_id, " Bob")


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_actors_are_refused(service, invoice_schema, blank):
    spec_id, _ = propose_spec(service, invoice_schema, "northwind")
    with pytest.raises(ApprovalPolicyError, match="non-blank"):
        service.approve_spec(spec_id, blank)
    assert service.approvals(spec_id) == []


def test_materialize_refuses_approval_forged_in_the_database(service, invoice_schema):
    spec_id, _ = propose_spec(service, invoice_schema, "northwind")
    db = service.db
    db.execute("INSERT INTO mapping_approval (spec_id, approver, decision, comment, created_at)"
               " VALUES (?, 'ghost', 'approve', '', '2026-01-01')", (spec_id,))
    db.execute("UPDATE mapping_spec SET status = 'approved' WHERE id = ?", (spec_id,))
    db.execute("INSERT INTO mapping_audit (ts, actor, action, entity_type, entity_id, payload_json,"
               " prev_hash, hash) VALUES ('2026-01-01', 'ghost', 'spec.approved', 'mapping_spec', ?,"
               " '{}', 'x', 'forged')", (str(spec_id),))
    with pytest.raises(UnapprovedSpecError, match="audit log fails verification"):
        service.verify_approved(spec_id)


def test_materialize_refuses_unreviewed_model_proposals(service, invoice_schema):
    spec_id, _ = propose_spec(service, invoice_schema, "northwind")
    service.approve_spec(spec_id, "bob")
    service.verify_approved(spec_id)
    service.db.execute("UPDATE mapping_proposal SET tier = 'model', status = 'proposed'"
                       " WHERE id = (SELECT MIN(id) FROM mapping_proposal WHERE spec_id = ?)", (spec_id,))
    with pytest.raises(UnapprovedSpecError, match="nobody reviewed"):
        service.verify_approved(spec_id)


@pytest.mark.parametrize(("raw", "expected"), [
    ("2,5", None), ("1.234,50", None), ("1,23.5", None), ("12,3456", None), (",5", None), ("5,", None),
    ("1,234.50", 1234.5), ("$1,234,567.89", 1234567.89), ("(12.00)", -12.0), ("3,000 USD", 3000.0),
    ("45%", 45.0), ("12", 12.0),
])
def test_decimal_commas_become_null_not_wrong_numbers(raw, expected):
    d = SQLiteDialect()
    conn = sqlite3.connect(":memory:")
    conn.execute('CREATE TABLE t ("v" TEXT)')
    conn.execute("INSERT INTO t VALUES (?)", (raw,))
    expr = f"CASE WHEN {d.comma_grouping_ok('v')} THEN {d.try_number(d.numeric_text('v'), False)} END"
    (got,) = conn.execute(f"SELECT {expr} FROM t").fetchone()
    assert got == pytest.approx(expected) if expected is not None else got is None


def test_headers_never_collide_with_lineage_or_each_other():
    out = dedupe_headers(["_source_file", "a", "A", "a_2", "", "_ROW_NUMBER"])
    assert out == ["_source_file_2", "a", "A_2", "a_2_2", "column_5", "_ROW_NUMBER_2"]
    assert len({c.lower() for c in out} | set(LINEAGE_COLUMNS)) == len(out) + len(LINEAGE_COLUMNS)


def test_utf16_csv_with_bom_is_read(tmp_path):
    path = tmp_path / "export.csv"
    path.write_bytes("id\tamount\n1\t2.5\n".encode("utf-16"))
    (table,) = ingest_file(path)
    assert table.columns == ["id", "amount"] and table.rows == [["1", "2.5"]]
    assert table.encoding == "utf-16"


def test_binary_file_is_a_clean_ingest_error(tmp_path):
    path = tmp_path / "garbage.csv"
    path.write_bytes(b"\x00\x01\x02binary\x00stuff")
    with pytest.raises(IngestError, match="NUL bytes"):
        ingest_file(path)
