from __future__ import annotations

import re
import sqlite3

import pytest

from schemagate.cli import main

from .conftest import EXAMPLES

DATA = EXAMPLES / "data"


def run(capsys, project, *args, actor="dana"):
    code = main(["-C", str(project), "--actor", actor, *map(str, args)])
    out = capsys.readouterr()
    return code, out.out + out.err


def proposal_ids(output, tier):
    return [m.group(1) for m in re.finditer(rf"^(\d+)\s+.*?\s{tier}\s", output, re.MULTILINE)]


def test_end_to_end_walkthrough(tmp_path, capsys):
    pytest.importorskip("openpyxl")
    p = tmp_path / "proj"
    assert run(capsys, p, "init", "--schema", EXAMPLES / "schemas" / "supplier_invoice.yaml")[0] == 0

    code, out = run(capsys, p, "ingest", "--feed", "cedar", "--schema", "supplier_invoice",
                    DATA / "cedar" / "cedar_export_2026-07.csv")
    assert code == 0 and "ingested" in out
    code, out = run(capsys, p, "ingest", "--feed", "cedar", DATA / "cedar" / "cedar_export_2026-07.csv")
    assert "skipped (same content hash)" in out

    code, out = run(capsys, p, "materialize", "--feed", "cedar")
    assert code == 3 and "no approved spec" in out

    code, out = run(capsys, p, "propose", "--feed", "cedar")
    assert code == 0 and "proposed spec 1" in out
    code, out = run(capsys, p, "review", "--spec", "1")
    model_ids = proposal_ids(out, "model")
    assert model_ids, out

    code, out = run(capsys, p, "approve", "1")
    assert code == 2 and "cannot review" in out
    code, out = run(capsys, p, "approve", "1", actor="omar")
    assert code == 2 and "need an explicit accept" in out
    assert run(capsys, p, "accept", *model_ids, actor="omar")[0] == 0
    code, out = run(capsys, p, "approve", "1", actor="omar")
    assert code == 0 and "status=approved" in out

    code, out = run(capsys, p, "materialize", "--feed", "cedar", actor="omar")
    assert code == 0 and "upserted 29 rows" in out

    run(capsys, p, "ingest", "--feed", "cedar", DATA / "cedar" / "cedar_export_2026-08.csv")
    code, out = run(capsys, p, "materialize", "--feed", "cedar", actor="omar")
    assert code == 3 and "blocked" in out
    code, out = run(capsys, p, "drift", "--feed", "cedar")
    assert code == 1 and "format_changed" in out and "renamed" in out

    code, out = run(capsys, p, "drift", "--feed", "cedar", "--remediate")
    assert code == 0 and "proposed expand spec 2" in out
    code, out = run(capsys, p, "review", "--spec", "2")
    rename_ids = proposal_ids(out, "rename")
    assert len(rename_ids) == 1
    run(capsys, p, "accept", *rename_ids, actor="omar")
    code, out = run(capsys, p, "approve", "2", actor="omar")
    assert "run `schemagate switch 2`" in out

    code, out = run(capsys, p, "switch", "2", actor="lee")
    assert code == 0
    code, out = run(capsys, p, "materialize", "--feed", "cedar", actor="omar")
    assert code == 0 and "upserted 27 rows" in out and "spec 2 v2" in out

    code, out = run(capsys, p, "materialize", "--feed", "cedar", "--object", "2", "--dialect", "databricks")
    assert code == 0 and "USING DELTA" in out

    code, out = run(capsys, p, "audit", "verify")
    assert code == 0 and "audit chain OK" in out
    code, out = run(capsys, p, "audit", "show", "--entity-type", "mapping_spec", "--entity-id", "2")
    assert "spec.activated" in out

    conn = sqlite3.connect(p / ".schemagate" / "warehouse.db")
    count, specs, dates = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT _spec_id), SUM(invoice_date IS NULL) FROM invoice_lines").fetchone()
    assert (count, specs, dates) == (56, 2, 0)
    assert conn.execute("SELECT COUNT(ext_freight_charge) FROM invoice_lines").fetchone()[0] == 27

    code, out = run(capsys, p, "status")
    assert "cedar" in out


def test_missing_project_is_a_clean_error(tmp_path, capsys):
    code, out = run(capsys, tmp_path, "status")
    assert code == 2 and "schemagate init" in out
