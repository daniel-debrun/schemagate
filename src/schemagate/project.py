from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from schemagate.config import ApprovalPolicy, ConfidencePolicy
from schemagate.drift.detect import detect_drift
from schemagate.errors import NotFoundError, SchemagateError
from schemagate.ingest.loader import IngestedTable, ingest_file
from schemagate.llm import get_provider
from schemagate.materialize.executor import load_raw_table
from schemagate.materialize.plan import raw_table_name
from schemagate.resolve.model import ModelResolver
from schemagate.resolve.pipeline import ResolverChain
from schemagate.schema.target import TargetSchema, load_schema
from schemagate.store.service import GovernanceService

CONFIG_FILE = "schemagate.yaml"
DEFAULT_CONFIG: dict[str, Any] = {
    "store": "sqlite:///.schemagate/governance.db",
    "warehouse": ".schemagate/warehouse.db",
    "schemas_dir": "schemas",
    "provider": "heuristic",
    "model_id": None,
    "synonym_threshold": 0.75,
    "confidence": ConfidencePolicy().model_dump(),
    "approval": ApprovalPolicy().model_dump(),
}


@dataclass
class IngestOutcome:
    object_id: int
    table: IngestedTable
    created: bool


class Project:
    """A working directory holding config, target schemas, the governance store and a SQLite warehouse."""

    def __init__(self, root: Path, config: dict[str, Any]) -> None:
        self.root = root
        self.config = {**DEFAULT_CONFIG, **config}
        self.confidence = ConfidencePolicy(**self.config["confidence"])
        self.approval = ApprovalPolicy(**self.config["approval"])
        self._service: GovernanceService | None = None
        self._warehouse: sqlite3.Connection | None = None

    @classmethod
    def init(cls, root: Path, schemas: list[Path] | None = None) -> Project:
        root.mkdir(parents=True, exist_ok=True)
        cfg_path = root / CONFIG_FILE
        if not cfg_path.exists():
            cfg_path.write_text(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False), "utf-8")
        sdir = root / DEFAULT_CONFIG["schemas_dir"]
        sdir.mkdir(exist_ok=True)
        for s in schemas or []:
            schema = load_schema(s)
            (sdir / f"{schema.name}.yaml").write_text(Path(s).read_text("utf-8"), "utf-8")
        project = cls.load(root)
        project.service()
        return project

    @classmethod
    def load(cls, root: Path) -> Project:
        cfg_path = root / CONFIG_FILE
        if not cfg_path.exists():
            raise SchemagateError(f"no {CONFIG_FILE} in {root}; run `schemagate init` first")
        return cls(root, yaml.safe_load(cfg_path.read_text("utf-8")) or {})

    def _path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else self.root / p

    def service(self) -> GovernanceService:
        if self._service is None:
            url = self.config["store"]
            if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
                url = "sqlite:///" + str(self._path(url.removeprefix("sqlite:///")))
            self._service = GovernanceService.open(url, approval_policy=self.approval,
                                                   confidence_policy=self.confidence)
        return self._service

    def warehouse(self) -> sqlite3.Connection:
        if self._warehouse is None:
            path = self._path(self.config["warehouse"])
            path.parent.mkdir(parents=True, exist_ok=True)
            self._warehouse = sqlite3.connect(path)
        return self._warehouse

    def schema(self, name: str) -> TargetSchema:
        sdir = self._path(self.config["schemas_dir"])
        for path in sorted(sdir.glob("*.y*ml")):
            schema = load_schema(path)
            if schema.name == name:
                return schema
        raise NotFoundError(f"schema {name!r} not found in {sdir}")

    def chain(self, provider: str | None = None) -> ResolverChain:
        name = provider or self.config["provider"]
        model = None if name == "none" else ModelResolver(get_provider(name, self.config.get("model_id")))
        return ResolverChain.default(model, self.config["synonym_threshold"])

    def ingest(self, feed: str, schema_name: str | None, path: Path, actor: str,
               header_row: int | None = None, sheets: list[str] | None = None) -> list[IngestOutcome]:
        svc = self.service()
        try:
            feed_row = svc.feed(feed)
            feed_id = int(feed_row["id"])
        except NotFoundError:
            if schema_name is None:
                raise SchemagateError(f"feed {feed!r} is new; pass --schema") from None
            feed_id = svc.register_feed(feed, self.schema(schema_name), actor)
        outcomes = []
        for table in ingest_file(path, header_row=header_row, sheets=sheets):
            obj_id, created = svc.record_source_object(feed_id, table, actor)
            # An object without a raw table is an earlier ingest that failed half-way: load it again.
            if created or svc.source_object(obj_id)["raw_table"] is None:
                raw = raw_table_name(feed, obj_id)
                load_raw_table(self.warehouse(), raw, table)
                self.warehouse().commit()
                svc.set_raw_table(obj_id, raw)
            outcomes.append(IngestOutcome(obj_id, table, created))
        return outcomes

    def raw_values(self, object_id: int) -> dict[str, list[str]]:
        obj = self.service().source_object(object_id)
        conn = self.warehouse()
        out: dict[str, list[str]] = {}
        for col in obj["columns"]:
            q = '"' + col.replace('"', '""') + '"'
            t = '"' + obj["raw_table"] + '"'
            out[col] = [r[0] or "" for r in conn.execute(f"SELECT {q} FROM {t}").fetchall()]
        return out

    def detect_drift(self, feed_id: int, object_id: int, actor: str,
                     spec_id: int | None = None) -> list[dict[str, Any]]:
        svc = self.service()
        spec = svc.spec(spec_id) if spec_id else svc.active_spec(feed_id)
        if spec is None:
            raise SchemagateError("feed has no active approved spec to compare against")
        obj = svc.source_object(object_id)
        if obj["feed_id"] != feed_id:
            raise SchemagateError(f"source object {object_id} does not belong to this feed")
        events = detect_drift(spec.schema, spec.mapping(), spec.source_profiles, obj["profiles"],
                              self.raw_values(object_id))
        svc.record_drift_events(feed_id, spec.id, object_id, [e.to_dict() for e in events], actor)
        return [e.to_dict() for e in events]
