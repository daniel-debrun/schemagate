from __future__ import annotations

import json

from benchmarks.generate import generate_suite, generate_variant, load_benchmark_schemas
from benchmarks.run import chain_model_uncapped, chain_schemagate, evaluate
from benchmarks.valentine import load_valentine


def test_generator_is_deterministic_and_has_ground_truth():
    schema = load_benchmark_schemas()[0]
    a = generate_variant(schema, 3, seed=1)
    b = generate_variant(schema, 3, seed=1)
    assert a.columns == b.columns and a.rows == b.rows
    assert set(a.truth) == set(a.columns)
    assert all(t is None or t in schema.field_names for t in a.truth.values())
    assert any(t is None for t in a.truth.values())
    required = {f.name for f in schema.fields if f.required}
    assert required <= {t for t in a.truth.values() if t}


def test_evaluate_small_suite():
    suite = generate_suite(2, seed=3)
    capped = evaluate("schemagate", chain_schemagate, suite)
    uncapped = evaluate("model_uncapped", chain_model_uncapped, suite)
    assert capped["columns"] == uncapped["columns"] > 0
    assert capped["deterministic_resolution"] > 0
    assert uncapped["deterministic_resolution"] == 0
    deterministic = sum(v["n"] for t, v in capped["by_tier"].items() if t in ("exact", "dictionary", "synonym"))
    assert capped["gate_queue_size"] == deterministic


def test_valentine_loader_on_a_tiny_fixture(tmp_path):
    pair = tmp_path / "pair_a"
    pair.mkdir()
    (pair / "x_source.csv").write_text("cust_nm,amt,junk\nAnn,1.5,z\nBo,2.0,y\n")
    (pair / "x_target.csv").write_text("Customer Name,Amount\nCy,3.0\n")
    (pair / "x_mapping.json").write_text(json.dumps({"matches": [
        {"source_table": "x_source", "source_column": "cust_nm",
         "target_table": "x_target", "target_column": "Customer Name"},
        {"source_table": "x_source", "source_column": "amt",
         "target_table": "x_target", "target_column": "Amount"}]}))
    ((schema, variant),) = load_valentine(tmp_path)
    assert schema.field_names == ["customer_name", "amount"]
    assert schema.field("amount").type == "number"
    assert variant.truth == {"cust_nm": "customer_name", "amt": "amount", "junk": None}


def test_agent_tasks_round_trip(tmp_path):
    from benchmarks.agent.store import ReplayProvider, load_tasks, prepare, save_answer

    from schemagate.resolve import ModelResolver, ResolverChain

    config = {"variants": 1, "seed": 3, "valentine": None, "per_group": 1}
    doc = prepare(config, data_dir=tmp_path)
    assert doc["tasks"] and load_tasks(tmp_path) == doc
    assert all("truth" not in t and t["columns"] for t in doc["tasks"])
    for task in doc["tasks"]:
        answer = {"mappings": [{"source_column": c, "target_field": None, "confidence": 0.1,
                                "rationale": "none", "evidence": []} for c in task["columns"]]}
        save_answer("t", task, json.dumps(answer), "test-model", data_dir=tmp_path)
    replay = ReplayProvider("t", data_dir=tmp_path)
    suite = generate_suite(1, seed=3)
    evaluate("agent", lambda _s: ResolverChain.default(ModelResolver(replay)), suite)
    assert replay.served == len(doc["tasks"]) and replay.missing == 0
