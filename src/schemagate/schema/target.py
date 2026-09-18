from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from schemagate.errors import SchemaDefinitionError
from schemagate.schema.normalize import normalize_name
from schemagate.schema.vocabulary import Vocabulary

FieldType = Literal["string", "integer", "number", "date", "boolean"]
_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


class FieldSpec(BaseModel):
    name: str
    type: FieldType = "string"
    description: str = ""
    required: bool = False
    unit: str | None = None
    allowed_values: list[str] | None = None
    pattern: str | None = None
    aliases: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _ident(cls, v: str) -> str:
        if not _IDENT.match(v):
            raise ValueError(f"field name {v!r} must be lower snake_case")
        return v

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v


class TargetSchema(BaseModel):
    name: str
    table: str
    version: int = 1
    description: str = ""
    key: list[str] = Field(default_factory=list)
    published: bool = False
    fields: list[FieldSpec]
    synonyms: dict[str, str] = Field(default_factory=dict)

    @field_validator("table", "name")
    @classmethod
    def _ident(cls, v: str) -> str:
        if not _IDENT.match(v):
            raise ValueError(f"{v!r} must be lower snake_case")
        return v

    @model_validator(mode="after")
    def _check(self) -> TargetSchema:
        names = [f.name for f in self.fields]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate field names: {sorted(dupes)}")
        missing = [k for k in self.key if k not in names]
        if missing:
            raise ValueError(f"key fields not defined in schema: {missing}")
        seen: dict[str, str] = {}
        for f in self.fields:
            for alias in [f.name, *f.aliases]:
                norm = normalize_name(alias).snake
                if norm in seen and seen[norm] != f.name:
                    raise ValueError(
                        f"alias {alias!r} is ambiguous between {seen[norm]!r} and {f.name!r}"
                    )
                seen[norm] = f.name
        return self

    def field(self, name: str) -> FieldSpec:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def vocabulary(self) -> Vocabulary:
        return Vocabulary.default(self.synonyms)


def load_schema(path: str | Path) -> TargetSchema:
    try:
        data = yaml.safe_load(Path(path).read_text("utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SchemaDefinitionError(f"cannot read schema {path}: {exc}") from exc
    return parse_schema(data, source=str(path))


def parse_schema(data: object, source: str = "<dict>") -> TargetSchema:
    try:
        return TargetSchema.model_validate(data)
    except ValidationError as exc:
        raise SchemaDefinitionError(f"invalid schema {source}: {exc}") from exc
