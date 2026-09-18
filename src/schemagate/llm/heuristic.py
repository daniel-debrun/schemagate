from __future__ import annotations

import json
import re

from schemagate.llm.base import ColumnBrief, FieldBrief, MappingRequest, ProviderResponse
from schemagate.schema.normalize import normalize_name
from schemagate.schema.vocabulary import Vocabulary

_WORD = re.compile(r"[a-z0-9]+")
NUMERIC_KIND = {"int", "float", "currency", "percent"}


def char_ngrams(text: str, n: int = 3) -> set[str]:
    padded = f"  {text} "
    return {padded[i : i + n] for i in range(len(padded) - n + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class HeuristicProvider:
    """Offline, dependency-free stand-in for an LLM.

    Scores each unresolved column against each target field using character trigram and token
    similarity over header vs name/aliases, overlap with the field description, value-type fit
    and allowed-value/pattern fit, then assigns greedily one-to-one. It speaks the same JSON
    protocol as the LLM providers so its output passes through identical validation.
    """

    def __init__(self, min_score: float = 0.35, vocabulary: Vocabulary | None = None) -> None:
        self.min_score = min_score
        self.vocab = vocabulary or Vocabulary.default()
        self.model_id = "heuristic-v1"
        self._token_cache: dict[str, tuple[str, ...]] = {}

    def _tokens(self, text: str) -> tuple[str, ...]:
        cached = self._token_cache.get(text)
        if cached is None:
            cached = self._token_cache[text] = self.vocab.expand(normalize_name(text).tokens)
        return cached

    def _name_similarity(self, col: ColumnBrief, fld: FieldBrief) -> tuple[float, str]:
        src_tokens = self._tokens(col.name)
        src = " ".join(src_tokens)
        best = (0.0, fld.name)
        for label in [fld.name, *fld.aliases]:
            tgt_tokens = self._tokens(label)
            tgt = " ".join(tgt_tokens)
            tri = jaccard(char_ngrams(src), char_ngrams(tgt))
            tok = jaccard(set(src_tokens), set(tgt_tokens))
            s = 0.6 * tri + 0.4 * tok
            if s > best[0]:
                best = (s, label)
        return best

    def _description_overlap(self, col: ColumnBrief, fld: FieldBrief) -> float:
        src = set(self._tokens(col.name))
        if not src or not fld.description:
            return 0.0
        desc = set(self.vocab.expand(tuple(_WORD.findall(fld.description.lower()))))
        return len(src & desc) / len(src)

    @staticmethod
    def _type_fit(col: ColumnBrief, fld: FieldBrief) -> float:
        if fld.type == "string":
            return 0.5 if col.inferred_type in NUMERIC_KIND | {"date", "bool"} else 1.0
        if not col.parse_rates:
            return 0.5
        return col.parse_rates.get(fld.type, 0.0)

    @staticmethod
    def _value_fit(col: ColumnBrief, fld: FieldBrief) -> float | None:
        if fld.allowed_values:
            allowed = {a.lower() for a in fld.allowed_values}
            s = col.sample_values
            return sum(1 for v in s if v.lower() in allowed) / len(s) if s else None
        if fld.pattern:
            rx = re.compile(fld.pattern)
            s = col.sample_values
            return sum(1 for v in s if rx.fullmatch(v)) / len(s) if s else None
        return None

    def score(self, col: ColumnBrief, fld: FieldBrief) -> tuple[float, dict]:
        name_sim, label = self._name_similarity(col, fld)
        desc = self._description_overlap(col, fld)
        type_fit = self._type_fit(col, fld)
        value_fit = self._value_fit(col, fld)
        value_component = type_fit if value_fit is None else value_fit
        total = 0.6 * name_sim + 0.1 * desc + 0.15 * type_fit + 0.15 * value_component
        if type_fit < 0.5:
            total *= 0.5
        return total, {"label": label, "name": name_sim, "description": desc, "type": type_fit,
                       "value": value_fit}

    def complete(self, request: MappingRequest) -> ProviderResponse:
        candidates = []
        fields = [f for f in request.fields if f.name not in request.already_mapped]
        for col in request.columns:
            for fld in fields:
                s, parts = self.score(col, fld)
                candidates.append((s, col.name, fld.name, parts, col, fld))
        candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
        assigned_cols: dict[str, dict] = {}
        used_fields: set[str] = set()
        for s, cname, fname, parts, col, fld in candidates:
            if cname in assigned_cols or fname in used_fields or s < self.min_score:
                continue
            used_fields.add(fname)
            evidence = [f"header '{cname}' ~ '{parts['label']}' (name similarity {parts['name']:.2f})"]
            if col.sample_values:
                evidence.append(f"sample values: {', '.join(col.sample_values[:3])}")
            if fld.description:
                evidence.append(f"description: {fld.description}")
            assigned_cols[cname] = {
                "source_column": cname,
                "target_field": fname,
                "confidence": round(min(1.0, s), 4),
                "rationale": (
                    f"Header resembles '{parts['label']}' (similarity {parts['name']:.2f}); "
                    f"{parts['type']:.0%} of values fit type {fld.type}."
                ),
                "evidence": evidence,
            }
        mappings = []
        for col in request.columns:
            mappings.append(assigned_cols.get(col.name, {
                "source_column": col.name,
                "target_field": None,
                "confidence": 0.0,
                "rationale": "No target field scored above the minimum similarity.",
                "evidence": [],
            }))
        return ProviderResponse(text=json.dumps({"mappings": mappings}), model_id=self.model_id)
