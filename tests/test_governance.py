from __future__ import annotations

import pytest

from schemagate.config import ApprovalPolicy
from schemagate.errors import (
    ApprovalPolicyError,
    InvalidTransitionError,
    UnapprovedSpecError,
)
from schemagate.models import MappingProposal, Tier
from schemagate.store import GovernanceService

from .conftest import propose_spec


def test_new_private_table_needs_one_non_proposer_approval(service, invoice_schema):
    spec_id, _ = propose_spec(service, invoice_schema, "northwind")
    with pytest.raises(ApprovalPolicyError, match="cannot review"):
        service.approve_spec(spec_id, "alice")
    assert service.approve_spec(spec_id, "bob") == "approved"
    spec = service.verify_approved(spec_id)
    assert spec.required_approvals == 1
    assert service.feed("northwind")["active_spec_id"] == spec_id


def test_duplicate_approver_rejected(service, invoice_schema):
    first, _ = propose_spec(service, invoice_schema, "northwind")
    service.approve_spec(first, "bob")
    shared, _ = propose_spec(service, invoice_schema, "cedar")
    assert service.approve_spec(shared, "bob") == "pending"
    with pytest.raises(ApprovalPolicyError, match="already approved"):
        service.approve_spec(shared, "bob")
    assert service.spec(shared).status == "pending"
    with pytest.raises(UnapprovedSpecError):
        service.verify_approved(shared)


def test_shared_table_needs_two_distinct_non_proposer_approvers(service, invoice_schema):
    first, _ = propose_spec(service, invoice_schema, "northwind")
    service.approve_spec(first, "bob")
    second, _ = propose_spec(service, invoice_schema, "bluepeak", actor="carol")
    with pytest.raises(ApprovalPolicyError):
        service.approve_spec(second, "carol")
    assert service.approve_spec(second, "bob") == "pending"
    assert service.spec(second).required_approvals == 2
    assert service.approve_spec(second, "dave") == "approved"
    approvers = {a["approver"] for a in service.approvals(second)}
    assert approvers == {"bob", "dave"}
    audit = service.audit.entries("mapping_spec", second, action="spec.approved")
    assert audit[0]["payload"]["approvers"] == ["bob", "dave"]


def test_published_schema_needs_two_approvers_from_the_start(service, invoice_schema):
    published = invoice_schema.model_copy(update={"published": True, "name": "pub", "table": "pub_lines"})
    spec_id, _ = propose_spec(service, published, "f1")
    assert service.spec(spec_id).required_approvals == 2
    assert service.approve_spec(spec_id, "bob") == "pending"
    assert service.approve_spec(spec_id, "carol") == "approved"


def test_self_approval_can_be_enabled_explicitly(invoice_schema):
    svc = GovernanceService.open("sqlite:///:memory:", approval_policy=ApprovalPolicy(allow_self_approval=True))
    spec_id, _ = propose_spec(svc, invoice_schema, "solo")
    assert svc.approve_spec(spec_id, "alice") == "approved"


def _spec_with_model_proposal(service, schema):
    feed = service.register_feed("m", schema, "alice")
    props = [
        MappingProposal("Inv", "invoice_number", 0.9, Tier.DICTIONARY, "alias"),
        MappingProposal("QtyShipped", "quantity", 0.7, Tier.MODEL, "looks like counts",
                        proposer="model:fake", model_id="fake", prompt_version="p/v1"),
        MappingProposal("Stuff", None, 0.0, Tier.UNRESOLVED, "none", extension_column="ext_stuff"),
    ]
    spec_id = service.create_spec(feed, props, "alice", source_object_id=None, source_profiles=[])
    ids = {p["source_column"]: p["id"] for p in service.spec(spec_id).proposals}
    return spec_id, ids


