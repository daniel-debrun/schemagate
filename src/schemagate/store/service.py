from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from schemagate.config import ApprovalPolicy, ConfidencePolicy
from schemagate.errors import (
    ApprovalPolicyError,
    InvalidTransitionError,
    NotFoundError,
    UnapprovedSpecError,
)
from schemagate.ingest.loader import IngestedTable
from schemagate.ingest.profile import ColumnProfile
from schemagate.models import MappingProposal, Tier
from schemagate.schema.normalize import extension_column_name
from schemagate.schema.target import TargetSchema
from schemagate.store.audit import AuditLog, actor_key, clean_actor, utcnow
from schemagate.store.db import Database

PENDING, APPROVED, REJECTED, SUPERSEDED = "pending", "approved", "rejected", "superseded"
P_PROPOSED, P_ACCEPTED, P_DEMOTED, P_OVERRIDDEN = "proposed", "accepted", "demoted", "overridden"
REVIEW_REQUIRED_TIERS = frozenset({Tier.MODEL.value, Tier.RENAME.value})


@dataclass
class Spec:
    id: int
    feed_id: int
    feed_name: str
    version: int
    target_table: str
    status: str
    change_kind: str
    required_approvals: int
    created_by: str
    source_object_id: int | None
    parent_spec_id: int | None
    schema: TargetSchema
    proposals: list[dict[str, Any]]
    source_profiles: list[ColumnProfile]

    def mapping(self) -> list[MappingProposal]:
        out = []
        for p in self.proposals:
            out.append(MappingProposal(
                source_column=p["source_column"], target_field=p["target_field"],
                confidence=p["confidence"], tier=Tier(p["tier"]), rationale=p["rationale"],
                evidence=json.loads(p["evidence_json"]), proposer=p["proposer"],
                prompt_version=p["prompt_version"], model_id=p["model_id"],
                extension_column=p["extension_column"],
                conflict=json.loads(p["conflict_json"]) if p["conflict_json"] else None,
            ))
        return out


