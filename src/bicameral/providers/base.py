from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """Raised for any provider-side failure (auth, rate limit, refusal, truncation)."""


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: float | None = None  # provider-reported cost, if it knows better than the catalog


@dataclass
class AgentEditResult:
    """Outcome of letting an agentic backend edit the workspace directly."""

    summary: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float | None = None


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


@runtime_checkable
class InPlaceEditor(Protocol):
    """Backends that can edit files in a workspace themselves (Claude Code, Codex CLI)."""

    def edit_in_place(self, model: str, root: Path, prompt: str, effort: str | None = None) -> AgentEditResult: ...
