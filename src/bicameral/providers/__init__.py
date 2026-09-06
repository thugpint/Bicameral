"""Provider registry and the model-agnostic LLM facade used by the orchestrator."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .. import credentials, models
from .base import Completion, Provider, ProviderError

__all__ = ["Completion", "Provider", "ProviderError", "LLM", "CallStats", "build_providers", "parse_json"]


@dataclass
class CallStats:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: int


def build_providers() -> dict[str, Provider]:
    """Instantiate a provider for every service with stored credentials."""
    out: dict[str, Provider] = {}
    key = credentials.get_key("anthropic")
    if key:
        from .anthropic_provider import AnthropicProvider

        out["anthropic"] = AnthropicProvider(key)
    key = credentials.get_key("openai")
    if key:
        from .openai_provider import OpenAIProvider

        out["openai"] = OpenAIProvider(key)
    return out


def build_provider(name: str, api_key: str) -> Provider:
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key)
    if name == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(api_key)
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
    """Routes a completion to the right provider for a model id and parses JSON output."""

    def __init__(self, providers: Mapping[str, Provider]):
        self.providers = dict(providers)

    def available(self) -> list[str]:
        return sorted(self.providers)

    def provider_for(self, model_id: str) -> Provider:
        spec = models.find(model_id)
        provider = self.providers.get(spec.provider)
        if provider is None:
            raise ProviderError(
                f"no credentials for {spec.provider} (needed for {model_id}); run: bicameral login {spec.provider}"
            )
        return provider

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
            # One retry with an explicit reminder; structured outputs make this rare.
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
            )
        stats = CallStats(
            model=model_id,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cost_usd=models.estimate_cost(spec, completion.input_tokens, completion.output_tokens),
            latency_ms=completion.latency_ms,
        )
        return data, stats
