from __future__ import annotations

import os

from schemagate.errors import ProviderResponseError
from schemagate.llm.base import MappingRequest, ProviderResponse

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"


class AnthropicProvider:
    """Claude via the official anthropic SDK (install the 'anthropic' extra)."""

    def __init__(self, model_id: str | None = None, max_tokens: int = 16000, client=None) -> None:
        self.model_id = model_id or os.environ.get("SCHEMAGATE_ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
        self.max_tokens = max_tokens
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ImportError(
                    "AnthropicProvider requires: pip install 'schemadriftgate[anthropic]'"
                ) from exc
            client = anthropic.Anthropic()
        self._client = client

    def complete(self, request: MappingRequest) -> ProviderResponse:
        message = self._client.messages.create(
            model=self.model_id,
            max_tokens=self.max_tokens,
            system=request.system_prompt,
            messages=[{"role": "user", "content": request.user_prompt}],
        )
        if message.stop_reason == "refusal":
            raise ProviderResponseError("model declined the request (stop_reason=refusal)")
        if message.stop_reason == "max_tokens":
            raise ProviderResponseError("model response truncated at max_tokens")
        text = "".join(block.text for block in message.content if block.type == "text")
        return ProviderResponse(text=text, model_id=getattr(message, "model", self.model_id), raw=message)
