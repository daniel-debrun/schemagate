from __future__ import annotations

from pathlib import Path

import pytest

from schemagate.config import ApprovalPolicy
from schemagate.ingest.loader import IngestedTable, build_table
from schemagate.schema.target import TargetSchema, load_schema
from schemagate.store.service import GovernanceService

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


@pytest.fixture
def invoice_schema() -> TargetSchema:
    return load_schema(EXAMPLES / "schemas" / "supplier_invoice.yaml")


@pytest.fixture
def service() -> GovernanceService:
    svc = GovernanceService.open("sqlite:///:memory:", approval_policy=ApprovalPolicy())
    yield svc
    svc.db.close()


def make_table(columns: list[str], rows: list[list[str]], name: str = "test.csv",
               digest: str = "d" * 64) -> IngestedTable:
    table = build_table([columns, *rows], source_file=name, sheet=None, digest=digest,
                        ingested_at="2026-09-01T00:00:00+00:00", header_row=0)
    assert table is not None
    return table


INVOICE_COLUMNS = ["Invoice Number", "Line", "Invoice Date", "Vendor ID", "Qty", "Unit Price (USD)",
                   "Line Total (USD)", "Currency", "Warehouse"]


def invoice_rows(n: int = 4, start: int = 1) -> list[list[str]]:
    return [[f"INV-{i // 2 + start}", str(i % 2 + 1), f"2026-07-{i + 1:02d}", "V-1", str(i + 1),
             "$1,250.50", f"${(i + 1) * 1250.5:,.2f}", "USD", "TOR"] for i in range(n)]


def propose_spec(service: GovernanceService, schema: TargetSchema, feed: str, actor: str = "alice",
                 columns: list[str] | None = None, rows: list[list[str]] | None = None,
                 digest: str | None = None) -> tuple[int, int]:
    from schemagate.resolve import ResolverChain

    feed_id = service.register_feed(feed, schema, actor)
    table = make_table(columns or INVOICE_COLUMNS, rows or invoice_rows(),
                       digest=digest or (feed + "x" * 64)[:64])
    obj_id, _ = service.record_source_object(feed_id, table, actor)
    result = ResolverChain.default().run(schema, table.profiles)
    spec_id = service.create_spec(feed_id, result.proposals, actor, source_object_id=obj_id,
                                  source_profiles=table.profiles)
    return spec_id, obj_id
