"""Parsers for string cell values. Everything is ingested as text; these decide what it could be."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

NULL_TOKENS = frozenset({"", "na", "n/a", "null", "none", "nan", "-", "--", "#n/a", "nil"})
TRUE_TOKENS = frozenset({"true", "t", "yes", "y"})
FALSE_TOKENS = frozenset({"false", "f", "no", "n"})

_INT = re.compile(r"^[+-]?(\d+|\d{1,3}(,\d{3})+)$")
_FLOAT = re.compile(r"^[+-]?((\d+|\d{1,3}(,\d{3})+)(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_CURRENCY = re.compile(
    r"^(?P<neg1>\()?\s*(?P<sign>[+-])?\s*(?P<pre>[$€£¥]|USD|EUR|GBP|CAD|AUD|JPY)?\s*"
    r"(?P<num>(\d+|\d{1,3}(,\d{3})+)(\.\d+)?)\s*(?P<post>[$€£¥]|USD|EUR|GBP|CAD|AUD|JPY)?\s*(?P<neg2>\))?$",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)\s*%$")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
)}

ValueType = str  # one of: int, float, currency, percent, date, bool, string, empty


def is_null(value: str | None) -> bool:
    return value is None or value.strip().lower() in NULL_TOKENS


def parse_int(value: str) -> int | None:
    v = value.strip()
    return int(v.replace(",", "")) if _INT.match(v) else None


def parse_float(value: str) -> float | None:
    v = value.strip()
    return float(v.replace(",", "")) if _FLOAT.match(v) else None


def parse_currency(value: str) -> float | None:
    m = _CURRENCY.match(value.strip())
    if not m:
        return None
    if bool(m.group("neg1")) != bool(m.group("neg2")):
        return None
    amount = float(m.group("num").replace(",", ""))
    if m.group("neg1") or m.group("sign") == "-":
        amount = -amount
    return amount


def has_currency_marker(value: str) -> bool:
    m = _CURRENCY.match(value.strip())
    return bool(m and (m.group("pre") or m.group("post") or m.group("neg1")))


def parse_percent(value: str) -> float | None:
    v = value.strip()
    return float(v.rstrip("%").strip()) if _PERCENT.match(v) else None


def parse_bool(value: str) -> bool | None:
    v = value.strip().lower()
    if v in TRUE_TOKENS:
        return True
    if v in FALSE_TOKENS:
        return False
    return None


@dataclass(frozen=True)
class DateFormat:
    """A three-part date layout: component order, separator, and whether the month is a name."""

    order: str
    sep: str
    month_name: bool = False

    @property
    def token(self) -> str:
        return f"{self.order}{self.sep}{'mon' if self.month_name else ''}"

    @classmethod
    def from_token(cls, token: str) -> DateFormat:
        month_name = token.endswith("mon")
        core = token[:-3] if month_name else token
        return cls(order=core[:3], sep=core[3:], month_name=month_name)

    def parse(self, value: str) -> date | None:
        parts = value.strip().split(self.sep)
        if len(parts) != 3:
            return None
        comp = dict(zip(self.order, parts))
        try:
            if self.month_name:
                month = _MONTHS.get(comp["m"][:3].lower())
                if month is None:
                    return None
            else:
                if not comp["m"].isdigit() or len(comp["m"]) > 2:
                    return None
                month = int(comp["m"])
            if not (comp["d"].isdigit() and len(comp["d"]) <= 2):
                return None
            if not (comp["y"].isdigit() and len(comp["y"]) == 4):
                return None
            return date(int(comp["y"]), month, int(comp["d"]))
        except ValueError:
            return None


DATE_FORMATS: tuple[DateFormat, ...] = (
    DateFormat("ymd", "-"),
    DateFormat("ymd", "/"),
    DateFormat("mdy", "/"),
    DateFormat("dmy", "/"),
    DateFormat("dmy", "."),
    DateFormat("dmy", "-"),
    DateFormat("mdy", "-"),
    DateFormat("dmy", "-", True),
    DateFormat("dmy", " ", True),
)


def _strip_time(value: str) -> str:
    v = value.strip()
    if "T" in v[10:11]:
        return v[:10]
    if len(v) > 10 and v[10] == " " and ":" in v[10:]:
        return v[:10]
    return v


def parse_date(value: str, fmt: DateFormat | None = None) -> date | None:
    v = _strip_time(value)
    if fmt is not None:
        return fmt.parse(v)
    for f in DATE_FORMATS:
        d = f.parse(v)
        if d is not None:
            return d
    return None


def detect_date_format(values: Sequence[str], threshold: float = 0.95) -> DateFormat | None:
    vals = [_strip_time(v) for v in values if not is_null(v)]
    if not vals:
        return None
    best: tuple[float, int, DateFormat] | None = None
    for i, fmt in enumerate(DATE_FORMATS):
        ok = sum(1 for v in vals if fmt.parse(v) is not None)
        rate = ok / len(vals)
        if rate >= threshold and (best is None or rate > best[0]):
            best = (rate, i, fmt)
    return best[2] if best else None


def _rate(values: Sequence[str], pred) -> float:
    return sum(1 for v in values if pred(v)) / len(values) if values else 0.0


def infer_type(values: Iterable[str], threshold: float = 0.95) -> tuple[ValueType, DateFormat | None]:
    """Infer the dominant value type of a column of strings, tolerating a few dirty cells."""
    vals = [v for v in values if not is_null(v)]
    if not vals:
        return "empty", None
    if _rate(vals, lambda v: parse_bool(v) is not None) >= threshold:
        return "bool", None
    if _rate(vals, lambda v: parse_int(v) is not None) >= threshold:
        return "int", None
    if _rate(vals, lambda v: parse_float(v) is not None) >= threshold:
        return "float", None
    if _rate(vals, lambda v: parse_percent(v) is not None) >= threshold:
        return "percent", None
    if _rate(vals, lambda v: parse_currency(v) is not None) >= threshold and any(
        has_currency_marker(v) for v in vals
    ):
        return "currency", None
    fmt = detect_date_format(vals, threshold)
    if fmt is not None:
        return "date", fmt
    return "string", None


def cell_type(value: str) -> ValueType:
    if is_null(value):
        return "empty"
    if parse_int(value) is not None or parse_float(value) is not None:
        return "number"
    if parse_percent(value) is not None or parse_currency(value) is not None:
        return "number"
    if parse_date(value) is not None:
        return "date"
    return "string"


def pattern_signature(value: str, max_len: int = 24) -> str:
    """Collapse a value into a shape: digits->9, upper->A, lower->a, runs collapsed."""
    out: list[str] = []
    for ch in value.strip()[:64]:
        if ch.isdigit():
            c = "9"
        elif ch.isalpha():
            c = "A" if ch.isupper() else "a"
        elif ch.isspace():
            c = " "
        else:
            c = ch
        if not out or out[-1] != c:
            out.append(c)
    return "".join(out)[:max_len]
