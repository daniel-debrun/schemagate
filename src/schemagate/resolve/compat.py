from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from schemagate.ingest.profile import ColumnProfile
from schemagate.schema.target import FieldSpec

OK_RATE = 0.9
VETO_RATE = 0.5


@dataclass(frozen=True)
class CompatResult:
    status: Literal["ok", "downgrade", "veto"]
    rate: float
    reason: str


def _status(rate: float) -> Literal["ok", "downgrade", "veto"]:
    if rate >= OK_RATE:
        return "ok"
    if rate >= VETO_RATE:
        return "downgrade"
    return "veto"


def check_compat(profile: ColumnProfile, spec: FieldSpec) -> CompatResult:
    """Check whether a column's observed values fit a target field's type and value constraints."""
    if profile.row_count == 0 or not profile.parse_rates:
        return CompatResult("ok", 1.0, "no non-null values to check")
    rates: list[tuple[float, str]] = []
    if spec.type != "string":
        r = profile.parse_rates.get(spec.type, 0.0)
        rates.append((r, f"{r:.0%} of values parse as {spec.type}"))
    if spec.allowed_values:
        allowed = {a.lower() for a in spec.allowed_values}
        if profile.top_values:
            r = sum(p for v, p in profile.top_values.items() if v.lower() in allowed)
        else:
            samples = profile.sample_values or []
            r = sum(1 for v in samples if v.lower() in allowed) / len(samples) if samples else 1.0
        rates.append((r, f"{r:.0%} of values are in the allowed set"))
    if spec.pattern:
        rx = re.compile(spec.pattern)
        values = list(profile.top_values) if profile.top_values else profile.sample_values
        r = sum(1 for v in values if rx.fullmatch(v)) / len(values) if values else 1.0
        rates.append((r, f"{r:.0%} of sampled values match pattern {spec.pattern}"))
    if not rates:
        return CompatResult("ok", 1.0, "string target accepts any value")
    rate, reason = min(rates, key=lambda x: x[0])
    return CompatResult(_status(rate), round(rate, 4), reason)
