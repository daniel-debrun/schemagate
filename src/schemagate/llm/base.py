from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from schemagate.ingest.profile import ColumnProfile
from schemagate.llm.prompts import PromptTemplate, load_prompt
from schemagate.schema.target import FieldSpec, TargetSchema


@dataclass(frozen=True)
class FieldBrief:
    name: str
    type: str
    description: str
    unit: str | None
    allowed_values: list[str] | None
    pattern: str | None
    aliases: list[str]

    @classmethod
    def from_spec(cls, f: FieldSpec) -> FieldBrief:
        return cls(f.name, f.type, f.description, f.unit, f.allowed_values, f.pattern, list(f.aliases))


@dataclass(frozen=True)
class ColumnBrief:
    name: str
    inferred_type: str
    null_rate: float
    distinct_count: int
    sample_values: list[str]
    pattern_signatures: dict[str, float]
    parse_rates: dict[str, float]

    @classmethod
    def from_profile(cls, p: ColumnProfile) -> ColumnBrief:
        return cls(p.name, p.inferred_type, p.null_rate, p.distinct_count, list(p.sample_values),
                   dict(p.pattern_signatures), dict(p.parse_rates))


@dataclass
class MappingRequest:
    schema_name: str
    fields: list[FieldBrief]
    columns: list[ColumnBrief]
    already_mapped: dict[str, str] = field(default_factory=dict)
    prompt: PromptTemplate = field(default_factory=load_prompt)

    @classmethod
    def build(cls, schema: TargetSchema, columns: list[ColumnProfile],
              already_mapped: dict[str, str] | None = None,
              prompt: PromptTemplate | None = None) -> MappingRequest:
        return cls(
            schema_name=schema.name,
            fields=[FieldBrief.from_spec(f) for f in schema.fields],
            columns=[ColumnBrief.from_profile(c) for c in columns],
            already_mapped=dict(already_mapped or {}),
            prompt=prompt or load_prompt(),
        )

    @property
    def system_prompt(self) -> str:
        return self.prompt.system

    @property
    def user_prompt(self) -> str:
        return self.prompt.render_user(
            schema_name=self.schema_name,
            fields_json=json.dumps([asdict(f) for f in self.fields], indent=1),
            already_mapped_json=json.dumps(self.already_mapped, indent=1),
            columns_json=json.dumps([asdict(c) for c in self.columns], indent=1),
        )


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    model_id: str
    raw: Any = None


class ModelProvider(Protocol):
    """Anything that turns a MappingRequest into raw text that should contain the JSON response."""

    model_id: str

    def complete(self, request: MappingRequest) -> ProviderResponse: ...
