from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Tier(str, Enum):
    HUMAN = "human"
    EXACT = "exact"
    CARRYOVER = "carryover"
    DICTIONARY = "dictionary"
    SYNONYM = "synonym"
    RENAME = "rename"
    MODEL = "model"
    UNRESOLVED = "unresolved"

    @property
    def rank(self) -> int:
        """Lower rank wins ties. Deterministic evidence always ranks above the model."""
        return _TIER_RANK[self]

    @property
    def deterministic(self) -> bool:
        return self in (Tier.EXACT, Tier.DICTIONARY, Tier.SYNONYM, Tier.CARRYOVER, Tier.HUMAN)


_TIER_RANK = {
    Tier.HUMAN: 0,
    Tier.EXACT: 1,
    Tier.CARRYOVER: 2,
    Tier.DICTIONARY: 3,
    Tier.SYNONYM: 4,
    Tier.RENAME: 5,
    Tier.MODEL: 6,
    Tier.UNRESOLVED: 7,
}


@dataclass
class MappingProposal:
    source_column: str
    target_field: str | None
    confidence: float
    tier: Tier
    rationale: str
    evidence: dict[str, Any] = field(default_factory=dict)
    proposer: str = ""
    prompt_version: str | None = None
    model_id: str | None = None
    extension_column: str | None = None
    conflict: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tier"] = self.tier.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MappingProposal:
        d = dict(d)
        d["tier"] = Tier(d["tier"])
        return cls(**d)

    @property
    def output_column(self) -> str | None:
        return self.target_field or self.extension_column
