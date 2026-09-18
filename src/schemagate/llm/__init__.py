from schemagate.llm.base import (
    ColumnBrief,
    FieldBrief,
    MappingRequest,
    ModelProvider,
    ProviderResponse,
)
from schemagate.llm.fake import FakeProvider
from schemagate.llm.heuristic import HeuristicProvider
from schemagate.llm.prompts import PromptTemplate, load_prompt
from schemagate.llm.validation import ValidatedMapping, validate_response


def get_provider(name: str, model_id: str | None = None) -> ModelProvider:
    if name == "heuristic":
        return HeuristicProvider()
    if name == "anthropic":
        from schemagate.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(model_id=model_id)
    if name == "openai":
        from schemagate.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(model_id=model_id)
    raise ValueError(f"unknown provider {name!r} (expected heuristic, anthropic, openai)")


__all__ = [
    "ColumnBrief",
    "FakeProvider",
    "FieldBrief",
    "HeuristicProvider",
    "MappingRequest",
    "ModelProvider",
    "PromptTemplate",
    "ProviderResponse",
    "ValidatedMapping",
    "get_provider",
    "load_prompt",
    "validate_response",
]
