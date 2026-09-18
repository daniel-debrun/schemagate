from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from schemagate.ingest.profile import (
    NUMERIC_TYPES,
    ColumnProfile,
    bin_distribution,
    category_distribution,
    numeric_value,
)
from schemagate.llm.heuristic import char_ngrams, jaccard
from schemagate.models import MappingProposal
from schemagate.resolve.compat import check_compat
from schemagate.resolve.pipeline import ResolverChain
from schemagate.schema.normalize import normalize_name
from schemagate.schema.target import TargetSchema

PSI_THRESHOLD = 0.25
PSI_MIN_VALUES = 50
CATEGORICAL_MAX_DISTINCT_RATIO = 0.5
NULL_RATE_JUMP = 0.2
RENAME_THRESHOLD = 0.6
EPS = 1e-4

INFO, WARNING, BREAKING = "info", "warning", "breaking"


@dataclass
class DriftEvent:
    kind: str
    severity: str
    source_column: str | None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "severity": self.severity, "source_column": self.source_column,
                "detail": self.detail}


def psi(expected: Sequence[float], actual: Sequence[float]) -> float:
    """Population stability index between two aligned distributions."""
    total = 0.0
    for e, a in zip(expected, actual):
        e, a = max(e, EPS), max(a, EPS)
        total += (a - e) * math.log(a / e)
    return total


def distribution_psi(baseline: ColumnProfile, values: Sequence[str]) -> float | None:
    """PSI of new values against the baseline profile, or None when the comparison is not meaningful
    (too few values, identifier-like or date columns)."""
    baseline_non_null = baseline.row_count * (1 - baseline.null_rate)
    new_non_null = [v for v in values if v.strip()]
    if min(baseline_non_null, len(new_non_null)) < PSI_MIN_VALUES or baseline.inferred_type == "date":
        return None
    if baseline.numeric_edges is not None and baseline.numeric_distribution is not None:
        nums = [n for n in (numeric_value(v) for v in values) if n is not None]
        if not nums:
            return None
        return psi(baseline.numeric_distribution, bin_distribution(nums, baseline.numeric_edges))
    if baseline.top_values and baseline.distinct_count <= CATEGORICAL_MAX_DISTINCT_RATIO * baseline_non_null:
        new = category_distribution(values)
        keys = set(baseline.top_values) | set(new)
        return psi([baseline.top_values.get(k, 0.0) for k in keys], [new.get(k, 0.0) for k in keys])
    return None


def profile_similarity(old: ColumnProfile, new: ColumnProfile) -> float:
    type_match = 1.0 if old.inferred_type == new.inferred_type else 0.0
    sig_overlap = sum(min(old.pattern_signatures.get(k, 0.0), v)
                      for k, v in new.pattern_signatures.items())
    if old.top_values and new.top_values:
        value_overlap = sum(min(old.top_values.get(k, 0.0), v) for k, v in new.top_values.items())
    elif old.inferred_type in NUMERIC_TYPES and new.inferred_type in NUMERIC_TYPES:
        value_overlap = 0.5
    else:
        value_overlap = len(set(old.sample_values) & set(new.sample_values)) / max(
            1, len(set(old.sample_values) | set(new.sample_values)))
    return 0.4 * type_match + 0.3 * sig_overlap + 0.3 * value_overlap


def _name_similarity(a: str, b: str) -> float:
    return jaccard(char_ngrams(normalize_name(a).snake), char_ngrams(normalize_name(b).snake))


def detect_drift(
    schema: TargetSchema,
    mapping: Sequence[MappingProposal],
    baseline: Sequence[ColumnProfile],
    current: Sequence[ColumnProfile],
    values: Mapping[str, Sequence[str]] | None = None,
) -> list[DriftEvent]:
    """Compare a new source object's profile to the approved spec. Detection only; no remediation."""
    base = {p.name: p for p in baseline}
    cur = {p.name: p for p in current}
    targets = {m.source_column: m.target_field for m in mapping}
    events: list[DriftEvent] = []

    removed = [n for n in base if n not in cur]
    added = [n for n in cur if n not in base]

    deterministic = ResolverChain.default()
    resolved_added = {p.source_column: p.target_field
                      for p in deterministic.run(schema, [cur[a] for a in added]).proposals}
    pairs = []
    for r in removed:
        for a in added:
            score = 0.5 * profile_similarity(base[r], cur[a]) + 0.3 * _name_similarity(r, a)
            same_target = targets.get(r) is not None and resolved_added.get(a) == targets.get(r)
            if same_target:
                score += 0.3
            pairs.append((min(score, 1.0), r, a, same_target))
    pairs.sort(key=lambda x: (-x[0], x[1], x[2]))
    renamed_from: set[str] = set()
    renamed_to: set[str] = set()
    for score, r, a, same_target in pairs:
        if score < RENAME_THRESHOLD or r in renamed_from or a in renamed_to:
            continue
        renamed_from.add(r)
        renamed_to.add(a)
        events.append(DriftEvent("renamed", BREAKING if targets.get(r) else WARNING, a, {
            "from": r, "to": a, "score": round(score, 4), "target_field": targets.get(r),
            "resolver_agrees": same_target}))
    for r in removed:
        if r not in renamed_from:
            events.append(DriftEvent("removed", BREAKING if targets.get(r) else WARNING, r,
                                     {"target_field": targets.get(r)}))
    for a in added:
        if a not in renamed_to:
            events.append(DriftEvent("added", WARNING, a, {
                "inferred_type": cur[a].inferred_type,
                "deterministic_target": resolved_added.get(a)}))

    for name in base.keys() & cur.keys():
        old, new = base[name], cur[name]
        target = targets.get(name)
        if "empty" not in (old.inferred_type, new.inferred_type) and old.inferred_type != new.inferred_type:
            severity = WARNING
            detail: dict[str, Any] = {"from": old.inferred_type, "to": new.inferred_type}
            if target:
                compat = check_compat(new, schema.field(target))
                detail["compat"] = {"status": compat.status, "reason": compat.reason}
                if compat.status == "veto":
                    severity = BREAKING
            events.append(DriftEvent("type_changed", severity, name, detail))
        elif old.date_format and new.date_format and old.date_format != new.date_format:
            events.append(DriftEvent("format_changed", BREAKING if target else WARNING, name,
                                     {"from": old.date_format, "to": new.date_format}))
        if abs(new.null_rate - old.null_rate) > NULL_RATE_JUMP:
            events.append(DriftEvent("null_rate_shift", WARNING, name,
                                     {"from": old.null_rate, "to": new.null_rate}))
        if values is not None and name in values and old.inferred_type == new.inferred_type:
            score = distribution_psi(old, values[name])
            if score is not None and score > PSI_THRESHOLD:
                events.append(DriftEvent("distribution_shift", WARNING, name,
                                         {"psi": round(score, 4), "threshold": PSI_THRESHOLD}))
    order = {BREAKING: 0, WARNING: 1, INFO: 2}
    events.sort(key=lambda e: (order[e.severity], e.kind, e.source_column or ""))
    return events
