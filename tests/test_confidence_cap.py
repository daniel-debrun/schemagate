from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from schemagate.config import ConfidencePolicy
from schemagate.ingest.profile import profile_table
from schemagate.llm import FakeProvider
from schemagate.models import MappingProposal, Tier
from schemagate.resolve import ModelResolver, ResolverChain, resolve_collisions
from schemagate.schema import load_schema

from .conftest import EXAMPLES

SCHEMA = load_schema(EXAMPLES / "schemas" / "supplier_invoice.yaml")


def test_default_tiers():
    p = ConfidencePolicy()
    assert (p.exact, p.dictionary, p.synonym, p.model_cap) == (0.95, 0.90, 0.85, 0.75)


@pytest.mark.parametrize("kwargs", [
    {"model_cap": 0.85},
    {"model_cap": 0.9},
    {"synonym": 0.7, "model_cap": 0.75},
    {"exact": 0.8, "dictionary": 0.9},
    {"gate_threshold": 0.7},
])
def test_policy_rejects_cap_not_below_deterministic_tiers(kwargs):
    with pytest.raises(ValidationError):
        ConfidencePolicy(**kwargs)


def test_policy_accepts_custom_ordering():
    p = ConfidencePolicy(exact=0.99, dictionary=0.9, synonym=0.8, model_cap=0.6, gate_threshold=0.8)
    assert p.model_cap < p.synonym


column_names = st.sampled_from(["Colour", "Amt Due", "Ref 2", "Stuff", "Notes", "Qty Shipped",
                                "Something Date", "Invoice Number", "Units"])
targets = st.one_of(st.none(), st.sampled_from([*SCHEMA.field_names, "made_up_field"]))


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    cols=st.lists(column_names, min_size=1, max_size=6, unique=True),
    answers=st.lists(st.tuples(targets, st.floats(min_value=-5, max_value=5, allow_nan=False)),
                     min_size=6, max_size=6),
    cap=st.sampled_from([0.5, 0.6, 0.75, 0.84]),
)
def test_model_proposals_never_exceed_cap_or_outrank_deterministic(cols, answers, cap):
    policy = ConfidencePolicy(model_cap=cap)

    def respond(request):
        return json.dumps({"mappings": [
            {"source_column": c.name, "target_field": t, "confidence": conf,
             "rationale": "random", "evidence": ["x"]}
            for c, (t, conf) in zip(request.columns, answers)]})

    rows = [["1" for _ in cols], ["2" for _ in cols]]
    chain = ResolverChain.default(ModelResolver(FakeProvider(respond)))
    result = chain.run(SCHEMA, profile_table(cols, rows), policy)
    deterministic_floor = min(policy.exact, policy.dictionary, policy.synonym) - policy.compat_penalty
    for p in result.proposals:
        if p.tier is Tier.MODEL:
            assert 0.0 <= p.confidence <= cap
            assert p.target_field in SCHEMA.field_names or p.conflict is not None
        if p.target_field is not None and p.tier.deterministic:
            assert p.confidence >= deterministic_floor
    targets_held = [p.target_field for p in result.proposals if p.target_field]
    assert len(targets_held) == len(set(targets_held))


@given(
    model_conf=st.floats(min_value=0, max_value=1),
    tier=st.sampled_from([Tier.EXACT, Tier.DICTIONARY, Tier.SYNONYM]),
)
def test_collision_never_lets_capped_model_beat_deterministic(model_conf, tier):
    policy = ConfidencePolicy()
    det_conf = {Tier.EXACT: policy.exact, Tier.DICTIONARY: policy.dictionary,
                Tier.SYNONYM: policy.synonym}[tier]
    model = MappingProposal("a_model_col", "quantity", min(model_conf, policy.model_cap), Tier.MODEL, "m")
    det = MappingProposal("z_det_col", "quantity", det_conf, tier, "d")
    out = {p.source_column: p for p in resolve_collisions([model, det])}
    assert out["z_det_col"].target_field == "quantity"
    assert out["a_model_col"].target_field is None
    assert out["a_model_col"].conflict["winner"] == "z_det_col"


def test_store_rejects_model_proposal_over_cap(service, invoice_schema):
    feed = service.register_feed("f", invoice_schema, "alice")
    bad = MappingProposal("Qty", "quantity", 0.9, Tier.MODEL, "sneaky")
    with pytest.raises(ValueError, match="exceeds cap"):
        service.create_spec(feed, [bad], "alice", source_object_id=None, source_profiles=[])
