from __future__ import annotations

import re
from dataclasses import dataclass, field

from schemagate.errors import DriftBlockedError
from schemagate.ingest.loader import LINEAGE_COLUMNS
from schemagate.store.service import GovernanceService, Spec

_SAFE = re.compile(r"[^a-z0-9_]+")


def raw_table_name(feed_name: str, object_id: int) -> str:
    return f"raw_{_SAFE.sub('_', feed_name.lower()).strip('_')}_{object_id}"


@dataclass(frozen=True)
class ColumnPlan:
    output_column: str
    target_type: str
    source_column: str | None
    source_type: str | None = None
    date_format: str | None = None
    is_extension: bool = False


@dataclass
class MaterializationPlan:
    spec_id: int
    spec_version: int
    feed_name: str
    target_table: str
    raw_table: str
    source_object_id: int
    columns: list[ColumnPlan]
    key: list[str]
    lineage_columns: tuple[str, ...] = LINEAGE_COLUMNS
    warnings: list[str] = field(default_factory=list)

    @property
    def schema_columns(self) -> list[ColumnPlan]:
        return [c for c in self.columns if not c.is_extension]

    @property
    def extension_columns(self) -> list[ColumnPlan]:
        return [c for c in self.columns if c.is_extension]


def build_plan(service: GovernanceService, spec_id: int, source_object_id: int) -> MaterializationPlan:
    """Build a plan from an approved spec. Raises UnapprovedSpecError / DriftBlockedError."""
    spec: Spec = service.verify_approved(spec_id)
    obj = service.source_object(source_object_id)
    if obj["feed_id"] != spec.feed_id:
        raise DriftBlockedError(f"source object {source_object_id} belongs to a different feed")
    present = set(obj["columns"])
    breaking = [e for e in service.drift_events(spec.feed_id)
                if e["source_object_id"] == source_object_id and e["spec_id"] == spec_id
                and e["severity"] == "breaking"]
    if breaking:
        kinds = ", ".join(f"{e['kind']}:{e['source_column']}" for e in breaking)
        raise DriftBlockedError(
            f"source object {source_object_id} has open breaking drift against spec {spec_id} ({kinds});"
            " run remediation and approve a new spec version")
    profiles = {p.name: p for p in spec.source_profiles}
    mapping = spec.mapping()
    missing = [m.source_column for m in mapping if m.target_field and m.source_column not in present]
    if missing:
        raise DriftBlockedError(
            f"source object {source_object_id} lacks mapped columns {missing}; run `schemagate drift`")
    by_target = {m.target_field: m for m in mapping if m.target_field}
    warnings: list[str] = []
    columns: list[ColumnPlan] = []
    for f in spec.schema.fields:
        m = by_target.get(f.name)
        if m is None:
            if f.required:
                warnings.append(f"required field '{f.name}' is not mapped; it will be NULL")
            columns.append(ColumnPlan(f.name, f.type, None))
            continue
        prof = profiles.get(m.source_column)
        columns.append(ColumnPlan(f.name, f.type, m.source_column,
                                  prof.inferred_type if prof else None,
                                  prof.date_format if prof else None))
    for m in mapping:
        if m.target_field is None and m.extension_column:
            src = m.source_column if m.source_column in present else None
            if src is None:
                warnings.append(f"extension source column '{m.source_column}' absent; NULL")
            columns.append(ColumnPlan(m.extension_column, "string", src, is_extension=True))
    key = list(spec.schema.key)
    unmapped_key = [k for k in key if k not in by_target]
    if unmapped_key:
        raise DriftBlockedError(f"spec {spec_id} does not map key field(s) {unmapped_key}; cannot upsert")
    return MaterializationPlan(
        spec_id=spec.id, spec_version=spec.version, feed_name=spec.feed_name,
        target_table=spec.target_table,
        raw_table=obj["raw_table"] or raw_table_name(spec.feed_name, source_object_id),
        source_object_id=source_object_id, columns=columns, key=key, warnings=warnings)
