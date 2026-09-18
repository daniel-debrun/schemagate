from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from schemagate.ingest.values import cell_type, is_null


@dataclass(frozen=True)
class HeaderCandidate:
    row_index: int
    score: float
    stringness: float
    uniqueness: float
    fill: float
    consistency: float


def _row_width(rows: list[list[str]]) -> int:
    return max((len(r) for r in rows), default=0)


def score_header_rows(
    rows: list[list[str]], max_scan: int = 20, lookahead: int = 15
) -> list[HeaderCandidate]:
    """Score each of the first rows as a potential header.

    A header row is mostly non-numeric text, has unique labels, spans most of the table width,
    and the rows beneath it have a consistent per-column value type.
    """
    width = _row_width(rows[: max_scan + lookahead])
    if width == 0:
        return []
    candidates: list[HeaderCandidate] = []
    for i, row in enumerate(rows[:max_scan]):
        cells = [c for c in row if not is_null(c)]
        if not cells:
            continue
        stringness = sum(1 for c in cells if cell_type(c) == "string") / len(cells)
        uniqueness = len({c.strip().lower() for c in cells}) / len(cells)
        fill = len(cells) / width
        below = [r for r in rows[i + 1 : i + 1 + lookahead] if any(not is_null(c) for c in r)]
        if below:
            col_scores = []
            for j in range(width):
                types = [cell_type(r[j]) for r in below if j < len(r) and not is_null(r[j])]
                if not types:
                    continue
                col_scores.append(Counter(types).most_common(1)[0][1] / len(types))
            populated = sum(1 for j in range(width) if any(j < len(r) and not is_null(r[j]) for r in below))
            consistency = (sum(col_scores) / len(col_scores)) if col_scores else 0.0
            coverage = populated / width
        else:
            consistency, coverage = 0.0, 0.0
        score = (
            1.5 * stringness
            + 1.0 * uniqueness
            + 2.0 * fill
            + 1.5 * consistency * coverage
            - (0.6 if fill < 0.5 else 0.0)
        )
        candidates.append(HeaderCandidate(i, round(score, 4), stringness, uniqueness, fill, consistency))
    return candidates


def detect_header_row(rows: list[list[str]], max_scan: int = 20) -> int:
    candidates = score_header_rows(rows, max_scan=max_scan)
    if not candidates:
        return 0
    best = max(candidates, key=lambda c: (c.score, -c.row_index))
    return best.row_index
