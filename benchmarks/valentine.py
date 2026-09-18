"""Loader and runner for the Valentine schema-matching datasets (Koutras et al., ICDE 2021).

No Valentine data is bundled. To reproduce ``benchmarks/results/valentine.md``:

1. Download ``Valentine-datasets.zip`` from Zenodo (record 5084605, about 600 MB) and unzip it.
   Each dataset pair is a directory holding

       <name>_source.csv
       <name>_target.csv
       <name>_mapping.json   # {"matches": [{"source_column": ..., "target_column": ...}, ...]}

   macOS metadata (``__MACOSX/``, ``._*`` files) in the archive is ignored.
2. Run:  python -m benchmarks.valentine /path/to/Valentine-datasets --out benchmarks/results

Each pair becomes one target schema (fields = target CSV columns, types inferred from values, no aliases,
so the dictionary tier contributes nothing) and one sender variant (the source CSV, first 500 rows) with
ground truth from the mapping file. Source columns without a match are treated as decoys. Results are
reported overall and per dataset group (``<collection>/<scenario>``, e.g. ``TPC-DI/Joinable``).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

from benchmarks.generate import Variant
from schemagate.ingest.profile import profile_table
from schemagate.schema.normalize import snake_case
from schemagate.schema.target import TargetSchema, parse_schema

TYPE_MAP = {"int": "integer", "float": "number", "currency": "number", "percent": "number",
            "date": "date", "bool": "boolean"}
MAX_ROWS = 500


def _read(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1 : MAX_ROWS + 1]


def _field_name(raw: str, used: set[str]) -> str:
    name = snake_case(raw)
    if not re.match(r"^[a-z]", name):
        name = f"f_{name}"
    base, n = name, 2
    while name in used:
        name = f"{base}_{n}"
        n += 1
    used.add(name)
    return name


def _find(pair_dir: Path, suffix: str) -> Path | None:
    matches = sorted(p for p in pair_dir.glob(f"*{suffix}") if not p.name.startswith("._"))
    return matches[0] if matches else None


def load_pair(pair_dir: Path, index: int) -> tuple[TargetSchema, Variant] | None:
    src, tgt, mapping = (_find(pair_dir, s) for s in ("_source.csv", "_target.csv", "_mapping.json"))
    if not (src and tgt and mapping):
        return None
    t_cols, t_rows = _read(tgt)
    used: set[str] = set()
    names = {c: _field_name(c, used) for c in t_cols}
    profiles = {p.name: p for p in profile_table(t_cols, t_rows)}
    schema = parse_schema({
        "name": f"valentine_{index}",
        "table": f"valentine_{index}",
        "fields": [{"name": names[c], "type": TYPE_MAP.get(profiles[c].inferred_type, "string"),
                    "description": c} for c in t_cols],
    })
    s_cols, s_rows = _read(src)
    matches = json.loads(mapping.read_text("utf-8")).get("matches", [])
    truth: dict[str, str | None] = {c: None for c in s_cols}
    for m in matches:
        if m.get("source_column") in truth and m.get("target_column") in names:
            truth[m["source_column"]] = names[m["target_column"]]
    ops = {c: ["valentine"] if truth[c] else ["decoy"] for c in s_cols}
    return schema, Variant(schema.name, index, s_cols, s_rows, truth, ops)


def _pair_dirs(root: Path) -> list[Path]:
    return sorted({
        p.parent for p in root.rglob("*_mapping.json")
        if not p.name.startswith("._") and "__MACOSX" not in p.parts
    })


def load_valentine(root: Path, limit: int | None = None) -> list[tuple[TargetSchema, Variant]]:
    return [pair for suite in load_valentine_groups(root, limit).values() for pair in suite]


def load_valentine_groups(root: Path, limit: int | None = None,
                          per_group: int | None = None) -> dict[str, list[tuple[TargetSchema, Variant]]]:
    """Dataset pairs keyed by group: the first two path components below ``root``.

    ``limit`` caps the total number of pairs, ``per_group`` the number per group (pairs beyond it
    are not read at all).
    """
    groups: dict[str, list[tuple[TargetSchema, Variant]]] = {}
    n = 0
    for i, d in enumerate(_pair_dirs(root)):
        if limit is not None and n >= limit:
            break
        rel = d.relative_to(root).parts
        group = "/".join(rel[:2]) if len(rel) > 2 else (rel[0] if rel else ".")
        if per_group is not None and len(groups.get(group, [])) >= per_group:
            continue
        loaded = load_pair(d, i)
        if loaded:
            groups.setdefault(group, []).append(loaded)
            n += 1
    return groups


ROW_KEYS = ("variants", "schema_columns", "decoy_columns", "precision", "recall",
            "decoy_false_positive_rate", "gate_queue_size", "gate_wrong")


def to_markdown(overall: list[dict], by_group: dict[str, list[dict]]) -> str:
    from benchmarks.run import fmt

    def row(label: str, r: dict) -> str:
        return (f"| {label} | {r['config']} | {r['variants']} | {r['schema_columns']} | {r['decoy_columns']} | "
                f"{fmt(r['precision'])} | {fmt(r['recall'])} | {fmt(r['decoy_false_positive_rate'])} | "
                f"{r['gate_queue_size']} | {r['gate_wrong']} |")

    lines = [
        "# Valentine results",
        "",
        "Generated by `python -m benchmarks.valentine <Valentine-datasets> --out benchmarks/results`.",
        "The model tier is the offline heuristic provider, not an LLM.",
        "",
        "| group | config | pairs | matchable columns | decoys | precision | recall | decoy FP rate "
        "| gate queue (conf >= 0.85) | wrong in gate queue |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [row("**all**", r) for r in overall]
    for group, results in sorted(by_group.items()):
        lines += [row(group, r) for r in results]
    lines += ["", "## By tier (schemagate, all pairs)", "", "| tier | mappings | precision |", "|---|---|---|"]
    for tier, v in overall[0]["by_tier"].items():
        lines.append(f"| {tier} | {v['n']} | {fmt(v['precision'])} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    from benchmarks.run import chain_model_uncapped, chain_schemagate, evaluate

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", type=Path, help="write valentine.json and valentine.md here")
    args = parser.parse_args()
    groups = load_valentine_groups(args.root, args.limit)
    if not groups:
        raise SystemExit(f"no dataset pairs found under {args.root}")
    configs = (("schemagate", chain_schemagate), ("model_uncapped", chain_model_uncapped))
    suite = [pair for g in groups.values() for pair in g]
    overall = [evaluate(name, make, suite) for name, make in configs]
    by_group = {g: [evaluate(name, make, s) for name, make in configs] for g, s in groups.items()}
    print(to_markdown(overall, by_group))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "valentine.json").write_text(
            json.dumps({"overall": overall, "by_group": by_group}, indent=2) + "\n", "utf-8")
        (args.out / "valentine.md").write_text(to_markdown(overall, by_group), "utf-8")


if __name__ == "__main__":
    main()
