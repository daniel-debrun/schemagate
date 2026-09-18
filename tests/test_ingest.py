from __future__ import annotations

import pytest

from schemagate.errors import IngestError
from schemagate.ingest import detect_header_row, ingest_file, profile_column
from schemagate.ingest.values import DateFormat, detect_date_format, infer_type, pattern_signature

from .conftest import EXAMPLES


def test_header_detection_skips_title_and_blank_rows():
    grid = [
        ["Acme Corp - Supplier Export", "", "", ""],
        ["Generated 2026-08-01", "", "", ""],
        ["", "", "", ""],
        ["Invoice", "Date", "Amount", "Status"],
        ["A-1", "2026-07-01", "10.50", "open"],
        ["A-2", "2026-07-02", "11.00", "paid"],
        ["A-3", "2026-07-03", "9.99", "open"],
    ]
    assert detect_header_row(grid) == 3


def test_header_detection_with_numeric_looking_title_row():
    grid = [
        ["Report", "2026", "", "", ""],
        ["Site", "Enrolled", "Screened", "Visit Date", "Region"],
        ["S-01", "12", "30", "01/02/2026", "North"],
        ["S-02", "7", "18", "01/09/2026", "South"],
        ["S-03", "3", "11", "01/16/2026", "East"],
    ]
    assert detect_header_row(grid) == 1


def test_header_is_first_row_for_clean_table():
    grid = [["a", "b"], ["1", "x"], ["2", "y"]]
    assert detect_header_row(grid) == 0


def test_csv_sniffs_semicolon_and_cp1252(tmp_path):
    path = tmp_path / "export.csv"
    path.write_bytes("Title line\nNom;Montant;Date\nCédar;1,50;2026-01-02\nÉlan;2,00;2026-01-03\n"
                     .encode("cp1252"))
    (table,) = ingest_file(path)
    assert table.delimiter == ";"
    assert table.encoding == "cp1252"
    assert table.columns == ["Nom", "Montant", "Date"]
    assert table.rows[0][0] == "Cédar"
    assert table.row_numbers == [3, 4]


def test_lineage_records_and_content_hash(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("x,y\n1,2\n3,4\n")
    (t1,) = ingest_file(path)
    (t2,) = ingest_file(path)
    assert t1.content_hash == t2.content_hash and len(t1.content_hash) == 64
    rec = t1.records()[1]
    assert rec["x"] == "3"
    assert rec["_source_file"] == "a.csv"
    assert rec["_row_number"] == 3
    assert rec["_content_hash"] == t1.content_hash
    assert set(rec) >= {"_sheet", "_ingested_at"}
    path.write_text("x,y\n1,2\n3,5\n")
    (t3,) = ingest_file(path)
    assert t3.content_hash != t1.content_hash


def test_duplicate_and_blank_headers_are_made_unique(tmp_path):
    path = tmp_path / "d.csv"
    path.write_text("id,,id\n1,a,2\n")
    (t,) = ingest_file(path)
    assert t.columns == ["id", "column_2", "id_2"]


def test_xlsx_multi_sheet(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cover"
    ws.append(["Quarterly site report"])
    data = wb.create_sheet("Sites")
    data.append(["Clinical site summary"])
    data.append([])
    data.append(["Site ID", "Enrolled", "Last Visit"])
    import datetime

    data.append(["S-01", 12, datetime.datetime(2026, 1, 2)])
    data.append(["S-02", 7.0, datetime.datetime(2026, 1, 9)])
    path = tmp_path / "r.xlsx"
    wb.save(path)
    tables = {t.sheet: t for t in ingest_file(path)}
    assert set(tables) == {"Cover", "Sites"}
    sites = tables["Sites"]
    assert sites.header_row == 2
    assert sites.rows == [["S-01", "12", "2026-01-02"], ["S-02", "7", "2026-01-09"]]
    assert [p.inferred_type for p in sites.profiles] == ["string", "int", "date"]
    only = ingest_file(path, sheets=["Sites"])
    assert [t.sheet for t in only] == ["Sites"]


def test_example_xlsx_title_rows():
    pytest.importorskip("openpyxl")
    tables = ingest_file(EXAMPLES / "data" / "bluepeak" / "billing_2026-07.xlsx", sheets=["Line Items"])
    assert tables[0].header_row == 3
    assert tables[0].columns[0] == "Inv #"


def test_unsupported_and_missing_files(tmp_path):
    with pytest.raises(IngestError):
        ingest_file(tmp_path / "nope.csv")
    p = tmp_path / "x.parquet"
    p.write_bytes(b"PAR1")
    with pytest.raises(IngestError):
        ingest_file(p)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["1", "2", "-3", "1,000"], "int"),
        (["1.5", "2", "3.25"], "float"),
        (["$1,200.00", "$3.50", "(4.00)"], "currency"),
        (["12%", "0.5%", "100 %"], "percent"),
        (["yes", "no", "Y"], "bool"),
        (["2026-01-02", "2026-02-03"], "date"),
        (["03/15/2026", "04/01/2026"], "date"),
        (["INV-1", "INV-2"], "string"),
        (["", "n/a", "NULL"], "empty"),
    ],
)
def test_infer_type(values, expected):
    assert infer_type(values)[0] == expected


def test_type_inference_tolerates_a_few_dirty_cells():
    values = [str(i) for i in range(40)] + ["see note"]
    assert infer_type(values)[0] == "int"


def test_date_format_disambiguation():
    assert detect_date_format(["25/12/2026", "01/02/2026"]) == DateFormat("dmy", "/")
    assert detect_date_format(["12/25/2026", "01/02/2026"]) == DateFormat("mdy", "/")
    assert detect_date_format(["05-Jan-2026", "17-Mar-2026"]) == DateFormat("dmy", "-", True)
    assert DateFormat.from_token("dmy-mon") == DateFormat("dmy", "-", True)


def test_profile_column_statistics():
    prof = profile_column("amount", 0, ["$1.00", "$2.00", "", "$2.00", "n/a"])
    assert prof.inferred_type == "currency"
    assert prof.null_rate == 0.4
    assert prof.distinct_count == 2
    assert prof.sample_values == ["$1.00", "$2.00"]
    assert prof.parse_rates["number"] == 1.0
    assert prof.numeric_distribution is not None
    assert pattern_signature("INV-00123") == "A-9"
    assert pattern_signature("ab 12") == "a 9"
