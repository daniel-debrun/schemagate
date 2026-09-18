from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from schemagate.errors import ProviderResponseError
from schemagate.ingest.profile import profile_table
from schemagate.llm import (
    FakeProvider,
    HeuristicProvider,
    MappingRequest,
    load_prompt,
    validate_response,
)
from schemagate.llm.anthropic_provider import AnthropicProvider
from schemagate.models import Tier
from schemagate.resolve import ModelResolver, ResolveContext


@pytest.fixture
def request_(invoice_schema):
    cols = profile_table(["QtyShipped", "Remarks"], [["3", "rush"], ["4", "none"]])
    return MappingRequest.build(invoice_schema, cols, {"invoice_number": "Inv"})


def entry(**kw):
    base = {"source_column": "QtyShipped", "target_field": "quantity", "confidence": 0.7,
            "rationale": "values 3, 4 are counts", "evidence": ["sample values: 3, 4"]}
    base.update(kw)
    return base


def test_invalid_json_raises(request_):
    with pytest.raises(ProviderResponseError, match="not valid JSON"):
        validate_response("Sure! Here is the mapping: {", request_, 0.75)


@pytest.mark.parametrize("payload", [[], {"maps": []}, {"mappings": {}}, {"mappings": ["x"]}])
def test_wrong_shape_raises(request_, payload):
    with pytest.raises(ProviderResponseError):
        validate_response(json.dumps(payload), request_, 0.75)


@pytest.mark.parametrize("bad", [
    {"confidence": "high"}, {"confidence": True}, {"rationale": ""}, {"target_field": 3},
    {"evidence": "x"}, {"source_column": None},
])
def test_field_types_are_strict(request_, bad):
    with pytest.raises(ProviderResponseError):
        validate_response(json.dumps({"mappings": [entry(**bad)]}), request_, 0.75)


def test_fenced_json_is_accepted(request_):
    text = "```json\n" + json.dumps({"mappings": [entry()]}) + "\n```"
    (v,) = validate_response(text, request_, 0.75)
    assert v.target_field == "quantity"


def test_hallucinated_target_is_rejected(request_):
    (v,) = validate_response(json.dumps({"mappings": [entry(target_field="shipped_qty")]}), request_, 0.75)
    assert v.target_field is None
    assert v.confidence == 0.0
    assert "hallucinated" in v.issues[0]


def test_target_already_mapped_is_rejected(request_):
    (v,) = validate_response(json.dumps({"mappings": [entry(target_field="invoice_number")]}),
                             request_, 0.75)
    assert v.target_field is None


def test_over_cap_confidence_is_clamped(request_):
    (v,) = validate_response(json.dumps({"mappings": [entry(confidence=0.99)]}), request_, 0.75)
    assert v.confidence == 0.75
    assert v.raw_confidence == 0.99
    assert any("clamped" in i for i in v.issues)
    (u,) = validate_response(json.dumps({"mappings": [entry(confidence=7)]}), request_, None)
    assert u.confidence == 1.0


def test_uncited_mapping_is_limited(request_):
    (v,) = validate_response(json.dumps({"mappings": [entry(evidence=[])]}), request_, 0.75)
    assert v.confidence == 0.5


def test_unknown_and_duplicate_columns_are_ignored(request_):
    payload = {"mappings": [entry(), entry(target_field=None, confidence=0.1),
                            entry(source_column="Ghost")]}
    out = validate_response(json.dumps(payload), request_, 0.75)
    assert [v.source_column for v in out] == ["QtyShipped"]
    assert out[0].target_field == "quantity"


def test_model_resolver_turns_provider_errors_into_unresolved(invoice_schema):
    cols = profile_table(["QtyShipped"], [["3"]])
    resolver = ModelResolver(FakeProvider("not json at all"))
    (p,) = resolver.resolve(ResolveContext(invoice_schema), cols)
    assert p.target_field is None
    assert p.tier is Tier.UNRESOLVED
    assert "rejected" in p.rationale
    assert p.model_id == "fake-model"


def test_model_resolver_records_prompt_version_and_model(invoice_schema):
    cols = profile_table(["QtyShipped"], [["3"]])
    resolver = ModelResolver(FakeProvider({"mappings": [entry(confidence=0.95)]}, model_id="m-1"))
    (p,) = resolver.resolve(ResolveContext(invoice_schema), cols)
    assert (p.target_field, p.tier, p.confidence) == ("quantity", Tier.MODEL, 0.75)
    assert p.prompt_version == load_prompt().version
    assert p.proposer == "model:m-1"
    assert p.evidence["raw_confidence"] == 0.95


def test_prompt_template_versioning():
    prompt = load_prompt()
    assert prompt.version.startswith("map_columns/v1+")
    assert "ALREADY MAPPED" in prompt.user
    assert "never applied automatically" in prompt.system


def test_heuristic_output_passes_strict_validation(request_):
    text = HeuristicProvider().complete(request_).text
    out = validate_response(text, request_, 0.75)
    assert {v.source_column for v in out} == {"QtyShipped", "Remarks"}


class _FakeMessages:
    def __init__(self, stop_reason="end_turn"):
        self.calls = []
        self.stop_reason = stop_reason

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason, model=kwargs["model"],
            content=[SimpleNamespace(type="text", text='{"mappings": []}')])


def test_anthropic_provider_request_shape(request_):
    messages = _FakeMessages()
    provider = AnthropicProvider(client=SimpleNamespace(messages=messages))
    assert provider.model_id == "claude-sonnet-5"
    resp = provider.complete(request_)
    assert resp.text == '{"mappings": []}'
    call = messages.calls[0]
    assert call["system"] == request_.system_prompt
    assert call["messages"][0]["role"] == "user"


def test_anthropic_provider_refusal_is_a_response_error(request_):
    provider = AnthropicProvider(client=SimpleNamespace(messages=_FakeMessages("refusal")))
    with pytest.raises(ProviderResponseError):
        provider.complete(request_)
