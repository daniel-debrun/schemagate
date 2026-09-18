from __future__ import annotations

from schemagate.errors import ProviderResponseError
from schemagate.ingest.profile import ColumnProfile
from schemagate.llm.base import MappingRequest, ModelProvider
from schemagate.llm.prompts import PromptTemplate, load_prompt
from schemagate.llm.validation import validate_response
from schemagate.models import MappingProposal, Tier
from schemagate.resolve.base import ResolveContext

UNCAPPED = object()


class ModelResolver:
    """Sends only the still-unresolved columns to a provider; output is validated and capped."""

    name = "model"

    def __init__(self, provider: ModelProvider, prompt: PromptTemplate | None = None,
                 cap_override: float | object | None = None) -> None:
        self.provider = provider
        self.prompt = prompt or load_prompt()
        self._cap_override = cap_override

    def _cap(self, ctx: ResolveContext) -> float | None:
        if self._cap_override is UNCAPPED:
            return None
        if isinstance(self._cap_override, float):
            return min(self._cap_override, ctx.policy.model_cap)
        return ctx.policy.model_cap

    def resolve(self, ctx: ResolveContext, columns: list[ColumnProfile]) -> list[MappingProposal]:
        if not columns:
            return []
        request = MappingRequest.build(ctx.schema, columns, ctx.claimed, self.prompt)
        version = self.prompt.version
        try:
            response = self.provider.complete(request)
            validated = validate_response(response.text, request, self._cap(ctx))
            model_id = response.model_id
        except ProviderResponseError as exc:
            return [
                MappingProposal(
                    source_column=c.name, target_field=None, confidence=0.0, tier=Tier.UNRESOLVED,
                    rationale=f"model response rejected: {exc}", evidence={"provider_error": str(exc)},
                    proposer=f"model:{self.provider.model_id}", prompt_version=version,
                    model_id=self.provider.model_id,
                )
                for c in columns
            ]
        out = []
        for v in validated:
            out.append(MappingProposal(
                source_column=v.source_column,
                target_field=v.target_field,
                confidence=v.confidence if v.target_field else 0.0,
                tier=Tier.MODEL if v.target_field else Tier.UNRESOLVED,
                rationale=v.rationale,
                evidence={"cited": v.evidence, "raw_confidence": v.raw_confidence,
                          "validation_issues": v.issues},
                proposer=f"model:{model_id}",
                prompt_version=version,
                model_id=model_id,
            ))
        return out
