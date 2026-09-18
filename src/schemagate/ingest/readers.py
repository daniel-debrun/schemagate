from __future__ import annotations

import csv
import io
from datetime import date, datetime, time
from pathlib import Path

from schemagate.errors import IngestError

ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
DELIMITERS = ",;\t|"


def decode_bytes(data: bytes) -> tuple[str, str]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 with a byte-order mark (Excel "Unicode text")
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError as exc:
            raise IngestError(f"file has a UTF-16 byte-order mark but does not decode: {exc}") from exc
    if b"\x00" in data:
        raise IngestError("file contains NUL bytes; it is not a text CSV (binary, or UTF-16 without a BOM)")
    for enc in ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    raise IngestError("could not decode file")  # pragma: no cover - latin-1 always decodes


def sniff_delimiter(text: str) -> str:
    sample_lines = [ln for ln in text.splitlines()[:50] if ln.strip()]
    sample = "\n".join(sample_lines)
    try:
        return csv.Sniffer().sniff(sample, delimiters=DELIMITERS).delimiter
    except csv.Error:
        pass
    best, best_score = ",", -1.0
    for d in DELIMITERS:
        counts = [ln.count(d) for ln in sample_lines]
        nonzero = [c for c in counts if c]
        if not nonzero:
            continue
        mode = max(set(nonzero), key=nonzero.count)
        score = nonzero.count(mode) * mode
        if score > best_score:
            best, best_score = d, score
    return best


def read_csv(path: Path) -> tuple[list[list[str]], str, str]:
    data = path.read_bytes()
    text, encoding = decode_bytes(data)
    delimiter = sniff_delimiter(text)
    rows = [list(r) for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    return rows, encoding, delimiter


def _cell_to_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.time() == time(0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read_xlsx(path: Path) -> dict[str, list[list[str]]]:
    try:
        import openpyxl
    except ImportError as exc:
        raise IngestError(
            "reading .xlsx requires the 'excel' extra: pip install 'schemadriftgate[excel]'"
        ) from exc
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise IngestError(f"cannot open workbook {path}: {exc}") from exc
    sheets: dict[str, list[list[str]]] = {}
    try:
        for ws in wb.worksheets:
            rows = [[_cell_to_str(c) for c in row] for row in ws.iter_rows(values_only=True)]
            while rows and not any(rows[-1]):
                rows.pop()
            sheets[ws.title] = rows
    finally:
        wb.close()
    return sheets
