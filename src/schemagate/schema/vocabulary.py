from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

import yaml

from schemagate.schema.normalize import normalize_name


@lru_cache(maxsize=1)
def _load_default() -> tuple[dict[str, tuple[str, ...]], frozenset[str]]:
    text = resources.files("schemagate.schema").joinpath("data/synonyms.yaml").read_text("utf-8")
    data = yaml.safe_load(text)
    syn = {str(k).lower(): tuple(str(v).lower().split()) for k, v in data["synonyms"].items()}
    return syn, frozenset(str(s).lower() for s in data.get("stopwords", []))


@dataclass
class Vocabulary:
    """Token synonym table and stopwords used for token-level expansion."""

    synonyms: dict[str, tuple[str, ...]] = field(default_factory=dict)
    stopwords: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def default(cls, extra: dict[str, str] | None = None) -> Vocabulary:
        syn, stop = _load_default()
        vocab = cls(synonyms=dict(syn), stopwords=stop)
        if extra:
            vocab.add(extra)
        return vocab

    def add(self, extra: dict[str, str]) -> None:
        for k, v in extra.items():
            self.synonyms[str(k).lower()] = tuple(str(v).lower().split())

    def expand(self, tokens: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        out: list[str] = []
        for tok in tokens:
            if tok in self.stopwords:
                continue
            out.extend(self.synonyms.get(tok, (tok,)))
        return tuple(out)

    def expand_name(self, raw: str) -> tuple[str, ...]:
        return self.expand(normalize_name(raw).tokens)
