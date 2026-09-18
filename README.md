# schemagate

Governed schema mapping for spreadsheets and CSVs that arrive in someone else's layout.
Deterministic resolution first, the model last, and nothing reaches a published table without a
recorded human decision.

## The problem

Companies receive recurring files from many senders: suppliers, clinical sites, stores, carriers.
Each sender uses its own column names, puts title rows above the header, splits data across sheets,
and changes the layout without telling anyone. Analysts re-map the same columns by hand every month.

Automation tends to fail in one of two ways. Tools that hand every column to a model produce mappings
that look confident and are occasionally wrong, and a wrong mapping in a shared table is worse than a
missing one because nobody notices it. Tools that stop at a mapping UI never get the data into the
warehouse, so the manual work just moves.

schemagate is a small pipeline and governance store that sits between those files and a warehouse
table. It resolves what can be resolved from facts, asks a model only about the rest, records who
decided what and why, and refuses to write a table from a mapping that was not approved.

## Where it fits

schemagate is a governance layer, not a better matcher. Research matchers are more accurate at
proposing column correspondences. What schemagate adds is everything around the proposal: who is
allowed to trust it, the record of that decision, what happens when the sender's layout changes, and
the refusal to load data without approval.

| Tool | What it does | Relation to schemagate |
|---|---|---|
| [Valentine](https://github.com/delftdata/valentine), [Magneto](https://github.com/VIDA-NYU/magneto-matcher) | Schema-matching algorithms and benchmarks (Magneto combines small and large language models) | Stronger matchers. They produce ranked correspondences but have no approval, audit, drift, or load step. A matcher like these could sit behind the model tier as a provider. |
| [bdi-kit](https://github.com/VIDA-NYU/bdi-kit) | Matching plus value mapping and materialization for biomedical data integration | Closest on the pipeline side. It has no approval gate or tamper-evident decision record. |
| Flatfile, OneSchema (commercial); Impler, YoBulk (open source) | Embedded importers where an end user maps columns while uploading | Built for one-off uploads by the person who owns the file. schemagate is for recurring feeds from third parties into a shared table, where the reviewer is not the uploader. |
| Great Expectations, data-contract tools | Validate data against expectations and detect drift | Validate data once columns are known. They do not decide which source column is which target field. |

## Principles

1. **Deterministic resolution first.** Exact names, a maintained alias dictionary and token-level
   synonyms run before any model. The model only sees columns those could not resolve, and it is told
   which targets are already taken.
2. **A model proposal cannot outrank a verified fact.** Confidence tiers are exact 0.95, dictionary
   0.90, synonym 0.85; model proposals are clamped to 0.75. The tiers are configurable, but
   `ConfidencePolicy` rejects any configuration where the cap is not strictly below every deterministic
   tier. Collisions go to the highest confidence, then the stronger tier.
3. **Nothing is published without a recorded approval.** The materializer re-derives approval from the
   approval rows and the audit log before generating any SQL, and raises `UnapprovedSpecError`
   otherwise. Model-originated proposals need an explicit per-column accept.
4. **Drift detection is separate from remediation.** Detecting that a sender changed its layout blocks
   the load and records events. Fixing it is a separate command that proposes a new spec version, which
   is approved and then switched to explicitly.
5. **Every decision is auditable.** Each proposal records the proposer (resolver or model id), rationale,
   cited evidence, prompt version and raw model confidence; approvals, overrides, demotions, switches
   and loads land in an append-only, hash-chained audit log.

## Quickstart

```bash
pip install schemadriftgate          # import name and CLI: schemagate
pip install 'schemadriftgate[excel,duckdb]'   # extras: excel, duckdb, postgres, anthropic, openai
```

From source, with the example walkthrough:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'          # extras: excel, duckdb, postgres, anthropic, openai
bash examples/walkthrough.sh      # full run on the synthetic example data, into ./walkthrough-run
```

The same flow by hand for one sender (run from the repository root; ids are the ones this sequence
produces on a fresh project):

```bash
schemagate -C proj init --schema examples/schemas/supplier_invoice.yaml
schemagate -C proj --actor dana ingest --feed cedar --schema supplier_invoice \
    examples/data/cedar/cedar_export_2026-07.csv
schemagate -C proj --actor dana propose --feed cedar        # offline heuristic provider by default
schemagate -C proj review --spec 1                          # rationale per column; * = needs a decision
schemagate -C proj --actor omar accept 4 6 8 10 11 12 13    # the model-tier proposals
schemagate -C proj --actor omar approve 1                   # dana (the proposer) would be refused
schemagate -C proj --actor omar materialize --feed cedar    # upsert into the SQLite warehouse

schemagate -C proj --actor dana ingest --feed cedar examples/data/cedar/cedar_export_2026-08.csv
schemagate -C proj drift --feed cedar                       # rename + date format change: breaking
schemagate -C proj --actor dana drift --feed cedar --remediate   # proposes expand spec 2
schemagate -C proj --actor omar accept 23                   # the detected rename
schemagate -C proj --actor omar approve 2
schemagate -C proj --actor lee switch 2
schemagate -C proj --actor omar materialize --feed cedar
schemagate -C proj materialize --feed cedar --object 2 --dialect snowflake   # SQL text only
schemagate -C proj audit verify
```

[`examples/walkthrough.md`](examples/walkthrough.md) is the unedited output of a full run: three
senders with different layouts feeding one `invoice_lines` table, the two-approver rule kicking in once
the table is shared, and one sender whose month-2 export renames a column, changes its date format and
adds a column.

Using Claude instead of the offline heuristic:

```bash
pip install -e '.[anthropic]'
export ANTHROPIC_API_KEY=...
schemagate -C proj propose --feed cedar --provider anthropic   # default model id: claude-sonnet-5
```

## Architecture

```
 files ─► ingest ──────────► profile ──► resolve ───────────────────────────► store ───────► materialize
          CSV: encoding,     type,       exact ─► dictionary ─► synonym ─►   pending spec     plan (verified
          delimiter sniff    null rate,     │  value-compat veto/downgrade    proposals        approval, drift
          XLSX: all sheets   distinct,      ▼                                 approvals        and key checks)
          header-row score   samples,    model (only unresolved columns;      audit chain      ─► SQLite / DuckDB
          SHA-256 skip       patterns,   strict JSON validation, cap)         drift events        (executed)
          lineage columns    parse rates    ▼                                                  ─► Postgres /
                                         collisions ─► ext_ columns                               Snowflake /
                                                                                                  Databricks (text)
 new file ─► drift detect (events only) ──► drift --remediate (expand spec) ──► approve ──► switch
```

| module | responsibility |
|---|---|
| `schemagate.ingest` | CSV/XLSX readers, header-row detection (scores string-ness, label uniqueness, row fill and type consistency of the rows below), dedup of header names, content hash, lineage, column profiles |
| `schemagate.schema` | YAML target schemas (type, description, required, unit, allowed values, pattern, aliases), name normalization (case, punctuation, camelCase, unit suffixes such as `(USD)` or `%`), synonym vocabulary |
| `schemagate.resolve` | resolver chain, value-compatibility checks, collision handling |
| `schemagate.llm` | provider protocol; heuristic, Anthropic, OpenAI and fake providers; versioned prompt template; response validation |
| `schemagate.store` | SQLite (default) or Postgres governance store; `GovernanceService` is the only write path |
| `schemagate.drift` | drift detection and Expand remediation |
| `schemagate.materialize` | approval-gated plans, dialect SQL generation, SQLite/DuckDB execution |
| `schemagate.cli` | `init`, `ingest`, `propose`, `review`, `accept`, `demote`, `override`, `approve`, `reject`, `switch`, `materialize`, `drift`, `audit`, `status` |

Every ingested value is kept as text in a raw staging table (`raw_<feed>_<object>`) with
`_source_file`, `_sheet`, `_row_number`, `_content_hash` and `_ingested_at`. Typing happens in the
generated SQL, using the value formats observed in the approved profile (currency symbols, thousands
separators, percent signs, accounting negatives, date layouts). Unmapped columns and collision losers
are kept as `ext_<name>` text columns rather than dropped.

Value checks run against every proposal, deterministic or not: a header that matches `invoice_date`
exactly but whose values do not parse as dates is vetoed and passed on; a partial fit (50-90% of values)
lowers confidence by 0.15.

More detail: [docs/governance.md](docs/governance.md), [docs/drift.md](docs/drift.md),
[docs/materialize.md](docs/materialize.md), [docs/providers.md](docs/providers.md),
[docs/benchmark.md](docs/benchmark.md).

## Governance model

- A **feed** is one sender stream bound to one target schema. A **spec** is a versioned mapping for a
  feed; it starts `pending`, becomes `approved`, and is made active either automatically (the feed's
  first approved spec) or with `switch`.
- **Who can approve.** The proposer cannot approve their own spec or review its proposals. Each person
  counts once. A table private to one feed needs one approver; a table marked `published: true`, or one
  that already has an approved spec from another feed, needs two distinct approvers. The requirement is
  re-evaluated at each approval.
- **Per-column decisions.** Model and rename proposals must be accepted, overridden (recorded as tier
  `human` with a reason) or demoted to an extension column before the spec can be approved.
- **Enforcement.** `build_plan` calls `verify_approved`, which counts approval rows from distinct
  non-proposers against the required number and requires a `spec.approved` audit entry. A spec whose
  status was edited to `approved` directly in the database is still refused. Open breaking drift or
  missing mapped columns raise `DriftBlockedError`.
- **Audit.** Every transition is written in the same transaction as its audit entry. Entries are
  hash-chained and the table rejects UPDATE and DELETE via triggers. `schemagate audit verify` recomputes
  the chain and reports the first bad entry; `--expect-head` compares against an externally recorded
  head hash to detect truncation.

## Benchmark

Synthetic, reproducible, and described in full in [docs/benchmark.md](docs/benchmark.md). 200 sender
variants over four target schemas (supplier invoices, clinical trial site reports, retail POS lines,
freight shipments): 2971 source columns, 2444 with a true target and 527 decoys. Headers are
perturbed with registered aliases, held-out paraphrases, abbreviations, casing, unit suffixes, word
reversal, typos and foreign-language tokens; value formats vary per variant.

```bash
python -m benchmarks.run --variants 50 --seed 7 --out benchmarks/results   # 55 s to 1 min 45 s in our runs (single process)
```

| config | deterministic resolution | precision | recall | decoy FP rate | gate queue (conf >= 0.85) | wrong in gate queue |
|---|---|---|---|---|---|---|
| schemagate (chain + heuristic model, cap 0.75) | 0.425 | 0.938 | 0.883 | 0.076 | 1039 | 0 |
| heuristic model only, uncapped | 0.000 | 0.938 | 0.883 | 0.076 | 954 | 0 |
| schemagate, simulated overconfident model (+0.25) | 0.425 | 0.938 | 0.883 | 0.076 | 1039 | 0 |
| overconfident model only, uncapped | 0.000 | 0.938 | 0.883 | 0.076 | 1507 | 8 |

By tier in the schemagate run: exact 336 mappings, dictionary 412, synonym 291, all correct; model 1261
mappings at 0.887 precision (about 143 wrong, all of which sit below the gate and require a per-column
decision). Recall by perturbation ranges from 0.99 for registered aliases to 0.78 for paraphrases and
0.68 for foreign-language tokens. Full tables: [benchmarks/results/results.md](benchmarks/results/results.md).

How to read this honestly:

- **Final accuracy is identical across configurations.** The heuristic provider scores headers against
  the same names, aliases and synonyms the deterministic tiers use, so on this suite it reaches the same
  final mappings. The chain does not make matching smarter; it changes where decisions come from and
  what they are allowed to claim.
- **What the chain does change:** 42.5% of mappable columns never reach a model (with an LLM provider,
  that is the share of columns you do not pay for or have to trust), and every high-confidence mapping is
  backed by a name, alias or synonym match with a stated rationale.
- **What the cap does:** with a well-calibrated model the uncapped gate queue is also clean. With a
  simulated miscalibrated model, 8 wrong mappings enter the batch-approval queue in the uncapped setup
  and none do with the cap. The price is review effort: the capped setup sends all 1261 model mappings to
  individual review, versus 793 in the overconfident uncapped one.
- The overconfidence is simulated with a fixed +0.25 offset, not measured on a real LLM. The generator's
  abbreviations overlap with the built-in synonym table and a quarter of headers are registered aliases,
  which favours the deterministic tiers on those headers.
- These numbers use the offline heuristic as the model tier. Results with Claude Haiku 4.5 as the model
  tier are below.

### Valentine (real, third-party data)

The same resolver chain on all 551 dataset pairs of the Valentine schema-matching benchmark (Koutras
et al., ICDE 2021: TPC-DI, OpenData, ChEMBL, Magellan, Wikidata). These tables were not written by
this project. The target schemas are built from the target tables' column names with no aliases, so
the dictionary tier contributes nothing. Reproduce with `python -m benchmarks.valentine
<Valentine-datasets> --out benchmarks/results` (43 s per configuration).

| config | matchable columns | decoys | precision | recall | decoy FP rate | gate queue (conf >= 0.85) | wrong in gate queue |
|---|---|---|---|---|---|---|---|
| schemagate (chain + heuristic model, cap 0.75) | 8683 | 4270 | 0.777 | 0.566 | 0.266 | 1959 | 0 |
| heuristic model only, uncapped | 8683 | 4270 | 0.777 | 0.566 | 0.266 | 1636 | 0 |

- **Deterministic tiers held up on real data.** Exact and synonym matches made 1971 mappings, all
  correct, and they covered 22.7% of matchable columns. The heuristic model's 4357 mappings were
  67.6% correct. Every one of those sits below the gate and needs a per-column decision, which is what
  the cap is for.
- **The matcher is weak, as expected.** Recall is 0.57, and on the ChEMBL pairs precision is about
  0.55, because the offline heuristic only compares names. This is the case for plugging a stronger
  matcher or an LLM into the model tier; the governance around it does not change.
- As on the synthetic suite, final accuracy is the same with and without the chain, and the heuristic
  was not overconfident here, so the cap kept no wrong mappings out of the queue on this data. Per-group
  results: [benchmarks/results/valentine.md](benchmarks/results/valentine.md).

### Claude Haiku 4.5 as the model tier

The same chain and 0.75 cap with `claude-haiku-4-5` answering the model tier, compared with the
heuristic on the same variants: 20 synthetic variants and 28 Valentine pairs (2 per dataset group).

| suite | model tier | precision | recall | decoy FP rate | model-tier precision | wrong in gate queue | confident model mappings held by cap | wrong among held |
|---|---|---|---|---|---|---|---|---|
| Valentine | heuristic | 0.617 | 0.850 | 0.412 | 0.529 (n=261) | 0 | 13 | 0 |
| Valentine | claude-haiku-4-5 | 0.916 | 0.888 | 0.066 | 0.885 (n=166) | 0 | 147 | 6 |
| synthetic | heuristic | 0.935 | 0.886 | 0.082 | 0.876 (n=121) | 0 | 0 | 0 |
| synthetic | claude-haiku-4-5 | 0.992 | 1.000 | 0.041 | 0.985 (n=136) | 0 | 134 | 0 |

- **Haiku is a much stronger model tier on real data.** On Valentine its mappings were 88.5% correct,
  against 52.9% for the heuristic, and the share of decoy columns wrongly mapped fell from 41% to 7%.
- **The cap caught real overconfidence.** On Valentine, Haiku gave 6 wrong mappings a confidence of
  0.85 or more, enough for batch approval. The cap sent them to per-column review, and the batch queue
  stayed free of errors.
- **The price is review effort.** 147 confident Haiku mappings on Valentine went to individual review;
  141 of them were right.
- The sample is small (28 of the 551 Valentine pairs) and comes from a single run. Full tables:
  [benchmarks/results/haiku.md](benchmarks/results/haiku.md).

## Tests

```bash
pytest        # 144 tests, no network or API keys
ruff check .
```

Covered: normalization, header detection on messy sheets, CSV sniffing and XLSX multi-sheet ingest,
each resolver, the confidence-cap invariant (Hypothesis property tests over random model responses and
collisions), collision demotion, approval policy (self-approval, duplicate approver, two-approver rule,
published tables, per-column review of model proposals), audit chain verification and tamper
detection, drift cases including rename and PSI, Expand remediation and Switch gating, the materializer
refusing unapproved specs and writing correctly typed rows in SQLite and DuckDB, SQL generation for
all dialects, response validation with a fake provider (invalid JSON, hallucinated target, over-cap
confidence), and the CLI end to end on the example data.

## Limitations

- **Identity is asserted, not authenticated.** Approver distinctness is by actor string (compared
  case-insensitively with whitespace collapsed, so `Dana ` and `dana` are one person). Put the CLI or
  service behind something that sets the actor from a real identity before relying on it.
- **Postgres, Snowflake and Databricks SQL is generated, not executed** in this repository. The Postgres
  governance store backend is implemented but not exercised by the test suite. Only SQLite and DuckDB
  are run.
- **Header detection handles one header row.** Multi-row headers (merged group labels above column
  labels) and pivoted/cross-tab layouts are not reconstructed.
- **Value parsing is locale-light.** Decimal commas (`1.234,50`, `2,5`) are not supported and load as
  NULL rather than as a wrong number; ambiguous
  day/month dates default to month-first when every value fits both.
- **The heuristic provider is a baseline**, not a language model. It does poorly on paraphrases and
  other languages, as the benchmark shows.
- **Backfill and Contract are manual.** Expand and Switch are implemented and gated; see
  [docs/drift.md](docs/drift.md).
- **Single-process SQLite.** The store uses `BEGIN IMMEDIATE` transactions and is fine for a team's
  CLI usage, not for a multi-writer service.
- **Benchmark data is synthetic** and generated by the same author as the resolvers.

## Roadmap

- Run the model-tier benchmark on the full Valentine suite and more models, and publish calibration
  per perturbation type.
- Execute generated SQL against Postgres in CI (service container) and add a DuckDB-backed warehouse
  option to the CLI.
- Backfill planner: find source objects whose layout matches a new spec version and re-land them.
- Multi-row header reconstruction and decimal-comma locales.
- Reviewer UX: a small web view over `review` with sample values side by side, and a queue for
  accepting proposals in bulk that never includes model-tier items.
- Pluggable identity for actors (OIDC token subject) and signed audit heads.
- Plug a dedicated matcher such as Magneto into the model tier and compare it with Claude Haiku 4.5.

## License

Apache-2.0. See [LICENSE](LICENSE).
