from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from schemagate.ingest.profile import ColumnProfile
from schemagate.models import MappingProposal, Tier
from schemagate.resolve.collisions import resolve_collisions
from schemagate.resolve.compat import check_compat
from schemagate.resolve.pipeline import ResolverChain
from schemagate.store.service import GovernanceService, Spec


def propose_expand(
    service: GovernanceService,
    spec: Spec,
    object_id: int,
    events: Sequence[dict],
    chain: ResolverChain,
    actor: str,
) -> int:
    """Expand step: propose a new spec version that tolerates both old and new layouts.

    Unchanged mapped columns carry over, detected renames are proposed (and require an explicit
    reviewer accept), and new columns go through the resolver chain. Target columns whose source
    disappeared stay in the target table as nullable; nothing is dropped (Contract is a separate,
    later step). The result is a pending spec; switching the feed to it is a separate gated action.
    """
    obj = service.source_object(object_id)
    profiles: list[ColumnProfile] = obj["profiles"]
    by_name = {p.name: p for p in profiles}
    schema = spec.schema
    old = {m.source_column: m for m in spec.mapping()}
    renames = {e["detail"]["to"]: e for e in events if e["kind"] == "renamed"}

    proposals: dict[str, MappingProposal] = {}
    claimed: dict[str, str] = {}
    for name, prof in by_name.items():
        prev = old.get(name)
        if prev is not None and prev.target_field:
            compat = check_compat(prof, schema.field(prev.target_field))
            if compat.status == "veto":
                continue
            proposals[name] = MappingProposal(
                source_column=name, target_field=prev.target_field,
                confidence=service.confidence_policy.exact, tier=Tier.CARRYOVER,
                rationale=f"carried over from approved spec v{spec.version}",
                evidence={"from_spec_id": spec.id, "compat": compat.status},
                proposer=f"remediation:spec-{spec.id}")
            claimed[prev.target_field] = name
        elif name in renames:
            ev = renames[name]
            target = ev["detail"].get("target_field")
            if target and target not in claimed and check_compat(prof, schema.field(target)).status != "veto":
                proposals[name] = MappingProposal(
                    source_column=name, target_field=target,
                    confidence=round(min(service.confidence_policy.synonym, ev["detail"]["score"]), 4),
                    tier=Tier.RENAME,
                    rationale=(f"detected rename of '{ev['detail']['from']}' (score "
                               f"{ev['detail']['score']:.2f}); previously mapped to '{target}'"),
                    evidence={"drift_event_id": ev.get("id"), **ev["detail"]},
                    proposer=f"remediation:spec-{spec.id}")
                claimed[target] = name

    rest = [p for p in profiles if p.name not in proposals]
    resolved = chain.run(schema, rest, service.confidence_policy, claimed=claimed).proposals
    merged = [proposals.get(p.name) or next(r for r in resolved if r.source_column == p.name)
              for p in profiles]
    merged = resolve_collisions([replace(m, extension_column=None) for m in merged])
    retained = sorted({old[e["source_column"]].target_field for e in events
                       if e["kind"] == "removed" and e["source_column"] in old
                       and old[e["source_column"]].target_field})
    spec_id = service.create_spec(
        spec.feed_id, merged, actor, source_object_id=object_id, source_profiles=profiles,
        parent_spec_id=spec.id, change_kind="expand")
    service.audit.append(actor, "drift.expand_proposed", "mapping_spec", spec_id, {
        "parent_spec_id": spec.id, "source_object_id": object_id,
        "drift_event_ids": [e.get("id") for e in events],
        "retained_nullable_targets": retained})
    return spec_id
