# Benchmark

## Synthetic suite

`benchmarks/generate.py` builds sender variants for four target schemas in `benchmarks/schemas/`
(supplier invoices, clinical trial site monthly reports, retail POS transaction lines, freight
shipments). For each variant it:

- keeps all required fields and a random half-or-more of optional fields, in shuffled order;
- picks a base header per field: canonical name (25%), a registered alias (about 25%), or a held-out
  paraphrase that is in no alias list (the rest);
- applies perturbations with fixed probabilities: abbreviation (35%), foreign-language token (12%),
  word reversal (15%), a one-character typo (12%), casing style (always), unit suffix for fields with a
  unit (40%);
- adds 1 to 4 decoy columns that belong to no field (notes, internal refs, timestamps, ...);
- renders 30 rows with per-variant value formats (currency symbols or not, `%` or not, one of five date
  layouts, Y/N or true/false booleans, upper-cased enums).

Ground truth is the field each header was generated from (None for decoys). Generation is
deterministic for a given `--seed`.

Known bias: abbreviations in the generator overlap with the package synonym table, and 25% of headers
are registered aliases, so the deterministic tiers are favoured by construction on those headers.
Paraphrases and foreign tokens are not in any dictionary.

## Metrics

- deterministic resolution: share of true-target columns mapped by exact, dictionary or synonym tiers;
- precision and recall of final mappings after compatibility checks and collision handling;
- decoy false-positive rate: share of decoy columns mapped to any field;
- gate queue: mappings with confidence at or above `gate_threshold` (0.85), i.e. what a reviewer would
  batch-approve as high confidence; precision at gate and the absolute number of wrong mappings in it.

## Configurations

- `schemagate`: exact -> dictionary -> synonym -> heuristic model with the 0.75 cap.
- `model_uncapped`: every column goes straight to the heuristic model; its raw score is the confidence.
- `schemagate_overconfident` and `model_overconfident_uncapped`: the same, with the heuristic's
  confidence inflated by +0.25 to simulate a miscalibrated model.
- `anthropic` (optional, not run): `python -m benchmarks.run --with-anthropic` with `ANTHROPIC_API_KEY`
  set and the `anthropic` extra installed. It makes one API call per variant that has unresolved
  columns (up to 200 at the default size).

Results for the committed run are in `benchmarks/results/results.md` and summarized in the README.

## Valentine datasets

`benchmarks/valentine.py` loads all dataset pairs from the Valentine schema-matching benchmark
(Koutras et al., "Valentine: Evaluating Matching Techniques for Dataset Discovery", ICDE 2021).
Download `Valentine-datasets.zip` from Zenodo (record 5084605, about 600 MB) and unzip it. Each pair
becomes a schema built from the target table's columns (types inferred, no aliases) and one variant
from the source table (first 500 rows); source columns without a ground-truth match are decoys.

```bash
python -m benchmarks.valentine /path/to/Valentine-datasets --out benchmarks/results
```

The committed run covers all 551 pairs. Results are in `benchmarks/results/valentine.md` (overall and
per group) and `valentine.json`, and summarized in the README.

## Agent-answered model tier (MCP)

`benchmarks/agent/` lets a coding agent stand in for the model provider, so model tiers can be compared
without API keys. `prepare` records the exact request the model tier would receive for each variant
(only the columns the deterministic tiers left over). An MCP server serves those requests and stores the
agent's replies per run name, with structural checks only. `score` replays the chain with the stored
replies and reports the usual metrics, plus how many answers the cap held back from batch approval and
how many of those were wrong.

```bash
pip install -e '.[dev,bench-agent]'
python -m benchmarks.agent prepare --variants 5 --seed 7 --valentine /path/to/Valentine-datasets --per-group 2
claude mcp add -s user schemagate-bench -- "$PWD/.venv/bin/python" "$PWD/benchmarks/agent/server.py"
# agents call next_task(run, shard, shards) and submit_answer(run, task_id, answer, model)
python -m benchmarks.agent status
python -m benchmarks.agent score --run haiku --run sonnet --out benchmarks/results --name agent
```

Ground truth stays in the scoring process; the server only serves prompts. Agents are told to answer from
the prompt alone, but nothing stops an agent with file access from reading the generator. Run them without
repository tools, or check their transcripts.
