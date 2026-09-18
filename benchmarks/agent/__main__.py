from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.agent.store import ReplayProvider, build_suites, load_answers, load_tasks, prepare
from benchmarks.run import chain_schemagate, evaluate, fmt
from schemagate.resolve import ModelResolver, ResolverChain

COLUMNS = ("precision", "recall", "decoy_false_positive_rate", "gate_queue_size", "gate_wrong",
           "held_by_cap", "held_by_cap_wrong")


def cmd_prepare(args: argparse.Namespace) -> None:
    config = {"variants": args.variants, "seed": args.seed,
              "valentine": str(args.valentine.resolve()) if args.valentine else None,
              "per_group": args.per_group}
    doc = prepare(config)
    by_suite: dict[str, int] = {}
    for t in doc["tasks"]:
        by_suite[t["suite"]] = by_suite.get(t["suite"], 0) + 1
    print(f"prepared {len(doc['tasks'])} tasks: {by_suite}")


def cmd_status(args: argparse.Namespace) -> None:
    tasks = load_tasks()["tasks"]
    from benchmarks.agent.store import DATA_DIR

    folder = DATA_DIR / "answers"
    runs = sorted(p.name for p in folder.glob("*")) if folder.exists() else []
    print(f"{len(tasks)} tasks")
    for run in runs:
        print(f"  {run}: {len(load_answers(run))} answered")


def _row(label: str, r: dict) -> str:
    cells = [fmt(r[c]) if isinstance(r[c], float) or r[c] is None else str(r[c]) for c in COLUMNS]
    return f"| {label} | " + " | ".join(cells) + " |"


def cmd_score(args: argparse.Namespace) -> None:
    doc = load_tasks()
    suites = build_suites(doc["config"])
    results: dict[str, list[dict]] = {}
    lines = ["# Agent-answered model tier", "",
             f"Tasks prepared with `{json.dumps(doc['config'])}`. Each run's answers come from a coding "
             "agent over MCP (`benchmarks/agent/server.py`), replayed through the normal resolver chain "
             "with the 0.75 cap. \"held by cap\" counts model answers whose own confidence reached the "
             "0.85 gate but were capped into per-column review; \"wrong\" is how many of those were "
             "incorrect.", ""]
    for suite_name, suite in suites.items():
        rows = [evaluate("heuristic", chain_schemagate, suite)]
        coverage = {}
        for run in args.run:
            replay = ReplayProvider(run)
            result = evaluate(f"agent:{run}", lambda _s, p=replay: ResolverChain.default(ModelResolver(p)), suite)
            result["answers_used"], result["answers_missing"] = replay.served, replay.missing
            coverage[run] = (replay.served, replay.missing)
            rows.append(result)
        results[suite_name] = rows
        lines += [f"## {suite_name} ({len(suite)} variants)", "",
                  "| config | " + " | ".join(c.replace("_", " ") for c in COLUMNS) + " |",
                  "|---|" + "---|" * len(COLUMNS)]
        lines += [_row(r["config"], r) for r in rows]
        for run, (_served, missing) in coverage.items():
            if missing:
                lines.append(f"\n{run}: {missing} variant(s) had no answer and count as unresolved.")
        lines += ["", "Model-tier precision: " + ", ".join(
            f"{r['config']} {fmt(r['by_tier'].get('model', {}).get('precision'))} "
            f"(n={r['by_tier'].get('model', {}).get('n', 0)})" for r in rows), ""]
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"{args.name}.md").write_text(text, "utf-8")
        (args.out / f"{args.name}.json").write_text(json.dumps(results, indent=2) + "\n", "utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m benchmarks.agent")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="record the model-tier questions for a benchmark suite")
    p.add_argument("--variants", type=int, default=5, help="synthetic variants per schema")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--valentine", type=Path, help="Valentine-datasets directory (optional)")
    p.add_argument("--per-group", type=int, default=2, help="Valentine pairs per dataset group")
    p.set_defaults(func=cmd_prepare)
    p = sub.add_parser("status", help="answers per run")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("score", help="replay stored answers and report metrics")
    p.add_argument("--run", action="append", required=True)
    p.add_argument("--out", type=Path)
    p.add_argument("--name", default="agent")
    p.set_defaults(func=cmd_score)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
