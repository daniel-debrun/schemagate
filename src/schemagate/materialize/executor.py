from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from schemagate.ingest.loader import LINEAGE_COLUMNS, IngestedTable
from schemagate.materialize.dialects import DuckDBDialect, SqlBundle, SQLiteDialect
from schemagate.materialize.plan import MaterializationPlan, build_plan
from schemagate.store.service import GovernanceService


@dataclass(frozen=True)
class MaterializationResult:
    table: str
    rows_staged: int
    rows_written: int
    rows_rejected_null_key: int
    sql: SqlBundle


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def load_raw_table(conn: Any, name: str, table: IngestedTable, replace: bool = True) -> None:
    """Stage an ingested table as all-text columns plus lineage (SQLite or DuckDB connection)."""
    cols = [*table.columns, *LINEAGE_COLUMNS]
    if replace:
        conn.execute(f"DROP TABLE IF EXISTS {_q(name)}")
    defs = ", ".join(f"{_q(c)} {'INTEGER' if c == '_row_number' else 'TEXT'}" for c in cols)
    conn.execute(f"CREATE TABLE {_q(name)} ({defs})")
    placeholders = ", ".join("?" for _ in cols)
    rows = []
    for i, row in enumerate(table.rows):
        lineage = table.lineage(i)
        rows.append([*row, *(lineage[c] for c in LINEAGE_COLUMNS)])
    if rows:
        conn.executemany(f"INSERT INTO {_q(name)} VALUES ({placeholders})", rows)


def _existing_columns_sqlite(conn: Any, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({_q(table)})").fetchall()}


def _existing_columns_duckdb(conn: Any, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]
    ).fetchall()
    return {r[0] for r in rows}


def _count(conn: Any, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def execute_plan(conn: Any, plan: MaterializationPlan, engine: str = "sqlite") -> MaterializationResult:
    dialect = SQLiteDialect() if engine == "sqlite" else DuckDBDialect()
    bundle = dialect.render(plan)
    conn.execute(bundle.create_table)
    existing = (_existing_columns_sqlite if engine == "sqlite" else _existing_columns_duckdb)(
        conn, plan.target_table)
    for c in plan.extension_columns:
        if c.output_column not in existing:
            conn.execute(dialect.add_column(plan.target_table, c.output_column, "string"))
    index = dialect.key_index(plan)
    if index:
        conn.execute(index)
    staged = _count(conn, f"SELECT COUNT(*) FROM {_q(plan.raw_table)}")
    written = _count(conn, f"SELECT COUNT(*) FROM ({dialect.staged_select(plan)}) x")
    key_null = " OR ".join(f"{_q(c.output_column)} IS NULL" for c in plan.columns
                           if c.output_column in plan.key)
    rejected = 0
    if key_null:
        casts = ", ".join(f"{dialect.cast(c)} AS {_q(c.output_column)}" for c in plan.columns
                          if c.output_column in plan.key)
        rejected = _count(conn, f"SELECT COUNT(*) FROM (SELECT {casts} FROM {_q(plan.raw_table)}) k"
                                f" WHERE {key_null}")
    conn.execute(bundle.merge)
    if engine == "sqlite":
        conn.commit()
    return MaterializationResult(plan.target_table, staged, written, rejected, bundle)


def materialize(service: GovernanceService, conn: Any, spec_id: int, source_object_id: int,
                actor: str, engine: str = "sqlite") -> MaterializationResult:
    """Approval-gated materialization. Raises UnapprovedSpecError before touching the warehouse."""
    plan = build_plan(service, spec_id, source_object_id)
    result = execute_plan(conn, plan, engine)
    service.record_materialization(spec_id, source_object_id, engine, plan.target_table,
                                   result.rows_written, actor)
    return result


def render_sql(service: GovernanceService, spec_id: int, source_object_id: int, dialect_name: str) -> SqlBundle:
    from schemagate.materialize.dialects import get_dialect

    plan = build_plan(service, spec_id, source_object_id)
    return get_dialect(dialect_name).render(plan)
