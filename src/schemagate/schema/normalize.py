from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

CURRENCY_CODES = frozenset(
    {"usd", "eur", "gbp", "cad", "aud", "jpy", "chf", "cny", "inr", "mxn", "sek", "nok", "dkk"}
)
UNIT_TOKENS = frozenset({"kg", "kgs", "lb", "lbs", "g", "mg", "ml", "l", "km", "mi", "m", "cm"})
CURRENCY_SYMBOLS = {"$": "usd", "€": "eur", "£": "gbp", "¥": "jpy"}

_CAMEL_1 = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_2 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_BRACKETED = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
_NON_ALNUM = re.compile(r"[^0-9a-zA-Z]+")


@dataclass(frozen=True)
class NormalizedName:
    raw: str
    tokens: tuple[str, ...]
    unit_hints: tuple[str, ...] = field(default=())

    @property
    def snake(self) -> str:
        return "_".join(self.tokens)


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def split_camel(text: str) -> str:
    return _CAMEL_1.sub(r"\1 \2", _CAMEL_2.sub(r"\1 \2", text))


def normalize_name(raw: str) -> NormalizedName:
    """Normalize a column header into lowercase tokens plus extracted unit hints.

    Bracketed units such as "(USD)" or "[kg]", currency symbols and "%" are removed from
    the token stream and returned as hints; "#" becomes the token "number".
    """
    text = _strip_accents(str(raw)).strip()
    hints: list[str] = []

    def _bracket(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        inner_tokens = [t.lower() for t in _NON_ALNUM.split(inner) if t]
        if inner == "%" or inner_tokens in (["pct"], ["percent"]):
            hints.append("percent")
            return " "
        if inner in CURRENCY_SYMBOLS:
            hints.append(CURRENCY_SYMBOLS[inner])
            return " "
        if inner_tokens and all(t in CURRENCY_CODES or t in UNIT_TOKENS for t in inner_tokens):
            hints.extend(inner_tokens)
            return " "
        return f" {inner} "

    text = _BRACKETED.sub(_bracket, text)
    if "%" in text:
        hints.append("percent")
        text = text.replace("%", " ")
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in text:
            hints.append(code)
            text = text.replace(sym, " ")
    text = text.replace("#", " number ")
    text = split_camel(text)
    tokens = [t.lower() for t in _NON_ALNUM.split(text) if t]
    while len(tokens) > 1 and tokens[-1] in CURRENCY_CODES:
        hints.append(tokens.pop())
    return NormalizedName(raw=str(raw), tokens=tuple(tokens), unit_hints=tuple(dict.fromkeys(hints)))


def snake_case(raw: str) -> str:
    snake = normalize_name(raw).snake
    return snake or "column"


def extension_column_name(raw: str) -> str:
    return f"ext_{snake_case(raw)}"
