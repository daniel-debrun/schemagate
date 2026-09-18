from __future__ import annotations

import sqlite3
from itertools import pairwise

import pytest

from schemagate.store.audit import GENESIS_HASH, chain_head

from .conftest import propose_spec


def test_every_transition_is_audited_and_chain_verifies(service, invoice_schema):
    spec_id, _ = propose_spec(service, invoice_schema, "northwind")
    service.approve_spec(spec_id, "bob")
    actions = [e["action"] for e in service.audit.entries()]
    assert actions == ["feed.registered", "source_object.ingested", "spec.proposed",
                       "spec.approval_recorded", "spec.approved", "spec.activated"]
    entries = service.audit.entries()
    assert entries[0]["prev_hash"] == GENESIS_HASH
    assert all(b["prev_hash"] == a["hash"] for a, b in pairwise(entries))
    result = service.audit.verify()
    assert result.ok and result.entries == 6
    assert chain_head(service.db) == entries[-1]["hash"]


def test_audit_table_is_append_only(service, invoice_schema):
    propose_spec(service, invoice_schema, "northwind")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        service.db.execute("UPDATE mapping_audit SET actor = 'mallory' WHERE id = 1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        service.db.execute("DELETE FROM mapping_audit WHERE id = 1")


def _drop_guards(service):
    service.db.execute("DROP TRIGGER mapping_audit_no_update")
    service.db.execute("DROP TRIGGER mapping_audit_no_delete")


def test_tampered_payload_is_detected(service, invoice_schema):
    spec_id, _ = propose_spec(service, invoice_schema, "northwind")
    service.approve_spec(spec_id, "bob")
    _drop_guards(service)
    target = service.audit.entries(action="spec.approved")[0]
    service.db.execute("UPDATE mapping_audit SET payload_json = ? WHERE id = ?",
                       ('{"approvers":["mallory"],"required":1}', target["id"]))
    result = service.audit.verify()
    assert not result.ok
    assert result.first_bad_id == target["id"]
    assert "hash" in result.reason


def test_rehashed_forgery_breaks_the_next_link(service, invoice_schema):
    from schemagate.store.audit import entry_hash

    spec_id, _ = propose_spec(service, invoice_schema, "northwind")
    service.approve_spec(spec_id, "bob")
    _drop_guards(service)
    rows = service.audit.entries()
    victim = rows[2]
    forged_payload = '{"forged":true}'
    forged_hash = entry_hash(victim["prev_hash"], victim["ts"], victim["actor"], victim["action"],
                             victim["entity_type"], victim["entity_id"], forged_payload)
    service.db.execute("UPDATE mapping_audit SET payload_json = ?, hash = ? WHERE id = ?",
                       (forged_payload, forged_hash, victim["id"]))
    result = service.audit.verify()
    assert not result.ok and result.first_bad_id == rows[3]["id"]


def test_truncation_is_detected_against_anchored_head(service, invoice_schema):
    spec_id, _ = propose_spec(service, invoice_schema, "northwind")
    service.approve_spec(spec_id, "bob")
    anchor = chain_head(service.db)
    _drop_guards(service)
    last = service.audit.entries()[-1]["id"]
    service.db.execute("DELETE FROM mapping_audit WHERE id = ?", (last,))
    assert service.audit.verify().ok
    assert chain_head(service.db) != anchor
