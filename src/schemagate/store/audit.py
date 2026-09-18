from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from schemagate.store.db import Database

GENESIS_HASH = "0" * 64


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def entry_hash(prev_hash: str, ts: str, actor: str, action: str, entity_type: str,
               entity_id: str, payload_json: str) -> str:
    material = canonical_json({
        "prev_hash": prev_hash, "ts": ts, "actor": actor, "action": action,
        "entity_type": entity_type, "entity_id": entity_id, "payload": payload_json,
    })
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    entries: int
    first_bad_id: int | None = None
    reason: str | None = None


def clean_actor(actor: str) -> str:
    """Collapse whitespace in an actor name and refuse blank ones."""
    cleaned = " ".join(str(actor).split())
    if not cleaned:
        from schemagate.errors import ApprovalPolicyError

        raise ApprovalPolicyError("actor must be a non-blank name")
    return cleaned


def actor_key(actor: str) -> str:
    """Identity used to compare actors: ``"Dana "`` and ``"dana"`` are the same person."""
    return " ".join(str(actor).split()).casefold()


class AuditLog:
    """Append-only, hash-chained audit log. Each entry commits to the previous entry's hash."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def append(self, actor: str, action: str, entity_type: str, entity_id: str | int,
               payload: dict[str, Any]) -> str:
        actor = clean_actor(actor)
        with self.db.transaction():
            last = self.db.one("SELECT hash FROM mapping_audit ORDER BY id DESC LIMIT 1")
            prev = last["hash"] if last else GENESIS_HASH
            ts = utcnow()
            payload_json = canonical_json(payload)
            h = entry_hash(prev, ts, actor, action, entity_type, str(entity_id), payload_json)
            self.db.insert(
                "INSERT INTO mapping_audit (ts, actor, action, entity_type, entity_id, payload_json,"
                " prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, actor, action, entity_type, str(entity_id), payload_json, prev, h),
            )
            return h

    def entries(self, entity_type: str | None = None, entity_id: str | int | None = None,
                action: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM mapping_audit WHERE 1=1"
        params: list[Any] = []
        if entity_type is not None:
            sql += " AND entity_type = ?"
            params.append(entity_type)
        if entity_id is not None:
            sql += " AND entity_id = ?"
            params.append(str(entity_id))
        if action is not None:
            sql += " AND action = ?"
            params.append(action)
        rows = self.db.query(sql + " ORDER BY id", params)
        for r in rows:
            r["payload"] = json.loads(r["payload_json"])
        return rows

    def verify(self) -> ChainVerification:
        prev = GENESIS_HASH
        rows = self.db.query("SELECT * FROM mapping_audit ORDER BY id")
        for r in rows:
            if r["prev_hash"] != prev:
                return ChainVerification(False, len(rows), r["id"], "prev_hash does not match previous entry")
            expected = entry_hash(prev, r["ts"], r["actor"], r["action"], r["entity_type"],
                                  r["entity_id"], r["payload_json"])
            if expected != r["hash"]:
                return ChainVerification(False, len(rows), r["id"], "entry content does not match its hash")
            prev = r["hash"]
        return ChainVerification(True, len(rows))


def chain_head(db: Database) -> str:
    last = db.one("SELECT hash FROM mapping_audit ORDER BY id DESC LIMIT 1")
    return last["hash"] if last else GENESIS_HASH
