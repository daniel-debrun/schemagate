from __future__ import annotations

from dataclasses import replace

from schemagate.models import MappingProposal, Tier
from schemagate.schema.normalize import extension_column_name


def _winner_key(p: MappingProposal) -> tuple[float, int, str]:
    return (-p.confidence, p.tier.rank, p.source_column)


def resolve_collisions(proposals: list[MappingProposal]) -> list[MappingProposal]:
    """Keep one source column per target; demote the rest (and all unmapped columns) to ext_ columns.

    The winner has the highest confidence; ties go to the stronger tier, then to the source column
    name so the outcome is deterministic.
    """
    by_target: dict[str, list[MappingProposal]] = {}
    for p in proposals:
        if p.target_field:
            by_target.setdefault(p.target_field, []).append(p)
    losers: dict[str, dict] = {}
    for target, group in by_target.items():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=_winner_key)
        winner = ordered[0]
        for loser in ordered[1:]:
            losers[loser.source_column] = {
                "target_field": target,
                "winner": winner.source_column,
                "winner_confidence": winner.confidence,
                "winner_tier": winner.tier.value,
                "demoted_confidence": loser.confidence,
                "demoted_tier": loser.tier.value,
            }
    out: list[MappingProposal] = []
    used_ext: set[str] = set()
    for p in proposals:
        if p.source_column in losers:
            conflict = losers[p.source_column]
            p = replace(
                p,
                target_field=None,
                conflict=conflict,
                rationale=(
                    f"{p.rationale} | demoted: '{conflict['winner']}' holds "
                    f"'{conflict['target_field']}' with higher precedence"
                ),
            )
        if p.target_field is None:
            ext = extension_column_name(p.source_column)
            base, n = ext, 2
            while ext in used_ext:
                ext = f"{base}_{n}"
                n += 1
            used_ext.add(ext)
            p = replace(p, extension_column=ext)
            if p.tier is not Tier.UNRESOLVED and p.conflict is None:
                p = replace(p, tier=Tier.UNRESOLVED)
        out.append(p)
    return out
