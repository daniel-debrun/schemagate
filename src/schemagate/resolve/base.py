from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from schemagate.config import ConfidencePolicy
from schemagate.ingest.profile import ColumnProfile
from schemagate.models import MappingProposal
from schemagate.schema.normalize import normalize_name
from schemagate.schema.target import TargetSchema
from schemagate.schema.vocabulary import Vocabulary


@dataclass
class ResolveContext:
    schema: TargetSchema
    policy: ConfidencePolicy = field(default_factory=ConfidencePolicy)
    vocabulary: Vocabulary | None = None
    claimed: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.vocabulary is None:
            self.vocabulary = self.schema.vocabulary()

    @property
    def vocab(self) -> Vocabulary:
        assert self.vocabulary is not None
        return self.vocabulary


class Resolver(Protocol):
    name: str

    def resolve(
        self, ctx: ResolveContext, columns: list[ColumnProfile]
    ) -> list[MappingProposal]:
        """Return proposals for the columns this resolver can map; omit the rest."""
        ...


def norm(raw: str) -> str:
    return normalize_name(raw).snake
