from __future__ import annotations

import os

from schemagate.errors import ProviderResponseError
from schemagate.llm.base import MappingRequest, ProviderResponse

DEFAULT_OPENAI_MODEL = "gpt-4.1"


class OpenAIProvider:
    """OpenAI chat completions (install the 'openai' extra)."""

    def __init__(self, model_id: str | None = None, client=None) -> None:
        self.model_id = model_id or os.environ.get("SCHEMAGATE_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
        if client is None:
            try:
                import openai
            except ImportError as exc:
                raise ImportError("OpenAIProvider requires: pip install 'schemadriftgate[openai]'") from exc
            client = openai.OpenAI()
        self._client = client

    def complete(self, request: MappingRequest) -> ProviderResponse:
        resp = self._client.chat.completions.create(
            model=self.model_id,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
        )
        choice = resp.choices[0]
        if choice.message.content is None:
            raise ProviderResponseError(f"empty response (finish_reason={choice.finish_reason})")
        return ProviderResponse(text=choice.message.content, model_id=resp.model, raw=resp)
