from __future__ import annotations

from schemagate.ingest.profile import ColumnProfile
from schemagate.models import MappingProposal, Tier
from schemagate.resolve.base import ResolveContext, norm
from schemagate.schema.normalize import normalize_name


class ExactResolver:
    """Normalized source header equals a canonical field name."""

    name = "exact"

    def resolve(self, ctx: ResolveContext, columns: list[ColumnProfile]) -> list[MappingProposal]:
        by_name = {norm(f.name): f.name for f in ctx.schema.fields}
        out = []
        for col in columns:
            key = norm(col.name)
            if key in by_name:
                out.append(MappingProposal(
                    source_column=col.name,
                    target_field=by_name[key],
                    confidence=ctx.policy.exact,
                    tier=Tier.EXACT,
                    rationale=f"normalized header '{key}' equals canonical field name",
                    evidence={"normalized": key},
                    proposer="resolver:exact",
                ))
        return out


class DictionaryResolver:
    """Normalized source header is a registered alias of a canonical field."""

    name = "dictionary"

    def resolve(self, ctx: ResolveContext, columns: list[ColumnProfile]) -> list[MappingProposal]:
        aliases: dict[str, tuple[str, str]] = {}
        for f in ctx.schema.fields:
            for alias in f.aliases:
                aliases[norm(alias)] = (f.name, alias)
        out = []
        for col in columns:
            key = norm(col.name)
            if key in aliases:
                target, alias = aliases[key]
                out.append(MappingProposal(
                    source_column=col.name,
                    target_field=target,
                    confidence=ctx.policy.dictionary,
                    tier=Tier.DICTIONARY,
                    rationale=f"header matches registered alias '{alias}' of '{target}'",
                    evidence={"normalized": key, "alias": alias},
                    proposer="resolver:dictionary",
                ))
        return out


UNIT_WORDS = frozenset({"percent", "usd", "eur", "gbp", "cad", "aud", "jpy", "chf"})


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class SynonymResolver:
    """Token-level synonym expansion followed by a token-set match against names and aliases."""

    name = "synonym"

    def __init__(self, threshold: float = 0.75) -> None:
        self.threshold = threshold

    def _field_token_sets(self, ctx: ResolveContext) -> dict[str, list[tuple[frozenset[str], str]]]:
        sets: dict[str, list[tuple[frozenset[str], str]]] = {}
        for f in ctx.schema.fields:
            for label in [f.name, *f.aliases]:
                toks = frozenset(ctx.vocab.expand(normalize_name(label).tokens))
                if f.unit and f.unit.lower() in UNIT_WORDS:
                    toks = toks | {f.unit.lower()}
                if toks:
                    sets.setdefault(f.name, []).append((toks, label))
        return sets

    def score(self, ctx: ResolveContext, source: frozenset[str],
              candidates: list[tuple[frozenset[str], str]]) -> tuple[float, str]:
        best = (0.0, "")
        for toks, label in candidates:
            s = _jaccard(source, toks)
            stripped_src, stripped_tgt = source - UNIT_WORDS, toks - UNIT_WORDS
            if stripped_src and stripped_tgt:
                s = max(s, 0.95 * _jaccard(stripped_src, stripped_tgt))
            if s > best[0]:
                best = (s, label)
        return best

    def resolve(self, ctx: ResolveContext, columns: list[ColumnProfile]) -> list[MappingProposal]:
        field_sets = self._field_token_sets(ctx)
        out = []
        for col in columns:
            nn = normalize_name(col.name)
            expanded = ctx.vocab.expand(nn.tokens)
            source = frozenset(expanded) | {h for h in nn.unit_hints if h in UNIT_WORDS}
            if not source:
                continue
            scored = sorted(
                ((*self.score(ctx, source, cands), fname) for fname, cands in field_sets.items()),
                reverse=True,
            )
            if not scored or scored[0][0] < self.threshold:
                continue
            best_score, label, target = scored[0]
            runner_up = scored[1][0] if len(scored) > 1 else 0.0
            if runner_up >= best_score:
                continue
            out.append(MappingProposal(
                source_column=col.name,
                target_field=target,
                confidence=ctx.policy.synonym,
                tier=Tier.SYNONYM,
                rationale=(
                    f"expanded tokens {sorted(source)} match '{label}' "
                    f"(token-set score {best_score:.2f})"
                ),
                evidence={
                    "expanded_tokens": list(expanded),
                    "matched_label": label,
                    "score": round(best_score, 4),
                    "runner_up_score": round(runner_up, 4),
                    "unit_hints": list(nn.unit_hints),
                },
                proposer="resolver:synonym",
            ))
        return out
