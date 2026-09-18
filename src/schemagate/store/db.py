from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS source_feed (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  schema_name TEXT NOT NULL,
  schema_json TEXT NOT NULL,
  active_spec_id INTEGER,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_object (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  feed_id INTEGER NOT NULL REFERENCES source_feed(id),
  file_name TEXT NOT NULL,
  sheet TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL,
  header_row INTEGER NOT NULL,
  row_count INTEGER NOT NULL,
  columns_json TEXT NOT NULL,
  profile_json TEXT NOT NULL,
  encoding TEXT,
  delimiter TEXT,
  raw_table TEXT,
  ingested_by TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  UNIQUE (feed_id, content_hash, sheet)
);
CREATE TABLE IF NOT EXISTS mapping_spec (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  feed_id INTEGER NOT NULL REFERENCES source_feed(id),
  version INTEGER NOT NULL,
  target_table TEXT NOT NULL,
  schema_name TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  source_object_id INTEGER REFERENCES source_object(id),
  parent_spec_id INTEGER REFERENCES mapping_spec(id),
  change_kind TEXT NOT NULL,
  status TEXT NOT NULL,
  required_approvals INTEGER NOT NULL,
  source_profile_json TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  UNIQUE (feed_id, version)
);
CREATE TABLE IF NOT EXISTS mapping_proposal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  spec_id INTEGER NOT NULL REFERENCES mapping_spec(id),
  source_column TEXT NOT NULL,
  target_field TEXT,
  extension_column TEXT,
  confidence REAL NOT NULL,
  tier TEXT NOT NULL,
  rationale TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  proposer TEXT NOT NULL,
  prompt_version TEXT,
  model_id TEXT,
  conflict_json TEXT,
  status TEXT NOT NULL,
  decided_by TEXT,
  decided_at TEXT,
  UNIQUE (spec_id, source_column)
);
CREATE TABLE IF NOT EXISTS mapping_approval (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  spec_id INTEGER NOT NULL REFERENCES mapping_spec(id),
  approver TEXT NOT NULL,
  decision TEXT NOT NULL,
  comment TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (spec_id, approver)
);
CREATE TABLE IF NOT EXISTS mapping_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS schema_drift_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  feed_id INTEGER NOT NULL REFERENCES source_feed(id),
  spec_id INTEGER NOT NULL REFERENCES mapping_spec(id),
  source_object_id INTEGER NOT NULL REFERENCES source_object(id),
  kind TEXT NOT NULL,
  source_column TEXT,
  severity TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  status TEXT NOT NULL,
  resolved_by_spec_id INTEGER REFERENCES mapping_spec(id),
  detected_by TEXT NOT NULL,
  detected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS materialization (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  spec_id INTEGER NOT NULL REFERENCES mapping_spec(id),
  source_object_id INTEGER NOT NULL REFERENCES source_object(id),
  dialect TEXT NOT NULL,
  target_table TEXT NOT NULL,
  rows_written INTEGER,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS mapping_audit_no_update BEFORE UPDATE ON mapping_audit
BEGIN SELECT RAISE(ABORT, 'mapping_audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS mapping_audit_no_delete BEFORE DELETE ON mapping_audit
BEGIN SELECT RAISE(ABORT, 'mapping_audit is append-only'); END;
"""


def _postgres_ddl() -> list[str]:
    ddl = SQLITE_DDL.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    ddl = ddl.replace("REAL", "DOUBLE PRECISION")
    ddl = ddl.split("CREATE TRIGGER", 1)[0]
    stmts = [s.strip() for s in ddl.split(";") if s.strip()]
    stmts += [
        """CREATE OR REPLACE FUNCTION schemagate_audit_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'mapping_audit is append-only'; END; $$ LANGUAGE plpgsql""",
        "DROP TRIGGER IF EXISTS mapping_audit_append_only ON mapping_audit",
        """CREATE TRIGGER mapping_audit_append_only BEFORE UPDATE OR DELETE ON mapping_audit
FOR EACH ROW EXECUTE FUNCTION schemagate_audit_append_only()""",
    ]
    return stmts


_QMARK = re.compile(r"\?")


class Database:
    """Minimal DB-API wrapper: SQLite by default, Postgres via psycopg (the 'postgres' extra)."""

    def __init__(self, url: str) -> None:
        self.url = url
        if url.startswith(("postgresql://", "postgres://")):
            try:
                import psycopg
            except ImportError as exc:
                raise ImportError("Postgres store requires: pip install 'schemadriftgate[postgres]'") from exc
            self.dialect = "postgres"
            self._conn = psycopg.connect(url, autocommit=True)
        else:
            path = url.removeprefix("sqlite:///")
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.dialect = "sqlite"
            self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            self._conn.execute("PRAGMA foreign_keys = ON")
        self._depth = 0

    def init_schema(self) -> None:
        if self.dialect == "sqlite":
            self._conn.executescript(SQLITE_DDL)
        else:
            for stmt in _postgres_ddl():
                self._conn.execute(stmt)

    def _sql(self, sql: str) -> str:
        return _QMARK.sub("%s", sql) if self.dialect == "postgres" else sql

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._conn.execute("BEGIN IMMEDIATE" if self.dialect == "sqlite" else "BEGIN")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")
        finally:
            self._depth = 0

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._conn.execute(self._sql(sql), tuple(params))

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        if self.dialect == "postgres":
            cur = self._conn.execute(self._sql(sql) + " RETURNING id", tuple(params))
            return int(cur.fetchone()[0])
        cur = self._conn.execute(sql, tuple(params))
        return int(cur.lastrowid)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cur = self._conn.execute(self._sql(sql), tuple(params))
        if cur.description is None:
            return []
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    @property
    def raw_connection(self) -> Any:
        return self._conn

    def close(self) -> None:
        self._conn.close()
