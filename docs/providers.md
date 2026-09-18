# Model providers

The model resolver only receives columns that exact, dictionary and synonym resolution left
unresolved, plus the list of targets already claimed. Every provider returns text that must parse as

```json
{"mappings": [{"source_column": "...", "target_field": "... or null", "confidence": 0.0,
               "rationale": "...", "evidence": ["..."]}]}
```

`schemagate.llm.validation.validate_response` then:

- raises `ProviderResponseError` for invalid JSON or wrong types (the resolver turns this into
  unresolved proposals that record the error);
- ignores entries for columns that were not asked about, and duplicates;
- clears targets that are not in the schema or were already claimed, recording the issue;
- limits confidence to 0.5 when no evidence is cited;
- clamps confidence to `ConfidencePolicy.model_cap` (0.75 by default).

## Offline heuristic (default)

`HeuristicProvider` needs no network: character trigram and token similarity between the header and the
field name/aliases (after synonym expansion), overlap with the field description, value-type fit from
parse rates, allowed-value and pattern fit, then greedy one-to-one assignment. It is a baseline, not a
language model.

## Anthropic

```bash
pip install 'schemadriftgate[anthropic]'
export ANTHROPIC_API_KEY=...
schemagate propose --feed cedar --provider anthropic
```

Default model id `claude-sonnet-5`; override with `model_id:` in `schemagate.yaml` or
`SCHEMAGATE_ANTHROPIC_MODEL`. The request is a single Messages API call with the system prompt and the
rendered user prompt; responses with `stop_reason` `refusal` or `max_tokens` are rejected as provider
errors. This path is covered by tests with a stub client only; no live calls were made while building
v0.1.

## OpenAI

```bash
pip install 'schemadriftgate[openai]'
export OPENAI_API_KEY=...
schemagate propose --feed cedar --provider openai
```

Uses chat completions with JSON response format. Same validation. Not exercised against the live API.

## Prompt versions

Templates live in `src/schemagate/llm/prompts/<name>_v<revision>.txt`. The recorded `prompt_version`
is `<name>/v<revision>+<first 8 hex of sha256(template)>`, so an edited template is visible in the audit
log even if someone forgets to bump the revision. Bump the revision for intentional changes.

## Writing a provider

Implement `model_id: str` and `complete(request: MappingRequest) -> ProviderResponse`. Use
`request.system_prompt` and `request.user_prompt`, or the structured `request.columns` and
`request.fields`. Return raw text; do not pre-validate or clamp, the resolver does that uniformly.