def test_model_proposals_require_explicit_review(service, invoice_schema):
    spec_id, ids = _spec_with_model_proposal(service, invoice_schema)
    with pytest.raises(ApprovalPolicyError, match="QtyShipped"):
        service.approve_spec(spec_id, "bob")
    with pytest.raises(ApprovalPolicyError):
        service.accept_proposal(ids["QtyShipped"], "alice")
    service.accept_proposal(ids["QtyShipped"], "bob")
    assert service.approve_spec(spec_id, "bob") == "approved"
    entry = service.audit.entries("mapping_proposal", ids["QtyShipped"])[0]
    assert entry["action"] == "proposal.accepted" and entry["payload"]["tier"] == "model"
    proposed = service.audit.entries("mapping_spec", spec_id, action="spec.proposed")[0]["payload"]
    model_row = next(p for p in proposed["proposals"] if p["tier"] == "model")
    assert model_row["model_id"] == "fake" and model_row["prompt_version"] == "p/v1"


def test_demote_and_override(service, invoice_schema):
    spec_id, ids = _spec_with_model_proposal(service, invoice_schema)
    service.demote_proposal(ids["QtyShipped"], "bob", "not confident")
    service.override_proposal(ids["Stuff"], "quantity", "bob", "sender confirmed by email")
    with pytest.raises(InvalidTransitionError, match="held by"):
        service.override_proposal(ids["QtyShipped"], "invoice_number", "bob", "x")
    with pytest.raises(ValueError):
        service.override_proposal(ids["QtyShipped"], "no_such_field", "bob", "x")
    spec = service.spec(spec_id)
    by = {p["source_column"]: p for p in spec.proposals}
    assert by["QtyShipped"]["target_field"] is None
    assert by["QtyShipped"]["extension_column"] == "ext_qty_shipped"
    assert by["Stuff"]["tier"] == "human" and by["Stuff"]["confidence"] == 1.0
    assert service.approve_spec(spec_id, "bob") == "approved"
    with pytest.raises(InvalidTransitionError, match="frozen"):
        service.demote_proposal(ids["Stuff"], "carol", "late")


def test_reject_is_terminal(service, invoice_schema):
    spec_id, _ = propose_spec(service, invoice_schema, "northwind")
    service.reject_spec(spec_id, "bob", "wrong feed")
    assert service.spec(spec_id).status == "rejected"
    with pytest.raises(InvalidTransitionError):
        service.approve_spec(spec_id, "carol")
    with pytest.raises(UnapprovedSpecError):
        service.verify_approved(spec_id)


def test_status_flag_alone_does_not_count_as_approval(service, invoice_schema):
    spec_id, _ = propose_spec(service, invoice_schema, "northwind")
    service.db.execute("UPDATE mapping_spec SET status = 'approved' WHERE id = ?", (spec_id,))
    with pytest.raises(UnapprovedSpecError, match="valid approvals"):
        service.verify_approved(spec_id)
    service.db.execute("INSERT INTO mapping_approval (spec_id, approver, decision, created_at)"
                       " VALUES (?, 'mallory', 'approve', 'now')", (spec_id,))
    with pytest.raises(UnapprovedSpecError, match="audit log"):
        service.verify_approved(spec_id)


def test_switch_requires_verified_approval_and_newer_version(service, invoice_schema):
    v1, _ = propose_spec(service, invoice_schema, "northwind")
    service.approve_spec(v1, "bob")
    v2, _ = propose_spec(service, invoice_schema, "northwind", digest="b" * 64)
    with pytest.raises(UnapprovedSpecError):
        service.switch_active(v2, "bob")
    service.approve_spec(v2, "bob")
    assert service.feed("northwind")["active_spec_id"] == v1
    service.switch_active(v2, "carol")
    assert service.feed("northwind")["active_spec_id"] == v2
    assert service.spec(v1).status == "superseded"
    with pytest.raises(InvalidTransitionError):
        service.switch_active(v1, "carol")


def test_duplicate_content_is_skipped(service, invoice_schema):
    from .conftest import invoice_rows, make_table

    feed = service.register_feed("northwind", invoice_schema, "alice")
    t = make_table(["a"], [["1"]], digest="e" * 64)
    first = service.record_source_object(feed, t, "alice")
    again = service.record_source_object(feed, t, "alice")
    assert first == (first[0], True) and again == (first[0], False)
    assert len(invoice_rows()) == 4


def test_feed_cannot_be_rebound_to_another_schema(service, invoice_schema):
    service.register_feed("northwind", invoice_schema, "alice")
    other = invoice_schema.model_copy(update={"name": "other"})
    with pytest.raises(InvalidTransitionError):
        service.register_feed("northwind", other, "alice")
