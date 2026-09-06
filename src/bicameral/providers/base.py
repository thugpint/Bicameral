from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ProviderError(RuntimeError):
    """Raised for any provider-side failure (auth, rate limit, refusal, truncation)."""


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class Provider(Protocol):
    name: str

    def complete(
        self,
        model: str,
        system: str,
        user: str,
        *,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "response",
        effort: str | None = None,
        max_tokens: int = 16000,
    ) -> Completion: ...

    def list_models(self) -> list[str]: ...
