from __future__ import annotations

from dataclasses import dataclass, field, replace

from schemagate.config import ConfidencePolicy
from schemagate.ingest.profile import ColumnProfile
from schemagate.models import MappingProposal, Tier
from schemagate.resolve.base import ResolveContext, Resolver
from schemagate.resolve.collisions import resolve_collisions
from schemagate.resolve.compat import check_compat
from schemagate.resolve.deterministic import DictionaryResolver, ExactResolver, SynonymResolver
from schemagate.resolve.model import ModelResolver
from schemagate.schema.target import TargetSchema


@dataclass
class ResolutionResult:
    proposals: list[MappingProposal]
    vetoes: dict[str, list[dict]] = field(default_factory=dict)

    def mapped(self) -> dict[str, str]:
        return {p.source_column: p.target_field for p in self.proposals if p.target_field}

    def by_column(self) -> dict[str, MappingProposal]:
        return {p.source_column: p for p in self.proposals}


def apply_compat(
    proposal: MappingProposal, profile: ColumnProfile, ctx: ResolveContext
) -> tuple[MappingProposal | None, dict | None]:
    """Downgrade or veto a proposal whose values do not fit the target field."""
    if proposal.target_field is None:
        return proposal, None
    result = check_compat(profile, ctx.schema.field(proposal.target_field))
    evidence = {**proposal.evidence, "compat": {"status": result.status, "rate": result.rate,
                                                "reason": result.reason}}
    if result.status == "ok":
        return replace(proposal, evidence=evidence), None
    if result.status == "downgrade":
        conf = round(max(0.0, proposal.confidence - ctx.policy.compat_penalty), 4)
        return replace(proposal, confidence=conf, evidence=evidence,
                       rationale=f"{proposal.rationale} | downgraded: {result.reason}"), None
    veto = {"tier": proposal.tier.value, "target_field": proposal.target_field,
            "reason": result.reason, "rate": result.rate}
    return None, veto


class ResolverChain:
    def __init__(self, resolvers: list[Resolver]) -> None:
        self.resolvers = resolvers

    @classmethod
    def default(cls, model: ModelResolver | None = None, synonym_threshold: float = 0.75) -> ResolverChain:
        chain: list[Resolver] = [ExactResolver(), DictionaryResolver(), SynonymResolver(synonym_threshold)]
        if model is not None:
            chain.append(model)
        return cls(chain)

    def run(self, schema: TargetSchema, profiles: list[ColumnProfile],
            policy: ConfidencePolicy | None = None,
            claimed: dict[str, str] | None = None) -> ResolutionResult:
        """Resolve columns. ``claimed`` maps target fields already held (e.g. by carried-over
        mappings) to their source column; proposals for those targets are dropped."""
        ctx = ResolveContext(schema=schema, policy=policy or ConfidencePolicy(),
                             claimed=dict(claimed or {}))
        held = set(ctx.claimed)
        by_name = {p.name: p for p in profiles}
        remaining = list(profiles)
        accepted: dict[str, MappingProposal] = {}
        vetoes: dict[str, list[dict]] = {}
        for resolver in self.resolvers:
            if not remaining:
                break
            for prop in resolver.resolve(ctx, remaining):
                if prop.source_column not in by_name or prop.source_column in accepted:
                    continue
                if prop.target_field in held:
                    continue
                if prop.target_field is None:
                    if prop.tier is Tier.UNRESOLVED:
                        accepted.setdefault(prop.source_column, prop)
                    continue
                checked, veto = apply_compat(prop, by_name[prop.source_column], ctx)
                if checked is None:
                    vetoes.setdefault(prop.source_column, []).append(veto or {})
                    continue
                accepted[prop.source_column] = checked
                if checked.tier.deterministic:
                    ctx.claimed.setdefault(checked.target_field or "", checked.source_column)
            remaining = [p for p in profiles if p.name not in accepted]
        proposals: list[MappingProposal] = []
        for p in profiles:
            prop = accepted.get(p.name)
            if prop is None:
                reasons = vetoes.get(p.name)
                prop = MappingProposal(
                    source_column=p.name, target_field=None, confidence=0.0, tier=Tier.UNRESOLVED,
                    rationale="no resolver produced a compatible mapping"
                    + (f" ({len(reasons)} vetoed by value checks)" if reasons else ""),
                    evidence={}, proposer="pipeline",
                )
            if p.name in vetoes:
                prop = replace(prop, evidence={**prop.evidence, "vetoes": vetoes[p.name]})
            proposals.append(prop)
        return ResolutionResult(proposals=resolve_collisions(proposals), vetoes=vetoes)
