"""Run the synthetic schema-matching benchmark.

    python -m benchmarks.run --variants 50 --seed 7 --out benchmarks/results

Configurations:
  schemagate      exact -> dictionary -> synonym -> heuristic model (confidence capped at 0.75)
  model_uncapped  every column goes straight to the heuristic model; raw scores used as confidence
  schemagate_overconfident / model_overconfident_uncapped
                  same two setups, but the heuristic's confidences are inflated by +0.25 (clipped to
                  1.0) to simulate a miscalibrated model; this isolates what the cap does
  anthropic       (optional) schemagate chain with AnthropicProvider; requires ANTHROPIC_API_KEY and
                  the 'anthropic' extra; pass --with-anthropic. Not run for the committed results.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from benchmarks.generate import Variant, generate_suite
from schemagate.config import ConfidencePolicy
from schemagate.ingest.profile import profile_table
from schemagate.llm import HeuristicProvider, MappingRequest, ProviderResponse
from schemagate.resolve import UNCAPPED, ModelResolver, ResolverChain
from schemagate.schema.target import TargetSchema

POLICY = ConfidencePolicy()
DETERMINISTIC = {"exact", "dictionary", "synonym"}


def chain_schemagate(schema: TargetSchema) -> ResolverChain:
    return ResolverChain.default(ModelResolver(HeuristicProvider(vocabulary=schema.vocabulary())))


def chain_model_uncapped(schema: TargetSchema) -> ResolverChain:
    return ResolverChain([ModelResolver(HeuristicProvider(vocabulary=schema.vocabulary()),
                                        cap_override=UNCAPPED)])


class OverconfidentProvider:
    """Simulated miscalibration: the heuristic's mappings with confidence inflated by a fixed amount."""

    def __init__(self, inner: HeuristicProvider, boost: float = 0.25) -> None:
        self.inner = inner
        self.boost = boost
        self.model_id = f"{inner.model_id}+overconfident{boost}"

    def complete(self, request: MappingRequest) -> ProviderResponse:
        payload = json.loads(self.inner.complete(request).text)
        for m in payload["mappings"]:
            if m["target_field"] is not None:
                m["confidence"] = min(1.0, m["confidence"] + self.boost)
        return ProviderResponse(json.dumps(payload), self.model_id)


def chain_schemagate_overconfident(schema: TargetSchema) -> ResolverChain:
    provider = OverconfidentProvider(HeuristicProvider(vocabulary=schema.vocabulary()))
    return ResolverChain.default(ModelResolver(provider))


def chain_overconfident_uncapped(schema: TargetSchema) -> ResolverChain:
    provider = OverconfidentProvider(HeuristicProvider(vocabulary=schema.vocabulary()))
    return ResolverChain([ModelResolver(provider, cap_override=UNCAPPED)])


def chain_anthropic(schema: TargetSchema) -> ResolverChain:
    from schemagate.llm.anthropic_provider import AnthropicProvider

    return ResolverChain.default(ModelResolver(AnthropicProvider()))


