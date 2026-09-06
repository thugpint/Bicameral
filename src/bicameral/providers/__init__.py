"""Provider registry and the model-agnostic LLM facade used by the engine."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .. import credentials, models
from .base import AgentEditResult, Completion, InPlaceEditor, Provider, ProviderError

__all__ = [
    "AgentEditResult", "Completion", "InPlaceEditor", "Provider", "ProviderError",
    "LLM", "CallStats", "build_providers", "build_provider", "parse_json",
]


@dataclass
class CallStats:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: int


def build_providers() -> dict[str, Provider]:
    """Instantiate every backend that can run right now.

    API providers need a key (or, for Anthropic, an `ant auth login` OAuth
    profile). CLI backends need the executable and a signed-in account.
    """
    from ..auth import has_anthropic_profile, status_claude_cli, status_codex_cli

    out: dict[str, Provider] = {}
    key = credentials.get_key("anthropic")
    if key or has_anthropic_profile():
        from .anthropic_provider import AnthropicProvider

        out["anthropic"] = AnthropicProvider(key)
    key = credentials.get_key("openai")
    if key:
        from .openai_provider import OpenAIProvider

        out["openai"] = OpenAIProvider(key)
    if status_claude_cli().available:
        from .claude_cli import ClaudeCliProvider

        out["claude-cli"] = ClaudeCliProvider()
    if status_codex_cli().available:
        from .codex_cli import CodexCliProvider

        out["codex-cli"] = CodexCliProvider()
    return out


def build_provider(name: str, api_key: str | None = None) -> Provider:
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key)
    if name == "openai":
        from .openai_provider import OpenAIProvider

        if not api_key:
            raise ProviderError("openai: an API key is required")
        return OpenAIProvider(api_key)
    if name == "claude-cli":
        from .claude_cli import ClaudeCliProvider

        return ClaudeCliProvider()
    if name == "codex-cli":
        from .codex_cli import CodexCliProvider

        return CodexCliProvider()
    raise ProviderError(f"unknown provider '{name}'")


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(text: str) -> dict[str, Any]:
    cleaned = _FENCE.sub("", text.strip()).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ProviderError("model returned no JSON object")
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as e:
            raise ProviderError(f"model returned malformed JSON: {e}") from e
    if not isinstance(data, dict):
        raise ProviderError("model returned JSON that is not an object")
    return data


class LLM:
    """Routes a completion to the right backend for a model id and parses JSON output."""

    def __init__(self, providers: Mapping[str, Provider]):
        self.providers = dict(providers)

    def available(self) -> list[str]:
        return sorted(self.providers)

    def provider_for(self, model_id: str) -> Provider:
        spec = models.find(model_id)
        provider = self.providers.get(spec.provider)
        if provider is None:
            hint = {
                "anthropic": "bicameral login anthropic",
                "openai": "bicameral login openai",
                "claude-cli": "claude auth login",
                "codex-cli": "codex login",
            }.get(spec.provider, "")
            raise ProviderError(f"backend '{spec.provider}' is not available for {model_id}; sign in with: {hint}")
        return provider

    def can_edit_in_place(self, model_id: str) -> bool:
        return isinstance(self.provider_for(model_id), InPlaceEditor)

    def complete_json(
        self,
        model_id: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        effort: str | None = None,
        max_tokens: int = 16000,
    ) -> tuple[dict[str, Any], CallStats]:
        spec = models.find(model_id)
        provider = self.provider_for(model_id)
        eff = effort if spec.supports_effort else None
        completion = provider.complete(
            model_id, system, user, json_schema=schema, schema_name=schema_name, effort=eff, max_tokens=max_tokens
        )
        try:
            data = parse_json(completion.text)
        except ProviderError:
            retry_user = user + "\n\nYour previous reply was not valid JSON. Reply with a single JSON object only."
            completion2 = provider.complete(
                model_id, system, retry_user, json_schema=schema, schema_name=schema_name, effort=eff, max_tokens=max_tokens
            )
            data = parse_json(completion2.text)
            completion = Completion(
                completion2.text,
                completion.input_tokens + completion2.input_tokens,
                completion.output_tokens + completion2.output_tokens,
                completion.latency_ms + completion2.latency_ms,
                _add(completion.cost_usd, completion2.cost_usd),
            )
        cost = completion.cost_usd
        if cost is None:
            cost = models.estimate_cost(spec, completion.input_tokens, completion.output_tokens)
        return data, CallStats(model_id, completion.input_tokens, completion.output_tokens, cost, completion.latency_ms)

    def edit_in_place(self, model_id: str, root, prompt: str, effort: str | None = None) -> tuple[AgentEditResult, CallStats]:
        spec = models.find(model_id)
        provider = self.provider_for(model_id)
        if not isinstance(provider, InPlaceEditor):
            raise ProviderError(f"{model_id} cannot edit files directly")
        res = provider.edit_in_place(model_id, root, prompt, effort if spec.supports_effort else None)
        cost = res.cost_usd if res.cost_usd is not None else models.estimate_cost(spec, res.input_tokens, res.output_tokens)
        return res, CallStats(model_id, res.input_tokens, res.output_tokens, cost, res.latency_ms)


def _add(a: float | None, b: float | None) -> float | None:
    if a is None and b is None:
        return None
    return (a or 0.0) + (b or 0.0)
