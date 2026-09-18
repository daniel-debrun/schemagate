from __future__ import annotations

import json
from collections.abc import Callable

from schemagate.llm.base import MappingRequest, ProviderResponse


class FakeProvider:
    """Test double returning canned text, or text computed from the request."""

    def __init__(
        self,
        response: str | dict | Callable[[MappingRequest], str],
        model_id: str = "fake-model",
    ) -> None:
        self._response = response
        self.model_id = model_id
        self.requests: list[MappingRequest] = []

    def complete(self, request: MappingRequest) -> ProviderResponse:
        self.requests.append(request)
        if callable(self._response):
            text = self._response(request)
        elif isinstance(self._response, dict):
            text = json.dumps(self._response)
        else:
            text = self._response
        return ProviderResponse(text=text, model_id=self.model_id)
