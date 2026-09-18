from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class ConfidencePolicy(BaseModel):
    """Confidence assigned per resolution tier.

    The model cap must be strictly below every deterministic tier so that a model
    proposal can never outrank a mapping backed by a verified name or alias.
    """

    model_config = {"frozen": True}

    exact: float = Field(0.95, gt=0, le=1)
    dictionary: float = Field(0.90, gt=0, le=1)
    synonym: float = Field(0.85, gt=0, le=1)
    model_cap: float = Field(0.75, gt=0, le=1)
    compat_penalty: float = Field(0.15, ge=0, le=1)
    gate_threshold: float = Field(0.85, gt=0, le=1)

    @model_validator(mode="after")
    def _enforce_ordering(self) -> ConfidencePolicy:
        if not (self.exact >= self.dictionary >= self.synonym):
            raise ValueError("confidence tiers must satisfy exact >= dictionary >= synonym")
        if self.model_cap >= self.synonym:
            raise ValueError(
                f"model_cap ({self.model_cap}) must be strictly below every deterministic tier "
                f"(lowest is synonym={self.synonym})"
            )
        if self.gate_threshold <= self.model_cap:
            raise ValueError("gate_threshold must be above model_cap")
        return self


class ApprovalPolicy(BaseModel):
    """How many distinct, non-proposer humans must approve a spec version."""

    model_config = {"frozen": True}

    new_table_approvers: int = Field(1, ge=1)
    shared_table_approvers: int = Field(2, ge=1)
    allow_self_approval: bool = False