class GovernanceService:
    """The only write path for governance state. Every transition appends to the audit log
    inside the same transaction as the state change."""

    def __init__(self, db: Database, approval_policy: ApprovalPolicy | None = None,
                 confidence_policy: ConfidencePolicy | None = None) -> None:
        self.db = db
        self.approval_policy = approval_policy or ApprovalPolicy()
        self.confidence_policy = confidence_policy or ConfidencePolicy()
        self.audit = AuditLog(db)

    @classmethod
    def open(cls, url: str, **kwargs: Any) -> GovernanceService:
        db = Database(url)
        db.init_schema()
        return cls(db, **kwargs)

    # feeds and source objects -------------------------------------------------------------

    def register_feed(self, name: str, schema: TargetSchema, actor: str) -> int:
        with self.db.transaction():
            existing = self.db.one("SELECT id, schema_name FROM source_feed WHERE name = ?", (name,))
            if existing:
                if existing["schema_name"] != schema.name:
                    raise InvalidTransitionError(
                        f"feed {name!r} is bound to schema {existing['schema_name']!r}")
                return int(existing["id"])
            feed_id = self.db.insert(
                "INSERT INTO source_feed (name, schema_name, schema_json, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, schema.name, schema.model_dump_json(), actor, utcnow()),
            )
            self.audit.append(actor, "feed.registered", "source_feed", feed_id,
                              {"name": name, "schema": schema.name, "table": schema.table})
            return feed_id

    def feed(self, name_or_id: str | int) -> dict[str, Any]:
        col = "id" if isinstance(name_or_id, int) else "name"
        row = self.db.one(f"SELECT * FROM source_feed WHERE {col} = ?", (name_or_id,))
        if row is None:
            raise NotFoundError(f"feed {name_or_id!r} not found")
        return row

    def feed_schema(self, feed_id: int) -> TargetSchema:
        return TargetSchema.model_validate_json(self.feed(feed_id)["schema_json"])

    def feeds(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM source_feed ORDER BY id")

    def record_source_object(self, feed_id: int, table: IngestedTable, actor: str,
                             raw_table: str | None = None) -> tuple[int, bool]:
        """Register an ingested sheet. Returns (id, created); identical content is skipped."""
        sheet = table.sheet or ""
        with self.db.transaction():
            existing = self.db.one(
                "SELECT id FROM source_object WHERE feed_id = ? AND content_hash = ? AND sheet = ?",
                (feed_id, table.content_hash, sheet),
            )
            if existing:
                return int(existing["id"]), False
            obj_id = self.db.insert(
                "INSERT INTO source_object (feed_id, file_name, sheet, content_hash, header_row,"
                " row_count, columns_json, profile_json, encoding, delimiter, raw_table,"
                " ingested_by, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (feed_id, table.source_file, sheet, table.content_hash, table.header_row,
                 len(table.rows), json.dumps(table.columns),
                 json.dumps([p.to_dict() for p in table.profiles]), table.encoding,
                 table.delimiter, raw_table, actor, table.ingested_at),
            )
            self.audit.append(actor, "source_object.ingested", "source_object", obj_id, {
                "feed_id": feed_id, "file": table.source_file, "sheet": sheet,
                "content_hash": table.content_hash, "rows": len(table.rows),
                "header_row": table.header_row,
            })
            return obj_id, True

    def set_raw_table(self, object_id: int, raw_table: str) -> None:
        self.db.execute("UPDATE source_object SET raw_table = ? WHERE id = ?", (raw_table, object_id))

    def source_object(self, object_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM source_object WHERE id = ?", (object_id,))
        if row is None:
            raise NotFoundError(f"source object {object_id} not found")
        row["columns"] = json.loads(row["columns_json"])
        row["profiles"] = [ColumnProfile.from_dict(d) for d in json.loads(row["profile_json"])]
        return row

    def source_objects(self, feed_id: int) -> list[dict[str, Any]]:
        ids = self.db.query("SELECT id FROM source_object WHERE feed_id = ? ORDER BY id", (feed_id,))
        return [self.source_object(int(r["id"])) for r in ids]

    # specs --------------------------------------------------------------------------------

    def _required_approvals(self, feed_id: int, schema: TargetSchema) -> tuple[int, str]:
        if schema.published:
            return self.approval_policy.shared_table_approvers, "target table is published"
        other = self.db.one(
            "SELECT COUNT(*) AS n FROM mapping_spec WHERE target_table = ? AND feed_id != ?"
            " AND status IN (?, ?)", (schema.table, feed_id, APPROVED, SUPERSEDED),
        )
        if other and other["n"]:
            return self.approval_policy.shared_table_approvers, "target table is shared with other feeds"
        return self.approval_policy.new_table_approvers, "target table is private to this feed"

    def create_spec(self, feed_id: int, proposals: list[MappingProposal], actor: str, *,
                    source_object_id: int | None, source_profiles: list[ColumnProfile],
                    parent_spec_id: int | None = None, change_kind: str = "initial") -> int:
        schema = self.feed_schema(feed_id)
        for p in proposals:
            if p.target_field is not None and p.target_field not in schema.field_names:
                raise ValueError(f"proposal targets unknown field {p.target_field!r}")
            if p.tier is Tier.MODEL and p.confidence > self.confidence_policy.model_cap:
                raise ValueError(
                    f"model proposal for {p.source_column!r} exceeds cap "
                    f"({p.confidence} > {self.confidence_policy.model_cap})")
        targets = [p.target_field for p in proposals if p.target_field]
        if len(targets) != len(set(targets)):
            raise ValueError("proposals map several source columns to one target; resolve collisions first")
        with self.db.transaction():
            row = self.db.one("SELECT COALESCE(MAX(version), 0) AS v FROM mapping_spec WHERE feed_id = ?",
                              (feed_id,))
            version = int(row["v"] if row else 0) + 1
            required, reason = self._required_approvals(feed_id, schema)
            spec_id = self.db.insert(
                "INSERT INTO mapping_spec (feed_id, version, target_table, schema_name, schema_version,"
                " source_object_id, parent_spec_id, change_kind, status, required_approvals,"
                " source_profile_json, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (feed_id, version, schema.table, schema.name, schema.version, source_object_id,
                 parent_spec_id, change_kind, PENDING, required,
                 json.dumps([p.to_dict() for p in source_profiles]), actor, utcnow()),
            )
            for p in proposals:
                self.db.insert(
                    "INSERT INTO mapping_proposal (spec_id, source_column, target_field,"
                    " extension_column, confidence, tier, rationale, evidence_json, proposer,"
                    " prompt_version, model_id, conflict_json, status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (spec_id, p.source_column, p.target_field,
                     p.extension_column if p.target_field is None else None, p.confidence,
                     p.tier.value, p.rationale, json.dumps(p.evidence, default=str), p.proposer,
                     p.prompt_version, p.model_id,
                     json.dumps(p.conflict) if p.conflict else None, P_PROPOSED),
                )
            self.audit.append(actor, "spec.proposed", "mapping_spec", spec_id, {
                "feed_id": feed_id, "version": version, "change_kind": change_kind,
                "parent_spec_id": parent_spec_id, "required_approvals": required,
                "approval_rule": reason,
                "proposals": [
                    {"source_column": p.source_column, "target_field": p.target_field,
                     "extension_column": p.extension_column, "tier": p.tier.value,
                     "confidence": p.confidence, "proposer": p.proposer,
                     "prompt_version": p.prompt_version, "model_id": p.model_id,
                     "rationale": p.rationale}
                    for p in proposals
                ],
            })
            return spec_id

    def spec(self, spec_id: int) -> Spec:
        row = self.db.one(
            "SELECT s.*, f.name AS feed_name, f.schema_json FROM mapping_spec s"
            " JOIN source_feed f ON f.id = s.feed_id WHERE s.id = ?", (spec_id,))
        if row is None:
            raise NotFoundError(f"spec {spec_id} not found")
        proposals = self.db.query("SELECT * FROM mapping_proposal WHERE spec_id = ? ORDER BY id", (spec_id,))
        return Spec(
            id=int(row["id"]), feed_id=int(row["feed_id"]), feed_name=row["feed_name"],
            version=int(row["version"]), target_table=row["target_table"], status=row["status"],
            change_kind=row["change_kind"], required_approvals=int(row["required_approvals"]),
            created_by=row["created_by"], source_object_id=row["source_object_id"],
            parent_spec_id=row["parent_spec_id"],
            schema=TargetSchema.model_validate_json(row["schema_json"]),
            proposals=proposals,
            source_profiles=[ColumnProfile.from_dict(d) for d in json.loads(row["source_profile_json"])],
        )

    def specs(self, feed_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT s.id, s.version, s.status, s.change_kind, s.target_table, s.required_approvals,"
               " s.created_by, s.created_at, f.name AS feed FROM mapping_spec s"
               " JOIN source_feed f ON f.id = s.feed_id WHERE 1=1")
        params: list[Any] = []
        if feed_id is not None:
            sql += " AND s.feed_id = ?"
            params.append(feed_id)
        if status is not None:
            sql += " AND s.status = ?"
            params.append(status)
        return self.db.query(sql + " ORDER BY s.id", params)

    def required_approvals(self, spec_id: int) -> tuple[int, str]:
        """Approvals the spec needs under the current state of the store, with the rule that applies."""
        spec = self.spec(spec_id)
        required, reason = self._required_approvals(spec.feed_id, spec.schema)
        if spec.status != PENDING or spec.required_approvals > required:
            return spec.required_approvals, reason
        return required, reason

    def approvals(self, spec_id: int) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM mapping_approval WHERE spec_id = ? ORDER BY id", (spec_id,))

    def _proposal(self, proposal_id: int) -> tuple[dict[str, Any], Spec]:
        row = self.db.one("SELECT * FROM mapping_proposal WHERE id = ?", (proposal_id,))
        if row is None:
            raise NotFoundError(f"proposal {proposal_id} not found")
        spec = self.spec(int(row["spec_id"]))
        if spec.status != PENDING:
            raise InvalidTransitionError(f"spec {spec.id} is {spec.status}; proposals are frozen")
        return row, spec

    def _check_reviewer(self, spec: Spec, actor: str) -> None:
        clean_actor(actor)
        if actor_key(actor) == actor_key(spec.created_by) and not self.approval_policy.allow_self_approval:
            raise ApprovalPolicyError(f"{actor!r} proposed spec {spec.id} and cannot review it")

    def accept_proposal(self, proposal_id: int, actor: str, comment: str = "") -> None:
        with self.db.transaction():
            row, spec = self._proposal(proposal_id)
            self._check_reviewer(spec, actor)
            if row["target_field"] is None:
                raise InvalidTransitionError("nothing to accept: proposal has no target")
            self.db.execute("UPDATE mapping_proposal SET status = ?, decided_by = ?, decided_at = ?"
                            " WHERE id = ?", (P_ACCEPTED, actor, utcnow(), proposal_id))
            self.audit.append(actor, "proposal.accepted", "mapping_proposal", proposal_id, {
                "spec_id": spec.id, "source_column": row["source_column"],
                "target_field": row["target_field"], "tier": row["tier"],
                "confidence": row["confidence"], "comment": comment})

    def demote_proposal(self, proposal_id: int, actor: str, reason: str) -> None:
        with self.db.transaction():
            row, spec = self._proposal(proposal_id)
            self._check_reviewer(spec, actor)
            ext = extension_column_name(row["source_column"])
            taken = {p["extension_column"] for p in spec.proposals if p["extension_column"]}
            base, n = ext, 2
            while ext in taken:
                ext = f"{base}_{n}"
                n += 1
            self.db.execute(
                "UPDATE mapping_proposal SET target_field = NULL, extension_column = ?, status = ?,"
                " decided_by = ?, decided_at = ? WHERE id = ?",
                (ext, P_DEMOTED, actor, utcnow(), proposal_id))
            self.audit.append(actor, "proposal.demoted", "mapping_proposal", proposal_id, {
                "spec_id": spec.id, "source_column": row["source_column"],
                "previous_target": row["target_field"], "extension_column": ext, "reason": reason})

    def override_proposal(self, proposal_id: int, target_field: str, actor: str, reason: str) -> None:
        with self.db.transaction():
            row, spec = self._proposal(proposal_id)
            self._check_reviewer(spec, actor)
            if target_field not in spec.schema.field_names:
                raise ValueError(f"{target_field!r} is not a field of schema {spec.schema.name!r}")
            holder = next((p for p in spec.proposals
                           if p["target_field"] == target_field and p["id"] != proposal_id), None)
            if holder is not None:
                raise InvalidTransitionError(
                    f"{target_field!r} is held by column {holder['source_column']!r};"
                    f" demote proposal {holder['id']} first")
            self.db.execute(
                "UPDATE mapping_proposal SET target_field = ?, extension_column = NULL, tier = ?,"
                " confidence = 1.0, status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                (target_field, Tier.HUMAN.value, P_OVERRIDDEN, actor, utcnow(), proposal_id))
            self.audit.append(actor, "proposal.overridden", "mapping_proposal", proposal_id, {
                "spec_id": spec.id, "source_column": row["source_column"],
                "previous_target": row["target_field"], "previous_tier": row["tier"],
                "target_field": target_field, "reason": reason})

    def approve_spec(self, spec_id: int, approver: str, comment: str = "") -> str:
        """Record an approval. Returns the spec status after the decision."""
        with self.db.transaction():
            spec = self.spec(spec_id)
            if spec.status != PENDING:
                raise InvalidTransitionError(f"spec {spec_id} is {spec.status}, not pending")
            self._check_reviewer(spec, approver)
            prior = {a["approver"] for a in self.approvals(spec_id)}
            if actor_key(approver) in {actor_key(a) for a in prior}:
                raise ApprovalPolicyError(f"{approver!r} has already approved spec {spec_id}")
            unreviewed = [p["source_column"] for p in spec.proposals
                          if p["tier"] in REVIEW_REQUIRED_TIERS and p["status"] == P_PROPOSED]
            if unreviewed:
                raise ApprovalPolicyError(
                    "model/rename proposals need an explicit accept, override or demote before approval: "
                    + ", ".join(unreviewed))
            required, reason = self._required_approvals(spec.feed_id, spec.schema)
            required = max(required, spec.required_approvals)
            self.db.insert("INSERT INTO mapping_approval (spec_id, approver, decision, comment, created_at)"
                           " VALUES (?, ?, ?, ?, ?)", (spec_id, approver, "approve", comment, utcnow()))
            count = len(prior) + 1
            self.audit.append(approver, "spec.approval_recorded", "mapping_spec", spec_id, {
                "approver": approver, "approvals": count, "required": required, "rule": reason,
                "comment": comment})
            if count < required:
                if required != spec.required_approvals:
                    self.db.execute("UPDATE mapping_spec SET required_approvals = ? WHERE id = ?",
                                    (required, spec_id))
                return PENDING
            self.db.execute("UPDATE mapping_spec SET status = ?, required_approvals = ?, decided_at = ?"
                            " WHERE id = ?", (APPROVED, required, utcnow(), spec_id))
            self.audit.append(approver, "spec.approved", "mapping_spec", spec_id, {
                "approvers": sorted(prior | {approver}), "required": required, "rule": reason})
            feed = self.feed(spec.feed_id)
            if feed["active_spec_id"] is None:
                self._activate(spec, approver, gate="first approved spec")
            return APPROVED

    def reject_spec(self, spec_id: int, actor: str, comment: str) -> None:
        with self.db.transaction():
            spec = self.spec(spec_id)
            if spec.status != PENDING:
                raise InvalidTransitionError(f"spec {spec_id} is {spec.status}, not pending")
            self._check_reviewer(spec, actor)
            self.db.insert("INSERT INTO mapping_approval (spec_id, approver, decision, comment, created_at)"
                           " VALUES (?, ?, ?, ?, ?)", (spec_id, actor, "reject", comment, utcnow()))
            self.db.execute("UPDATE mapping_spec SET status = ?, decided_at = ? WHERE id = ?",
                            (REJECTED, utcnow(), spec_id))
            self.audit.append(actor, "spec.rejected", "mapping_spec", spec_id, {"comment": comment})

    def verify_approved(self, spec_id: int) -> Spec:
        """Re-derive approval from the approval rows and the audit log rather than trusting a status flag."""
        spec = self.spec(spec_id)
        if spec.status not in (APPROVED, SUPERSEDED):
            raise UnapprovedSpecError(f"spec {spec_id} is {spec.status}; only approved specs can be materialized")
        approvers = {actor_key(a["approver"]) for a in self.approvals(spec_id) if a["decision"] == "approve"}
        if not self.approval_policy.allow_self_approval:
            approvers.discard(actor_key(spec.created_by))
        if len(approvers) < spec.required_approvals:
            raise UnapprovedSpecError(
                f"spec {spec_id} has {len(approvers)} valid approvals, {spec.required_approvals} required")
        unreviewed = [p["source_column"] for p in spec.proposals
                      if p["tier"] in REVIEW_REQUIRED_TIERS and p["status"] == P_PROPOSED]
        if unreviewed:
            raise UnapprovedSpecError(
                f"spec {spec_id} has model/rename proposals nobody reviewed: {', '.join(unreviewed)}")
        chain = self.audit.verify()
        if not chain.ok:
            raise UnapprovedSpecError(
                f"audit log fails verification at entry {chain.first_bad_id} ({chain.reason});"
                " approvals cannot be trusted")
        if not self.audit.entries("mapping_spec", spec_id, action="spec.approved"):
            raise UnapprovedSpecError(f"spec {spec_id} has no approval recorded in the audit log")
        return spec

    def _activate(self, spec: Spec, actor: str, gate: str) -> None:
        feed = self.feed(spec.feed_id)
        previous = feed["active_spec_id"]
        if previous is not None and previous != spec.id:
            self.db.execute("UPDATE mapping_spec SET status = ? WHERE id = ?", (SUPERSEDED, previous))
        self.db.execute("UPDATE source_feed SET active_spec_id = ? WHERE id = ?", (spec.id, spec.feed_id))
        self.db.execute(
            "UPDATE schema_drift_event SET status = 'remediated', resolved_by_spec_id = ?"
            " WHERE feed_id = ? AND status = 'open' AND spec_id != ?", (spec.id, spec.feed_id, spec.id))
        self.audit.append(actor, "spec.activated", "mapping_spec", spec.id, {
            "feed_id": spec.feed_id, "previous_spec_id": previous, "gate": gate})

    def switch_active(self, spec_id: int, actor: str) -> None:
        """Switch step: point the feed at a newer approved spec version."""
        with self.db.transaction():
            spec = self.verify_approved(spec_id)
            if spec.status == SUPERSEDED:
                raise InvalidTransitionError(f"spec {spec_id} was superseded; propose a new version")
            feed = self.feed(spec.feed_id)
            if feed["active_spec_id"] == spec_id:
                return
            if feed["active_spec_id"] is not None:
                active = self.spec(int(feed["active_spec_id"]))
                if spec.version < active.version:
                    raise InvalidTransitionError("cannot switch to an older spec version")
            self._activate(spec, actor, gate="switch: approved and verified")

    def active_spec(self, feed_id: int) -> Spec | None:
        feed = self.feed(feed_id)
        return self.spec(int(feed["active_spec_id"])) if feed["active_spec_id"] else None

    # drift and materialization -------------------------------------------------------------

    def record_drift_events(self, feed_id: int, spec_id: int, object_id: int,
                            events: list[dict[str, Any]], actor: str) -> list[int]:
        ids = []
        with self.db.transaction():
            self.db.execute("DELETE FROM schema_drift_event WHERE source_object_id = ? AND spec_id = ?"
                            " AND status = 'open'", (object_id, spec_id))
            now = utcnow()
            for e in events:
                ids.append(self.db.insert(
                    "INSERT INTO schema_drift_event (feed_id, spec_id, source_object_id, kind,"
                    " source_column, severity, detail_json, status, detected_by, detected_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                    (feed_id, spec_id, object_id, e["kind"], e.get("source_column"), e["severity"],
                     json.dumps(e.get("detail", {}), default=str), actor, now)))
            self.audit.append(actor, "drift.detected", "source_object", object_id, {
                "spec_id": spec_id, "events": [
                    {"kind": e["kind"], "column": e.get("source_column"), "severity": e["severity"]}
                    for e in events]})
        return ids

    def drift_events(self, feed_id: int, status: str | None = "open") -> list[dict[str, Any]]:
        sql = "SELECT * FROM schema_drift_event WHERE feed_id = ?"
        params: list[Any] = [feed_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        rows = self.db.query(sql + " ORDER BY id", params)
        for r in rows:
            r["detail"] = json.loads(r["detail_json"])
        return rows

    def record_materialization(self, spec_id: int, object_id: int, dialect: str, table: str,
                               rows: int | None, actor: str) -> None:
        with self.db.transaction():
            self.db.insert(
                "INSERT INTO materialization (spec_id, source_object_id, dialect, target_table,"
                " rows_written, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (spec_id, object_id, dialect, table, rows, actor, utcnow()))
            self.audit.append(actor, "spec.materialized", "mapping_spec", spec_id, {
                "source_object_id": object_id, "dialect": dialect, "table": table, "rows": rows})