def evaluate(name: str, make_chain: Callable[[TargetSchema], ResolverChain],
             suite: list[tuple[TargetSchema, Variant]]) -> dict:
    per_tier: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    per_op: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    predicted = correct = truth_mapped = decoys = decoys_mapped = 0
    det_resolved = gate_n = gate_correct = 0
    held_n = held_correct = 0  # model mappings whose raw confidence reached the gate, held back by the cap
    start = time.perf_counter()
    for schema, variant in suite:
        result = make_chain(schema).run(schema, profile_table(variant.columns, variant.rows), POLICY)
        for p in result.proposals:
            truth = variant.truth[p.source_column]
            ok = p.target_field is not None and p.target_field == truth
            if truth is None:
                decoys += 1
                decoys_mapped += p.target_field is not None
            else:
                truth_mapped += 1
                if p.target_field is not None and p.tier.value in DETERMINISTIC:
                    det_resolved += 1
                for op in variant.operations[p.source_column]:
                    per_op[op][0] += 1
                    per_op[op][1] += ok
            if p.target_field is not None:
                predicted += 1
                correct += ok
                per_tier[p.tier.value][0] += 1
                per_tier[p.tier.value][1] += ok
                if p.confidence >= POLICY.gate_threshold:
                    gate_n += 1
                    gate_correct += ok
                elif (p.evidence or {}).get("raw_confidence", 0.0) >= POLICY.gate_threshold:
                    held_n += 1
                    held_correct += ok
    elapsed = time.perf_counter() - start

    def ratio(a: int, b: int) -> float | None:
        return round(a / b, 4) if b else None

    return {
        "config": name,
        "variants": len(suite),
        "columns": truth_mapped + decoys,
        "schema_columns": truth_mapped,
        "decoy_columns": decoys,
        "deterministic_resolution": ratio(det_resolved, truth_mapped),
        "precision": ratio(correct, predicted),
        "recall": ratio(correct, truth_mapped),
        "decoy_false_positive_rate": ratio(decoys_mapped, decoys),
        "gate_queue_size": gate_n,
        "gate_share_of_predictions": ratio(gate_n, predicted),
        "gate_wrong": gate_n - gate_correct,
        "precision_at_gate": ratio(gate_correct, gate_n),
        "held_by_cap": held_n,
        "held_by_cap_wrong": held_n - held_correct,
        "by_tier": {t: {"n": n, "precision": ratio(c, n)} for t, (n, c) in sorted(per_tier.items())},
        "recall_by_header_operation": {op: {"n": n, "recall": ratio(c, n)}
                                       for op, (n, c) in sorted(per_op.items())},
        "seconds": round(elapsed, 2),
    }


def fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.3f}"


def to_markdown(results: list[dict], args: argparse.Namespace) -> str:
    lines = [
        f"Command: `python -m benchmarks.run --variants {args.variants} --seed {args.seed}`",
        "",
        f"Suite: {results[0]['variants']} sender variants over 4 schemas, {results[0]['columns']} source "
        f"columns ({results[0]['schema_columns']} with a true target, {results[0]['decoy_columns']} decoys). "
        f"Gate threshold: confidence >= {POLICY.gate_threshold}.",
        "",
        "| config | deterministic resolution | precision | recall | decoy FP rate | gate queue | wrong in gate queue | precision at gate |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r['config']} | {fmt(r['deterministic_resolution'])} | {fmt(r['precision'])} | "
                     f"{fmt(r['recall'])} | {fmt(r['decoy_false_positive_rate'])} | {r['gate_queue_size']} | {r['gate_wrong']} | "
                     f"{fmt(r['precision_at_gate'])} |")
    for r in results:
        lines += ["", f"### {r['config']}: by tier", "", "| tier | mapped | precision |", "|---|---|---|"]
        lines += [f"| {t} | {v['n']} | {fmt(v['precision'])} |" for t, v in r["by_tier"].items()]
        lines += ["", f"### {r['config']}: recall by header perturbation", "",
                  "| operation | columns | recall |", "|---|---|---|"]
        lines += [f"| {op} | {v['n']} | {fmt(v['recall'])} |"
                  for op, v in r["recall_by_header_operation"].items()]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--variants", type=int, default=50, help="variants per schema")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, default=Path("benchmarks/results"))
    parser.add_argument("--with-anthropic", action="store_true")
    args = parser.parse_args()
    suite = generate_suite(args.variants, args.seed)
    configs: list[tuple[str, Callable[[TargetSchema], ResolverChain]]] = [
        ("schemagate", chain_schemagate), ("model_uncapped", chain_model_uncapped),
        ("schemagate_overconfident", chain_schemagate_overconfident),
        ("model_overconfident_uncapped", chain_overconfident_uncapped)]
    if args.with_anthropic:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("--with-anthropic requires ANTHROPIC_API_KEY")
        configs.append(("anthropic", chain_anthropic))
    results = [evaluate(name, make, suite) for name, make in configs]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(json.dumps(results, indent=2) + "\n", "utf-8")
    (args.out / "results.md").write_text(to_markdown(results, args), "utf-8")
    print(to_markdown(results, args))


if __name__ == "__main__":
    main()
