from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from schemagate.errors import IngestError
from schemagate.ingest.header import detect_header_row
from schemagate.ingest.profile import ColumnProfile, profile_table
from schemagate.ingest.readers import read_csv, read_xlsx
from schemagate.ingest.values import is_null

LINEAGE_COLUMNS = ("_source_file", "_sheet", "_row_number", "_content_hash", "_ingested_at")


@dataclass
class IngestedTable:
    source_file: str
    sheet: str | None
    header_row: int
    columns: list[str]
    rows: list[list[str]]
    row_numbers: list[int]
    content_hash: str
    ingested_at: str
    encoding: str | None = None
    delimiter: str | None = None
    profiles: list[ColumnProfile] = field(default_factory=list)

    def lineage(self, i: int) -> dict[str, str | int | None]:
        return {
            "_source_file": self.source_file,
            "_sheet": self.sheet,
            "_row_number": self.row_numbers[i],
            "_content_hash": self.content_hash,
            "_ingested_at": self.ingested_at,
        }

    def records(self) -> list[dict[str, object]]:
        out = []
        for i, row in enumerate(self.rows):
            rec: dict[str, object] = dict(zip(self.columns, row))
            rec.update(self.lineage(i))
            out.append(rec)
        return out

    def column_values(self, name: str) -> list[str]:
        j = self.columns.index(name)
        return [r[j] for r in self.rows]


def content_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def dedupe_headers(raw: list[str]) -> list[str]:
    """Make header names unique (case-insensitively) and distinct from the lineage columns."""
    taken = {c.lower() for c in LINEAGE_COLUMNS}
    out = []
    for j, h in enumerate(raw):
        base = " ".join(h.split()) or f"column_{j + 1}"
        name, n = base, 1
        while name.lower() in taken:
            n += 1
            name = f"{base}_{n}"
        taken.add(name.lower())
        out.append(name)
    return out


def build_table(
    grid: list[list[str]],
    *,
    source_file: str,
    sheet: str | None,
    digest: str,
    ingested_at: str,
    header_row: int | None = None,
    encoding: str | None = None,
    delimiter: str | None = None,
) -> IngestedTable | None:
    if not any(any(not is_null(c) for c in r) for r in grid):
        return None
    hdr = detect_header_row(grid) if header_row is None else header_row
    width = max(len(r) for r in grid)
    raw_header = [*grid[hdr], *[""] * (width - len(grid[hdr]))]
    keep = [j for j in range(width) if raw_header[j].strip() or any(
        j < len(r) and not is_null(r[j]) for r in grid[hdr + 1 :]
    )]
    columns = dedupe_headers([raw_header[j] for j in keep])
    rows: list[list[str]] = []
    row_numbers: list[int] = []
    for i, r in enumerate(grid[hdr + 1 :], start=hdr + 2):
        cells = [(r[j] if j < len(r) else "").strip() for j in keep]
        if all(is_null(c) for c in cells):
            continue
        rows.append(cells)
        row_numbers.append(i)
    table = IngestedTable(
        source_file=source_file,
        sheet=sheet,
        header_row=hdr,
        columns=columns,
        rows=rows,
        row_numbers=row_numbers,
        content_hash=digest,
        ingested_at=ingested_at,
        encoding=encoding,
        delimiter=delimiter,
    )
    table.profiles = profile_table(columns, rows)
    return table


def ingest_file(
    path: str | Path, header_row: int | None = None, sheets: list[str] | None = None
) -> list[IngestedTable]:
    """Load a CSV or XLSX file as strings, one IngestedTable per non-empty sheet."""
    p = Path(path)
    if not p.is_file():
        raise IngestError(f"no such file: {p}")
    digest = content_hash(p)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    suffix = p.suffix.lower()
    tables: list[IngestedTable] = []
    if suffix in (".xlsx", ".xlsm"):
        for name, grid in read_xlsx(p).items():
            if sheets and name not in sheets:
                continue
            t = build_table(grid, source_file=p.name, sheet=name, digest=digest,
                            ingested_at=now, header_row=header_row)
            if t is not None:
                tables.append(t)
    elif suffix in (".csv", ".tsv", ".txt"):
        grid, enc, delim = read_csv(p)
        t = build_table(grid, source_file=p.name, sheet=None, digest=digest, ingested_at=now,
                        header_row=header_row, encoding=enc, delimiter=delim)
        if t is not None:
            tables.append(t)
    else:
        raise IngestError(f"unsupported file type: {suffix}")
    if not tables:
        raise IngestError(f"no tabular data found in {p}")
    return tables
