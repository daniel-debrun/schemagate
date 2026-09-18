from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from schemagate.ingest.values import (
    infer_type,
    is_null,
    parse_bool,
    parse_currency,
    parse_date,
    parse_float,
    parse_int,
    parse_percent,
    pattern_signature,
)

NUMERIC_TYPES = frozenset({"int", "float", "currency", "percent"})
TOP_VALUES_MAX_DISTINCT = 50
HISTOGRAM_BINS = 10


def numeric_value(value: str) -> float | None:
    for parser in (parse_float, parse_currency, parse_percent):
        v = parser(value)
        if v is not None:
            return v
    return None


@dataclass
class ColumnProfile:
    name: str
    position: int
    inferred_type: str
    null_rate: float
    distinct_count: int
    row_count: int
    sample_values: list[str] = field(default_factory=list)
    pattern_signatures: dict[str, float] = field(default_factory=dict)
    date_format: str | None = None
    numeric_edges: list[float] | None = None
    numeric_distribution: list[float] | None = None
    top_values: dict[str, float] | None = None
    parse_rates: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ColumnProfile:
        return cls(**d)


def quantile_edges(values: Sequence[float], bins: int = HISTOGRAM_BINS) -> list[float]:
    s = sorted(values)
    if not s:
        return []
    edges = []
    for i in range(1, bins):
        edges.append(s[min(len(s) - 1, round(i * len(s) / bins))])
    return sorted(set(edges))


def bin_distribution(values: Sequence[float], edges: Sequence[float]) -> list[float]:
    counts = [0] * (len(edges) + 1)
    for v in values:
        idx = 0
        while idx < len(edges) and v > edges[idx]:
            idx += 1
        counts[idx] += 1
    total = len(values) or 1
    return [c / total for c in counts]


def category_distribution(values: Sequence[str]) -> dict[str, float]:
    non_null = [v.strip() for v in values if not is_null(v)]
    total = len(non_null) or 1
    return {k: c / total for k, c in Counter(non_null).most_common()}


def parse_rates(non_null: Sequence[str]) -> dict[str, float]:
    if not non_null:
        return {}
    n = len(non_null)

    def rate(pred) -> float:
        return round(sum(1 for v in non_null if pred(v)) / n, 4)

    return {
        "integer": rate(lambda v: parse_int(v) is not None),
        "number": rate(lambda v: numeric_value(v) is not None),
        "date": rate(lambda v: parse_date(v) is not None),
        "boolean": rate(lambda v: parse_bool(v) is not None or v.strip() in ("0", "1")),
    }


def profile_column(name: str, position: int, values: Sequence[str], samples: int = 5) -> ColumnProfile:
    row_count = len(values)
    non_null = [v.strip() for v in values if not is_null(v)]
    inferred, date_fmt = infer_type(non_null)
    distinct = list(dict.fromkeys(non_null))
    sigs = Counter(pattern_signature(v) for v in non_null)
    total = len(non_null) or 1
    prof = ColumnProfile(
        name=name,
        position=position,
        inferred_type=inferred,
        null_rate=round(1 - len(non_null) / row_count, 4) if row_count else 1.0,
        distinct_count=len(distinct),
        row_count=row_count,
        sample_values=distinct[:samples],
        pattern_signatures={k: round(c / total, 4) for k, c in sigs.most_common(3)},
        date_format=date_fmt.token if date_fmt else None,
        parse_rates=parse_rates(non_null),
    )
    if inferred in NUMERIC_TYPES:
        nums = [n for n in (numeric_value(v) for v in non_null) if n is not None]
        prof.numeric_edges = quantile_edges(nums)
        prof.numeric_distribution = bin_distribution(nums, prof.numeric_edges)
    elif len(distinct) <= TOP_VALUES_MAX_DISTINCT:
        prof.top_values = {k: round(v, 4) for k, v in category_distribution(non_null).items()}
    return prof


def profile_table(columns: Sequence[str], rows: Sequence[Sequence[str]]) -> list[ColumnProfile]:
    out = []
    for j, col in enumerate(columns):
        values = [r[j] if j < len(r) else "" for r in rows]
        out.append(profile_column(col, j, values))
    return out
