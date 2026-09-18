from __future__ import annotations

from schemagate.config import ConfidencePolicy
from schemagate.ingest.profile import profile_table
from schemagate.llm import FakeProvider, HeuristicProvider
from schemagate.models import Tier
from schemagate.resolve import (
    DictionaryResolver,
    ExactResolver,
    ModelResolver,
    ResolveContext,
    ResolverChain,
    SynonymResolver,
    check_compat,
)
from schemagate.schema import parse_schema


def profiles(columns, rows):
    return profile_table(columns, rows)


def test_exact_resolver(invoice_schema):
    ctx = ResolveContext(invoice_schema)
    props = ExactResolver().resolve(ctx, profiles(["Invoice Number", "PO Number", "Other"],
                                                  [["A", "PO-1", "x"]]))
    assert {p.source_column: p.target_field for p in props} == {
        "Invoice Number": "invoice_number", "PO Number": "po_number"}
    assert all(p.tier is Tier.EXACT and p.confidence == 0.95 for p in props)


def test_dictionary_resolver(invoice_schema):
    ctx = ResolveContext(invoice_schema)
    props = DictionaryResolver().resolve(ctx, profiles(["Vendor-ID", "Price Each", "Invoice Number"],
                                                       [["V1", "1.00", "A"]]))
    got = {p.source_column: p.target_field for p in props}
    assert got == {"Vendor-ID": "supplier_id", "Price Each": "unit_price"}
    assert all(p.tier is Tier.DICTIONARY and p.confidence == 0.90 for p in props)
    assert props[0].evidence["alias"] == "vendor id"


def test_synonym_resolver_expansion_and_reordering(invoice_schema):
    ctx = ResolveContext(invoice_schema)
    cols = ["Qty", "Dt Invoice", "DueDt", "Ccy"]
    props = SynonymResolver().resolve(ctx, profiles(cols, [["1", "2026-01-01", "2026-02-01", "USD"]]))
    got = {p.source_column: p.target_field for p in props}
    assert got == {"Qty": "quantity", "Dt Invoice": "invoice_date", "DueDt": "due_date", "Ccy": "currency"}
    assert all(p.tier is Tier.SYNONYM and p.confidence == 0.85 for p in props)


def test_synonym_resolver_refuses_ties_and_weak_matches():
    schema = parse_schema({"name": "s", "table": "t", "fields": [
        {"name": "supplier_id"}, {"name": "supplier_name"}, {"name": "ship_date", "type": "date"}]})
    ctx = ResolveContext(schema)
    props = SynonymResolver().resolve(ctx, profiles(["Supplier", "Ship Date Planned"],
                                                    [["Acme", "2026-01-01"]]))
    assert props == []


def test_compat_ok_downgrade_veto(invoice_schema):
    date_field = invoice_schema.field("invoice_date")
    good = profiles(["d"], [["2026-01-01"], ["2026-01-02"]])[0]
    half = profiles(["d"], [["2026-01-01"], ["2026-01-02"], ["2026-01-03"], ["tbd"]])[0]
    bad = profiles(["d"], [["north"], ["south"]])[0]
    assert check_compat(good, date_field).status == "ok"
    assert check_compat(half, date_field).status == "downgrade"
    assert check_compat(bad, date_field).status == "veto"
    currency = invoice_schema.field("currency")
    assert check_compat(profiles(["c"], [["USD"], ["EUR"]])[0], currency).status == "ok"
    assert check_compat(profiles(["c"], [["dollars"], ["euros"]])[0], currency).status == "veto"
    po = invoice_schema.field("po_number")
    assert check_compat(profiles(["p"], [["PO-1"], ["PO-22"]])[0], po).status == "ok"
    assert check_compat(profiles(["p"], [["1"], ["22"]])[0], po).status == "veto"


def test_compat_veto_passes_column_on_and_is_recorded(invoice_schema):
    cols = ["Invoice Date", "Notes"]
    rows = [["not a date", "x"], ["also not", "y"]]
    result = ResolverChain.default().run(invoice_schema, profiles(cols, rows))
    prop = result.by_column()["Invoice Date"]
    assert prop.target_field is None
    assert prop.tier is Tier.UNRESOLVED
    assert prop.extension_column == "ext_invoice_date"
    assert prop.evidence["vetoes"][0]["tier"] == "exact"


def test_compat_downgrade_lowers_confidence(invoice_schema):
    rows = [["2026-01-01"], ["2026-01-02"], ["2026-01-03"], ["unknown"]]
    result = ResolverChain.default().run(invoice_schema, profiles(["Invoice Date"], rows))
    prop = result.proposals[0]
    assert prop.target_field == "invoice_date"
    assert prop.confidence == 0.80
    assert "downgraded" in prop.rationale


def test_model_only_sees_unresolved_columns(invoice_schema):
    fake = FakeProvider({"mappings": []})
    chain = ResolverChain.default(ModelResolver(fake))
    cols = ["Invoice Number", "Qty", "Mystery Column"]
    chain.run(invoice_schema, profiles(cols, [["A", "1", "z"]]))
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert [c.name for c in request.columns] == ["Mystery Column"]
    assert request.already_mapped == {"invoice_number": "Invoice Number", "quantity": "Qty"}
    assert "Mystery Column" in request.user_prompt


def test_model_not_called_when_everything_resolves(invoice_schema):
    fake = FakeProvider({"mappings": []})
    ResolverChain.default(ModelResolver(fake)).run(invoice_schema, profiles(["Qty"], [["1"]]))
    assert fake.requests == []


def test_heuristic_provider_maps_abbreviations(invoice_schema):
    chain = ResolverChain.default(ModelResolver(HeuristicProvider()))
    cols = ["QtyShipped", "PORef", "Wind Speed"]
    rows = [["4", "PO-100", "calm"], ["5", "PO-101", "gusty"]]
    result = chain.run(invoice_schema, profiles(cols, rows), ConfidencePolicy())
    got = result.by_column()
    assert got["QtyShipped"].target_field == "quantity"
    assert got["PORef"].target_field == "po_number"
    assert got["Wind Speed"].target_field is None
    assert got["QtyShipped"].tier is Tier.MODEL
    assert got["QtyShipped"].confidence <= 0.75
    assert got["QtyShipped"].prompt_version.startswith("map_columns/v1+")
    assert got["QtyShipped"].model_id == "heuristic-v1"
