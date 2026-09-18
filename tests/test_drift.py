from __future__ import annotations

import random

from schemagate.drift import detect_drift, propose_expand, psi
from schemagate.ingest.profile import profile_table
from schemagate.models import MappingProposal, Tier
from schemagate.resolve import ResolverChain

from .conftest import INVOICE_COLUMNS, invoice_rows, make_table, propose_spec


def _mapping(schema, columns, rows):
    profiles = profile_table(columns, rows)
    return ResolverChain.default().run(schema, profiles).proposals, profiles


def kinds(events):
    return {(e.kind, e.source_column, e.severity) for e in events}


def test_identical_layout_has_no_drift(invoice_schema):
    mapping, base = _mapping(invoice_schema, INVOICE_COLUMNS, invoice_rows())
    new = profile_table(INVOICE_COLUMNS, invoice_rows(start=9))
    assert detect_drift(invoice_schema, mapping, base, new) == []


def test_added_and_removed_columns(invoice_schema):
    mapping, base = _mapping(invoice_schema, INVOICE_COLUMNS, invoice_rows())
    cols = [c for c in INVOICE_COLUMNS if c not in ("Currency", "Warehouse")] + ["Carrier"]
    rows = [[v for c, v in zip(INVOICE_COLUMNS, r) if c not in ("Currency", "Warehouse")] + ["UPS"]
            for r in invoice_rows()]
    events = detect_drift(invoice_schema, mapping, base, profile_table(cols, rows))
    got = kinds(events)
    assert ("removed", "Currency", "breaking") in got
    assert ("removed", "Warehouse", "warning") in got
    assert ("added", "Carrier", "warning") in got
    assert events[0].severity == "breaking"


def test_rename_detected_via_resolver_and_profile(invoice_schema):
    mapping, base = _mapping(invoice_schema, INVOICE_COLUMNS, invoice_rows())
    cols = ["Invoice Number", "Line", "Invoice Date", "Vendor ID", "Quantity Invoiced",
            "Unit Price (USD)", "Line Total (USD)", "Currency", "Warehouse"]
    events = detect_drift(invoice_schema, mapping, base, profile_table(cols, invoice_rows()))
    (rename,) = [e for e in events if e.kind == "renamed"]
    assert rename.detail["from"] == "Qty" and rename.detail["to"] == "Quantity Invoiced"
    assert rename.detail["target_field"] == "quantity"
    assert rename.severity == "breaking"
    assert not [e for e in events if e.kind in ("added", "removed")]


def test_type_and_format_changes(invoice_schema):
    mapping, base = _mapping(invoice_schema, INVOICE_COLUMNS, invoice_rows())
    rows = invoice_rows()
    for r in rows:
        r[2] = r[2][5:7] + "/" + r[2][8:10] + "/" + r[2][:4]
        r[4] = "lots"
    got = kinds(detect_drift(invoice_schema, mapping, base, profile_table(INVOICE_COLUMNS, rows)))
    assert ("format_changed", "Invoice Date", "breaking") in got
    assert ("type_changed", "Qty", "breaking") in got


def test_null_rate_shift(invoice_schema):
    mapping, base = _mapping(invoice_schema, INVOICE_COLUMNS, invoice_rows())
    rows = invoice_rows()
    for r in rows[:3]:
        r[8] = ""
    got = kinds(detect_drift(invoice_schema, mapping, base, profile_table(INVOICE_COLUMNS, rows)))
    assert ("null_rate_shift", "Warehouse", "warning") in got


def test_distribution_shift_uses_psi_on_baseline_bins(invoice_schema):
    rng = random.Random(7)
    cols = ["Invoice Number", "Qty", "Currency"]

    def rows(mu, currencies):
        return [[f"I{i}", str(max(1, int(rng.gauss(mu, 5)))), rng.choice(currencies)] for i in range(300)]

    mapping, base = _mapping(invoice_schema, cols, rows(50, ["USD", "EUR"]))
    same = rows(50, ["USD", "EUR"])
    assert not [e for e in detect_drift(invoice_schema, mapping, base, profile_table(cols, same),
                                        values={c: [r[i] for r in same] for i, c in enumerate(cols)})
                if e.kind == "distribution_shift"]
    shifted = rows(90, ["GBP"])
    events = detect_drift(invoice_schema, mapping, base, profile_table(cols, shifted),
                          values={c: [r[i] for r in shifted] for i, c in enumerate(cols)})
    shifted_cols = {e.source_column for e in events if e.kind == "distribution_shift"}
    assert shifted_cols == {"Qty", "Currency"}


def test_psi_basics():
    assert psi([0.5, 0.5], [0.5, 0.5]) == 0
    assert psi([0.9, 0.1], [0.1, 0.9]) > 1


def test_expand_remediation_then_switch(service, invoice_schema):
    v1, _ = propose_spec(service, invoice_schema, "cedar")
    service.approve_spec(v1, "bob")
    spec = service.spec(v1)
    cols = ["Invoice Number", "Line", "Invoice Date", "Vendor ID", "Quantity Invoiced",
            "Unit Price (USD)", "Line Total (USD)", "Warehouse", "Freight"]
    rows = [[*r[:7], r[8], "12.00"] for r in invoice_rows(start=5)]
    table = make_table(cols, rows, digest="f" * 64)
    obj, _ = service.record_source_object(spec.feed_id, table, "alice")
    events = detect_drift(spec.schema, spec.mapping(), spec.source_profiles, table.profiles)
    service.record_drift_events(spec.feed_id, v1, obj, [e.to_dict() for e in events], "alice")
    open_events = service.drift_events(spec.feed_id)
    assert {e["kind"] for e in open_events} == {"renamed", "removed", "added"}

    v2 = propose_expand(service, spec, obj, open_events, ResolverChain.default(), "alice")
    new = service.spec(v2)
    assert new.change_kind == "expand" and new.parent_spec_id == v1 and new.version == 2
    by = {p["source_column"]: p for p in new.proposals}
    assert by["Invoice Number"]["tier"] == "carryover"
    assert by["Quantity Invoiced"]["tier"] == "rename"
    assert by["Quantity Invoiced"]["target_field"] == "quantity"
    assert by["Freight"]["extension_column"] == "ext_freight"
    expand_audit = service.audit.entries("mapping_spec", v2, action="drift.expand_proposed")[0]
    assert expand_audit["payload"]["retained_nullable_targets"] == ["currency"]

    service.accept_proposal(by["Quantity Invoiced"]["id"], "bob")
    assert service.approve_spec(v2, "bob") == "approved"
    assert service.feed("cedar")["active_spec_id"] == v1
    service.switch_active(v2, "carol")
    assert service.feed("cedar")["active_spec_id"] == v2
    assert service.drift_events(spec.feed_id) == []
    assert {e["status"] for e in service.drift_events(spec.feed_id, status=None)} == {"remediated"}


def test_unmapped_carryover_is_not_forced(invoice_schema):
    mapping = [MappingProposal("A", None, 0.0, Tier.UNRESOLVED, "x", extension_column="ext_a")]
    base = profile_table(["A"], [["1"]])
    new = profile_table(["A"], [["x"]])
    events = detect_drift(invoice_schema, mapping, base, new)
    assert [(e.kind, e.severity) for e in events] == [("type_changed", "warning")]
