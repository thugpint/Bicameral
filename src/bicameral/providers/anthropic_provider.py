from __future__ import annotations

import time
from typing import Any

from .base import Completion, ProviderError


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str):
        import anthropic

        self._sdk = anthropic
        self.client = anthropic.Anthropic(api_key=api_key)

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
    ) -> Completion:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        output_config: dict[str, Any] = {}
        if effort:
            output_config["effort"] = effort
        if json_schema:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if output_config:
            kwargs["output_config"] = output_config

        sdk = self._sdk
        t0 = time.perf_counter()
        try:
            resp = self.client.messages.create(**kwargs)
        except sdk.AuthenticationError as e:
            raise ProviderError(f"anthropic: invalid API key ({e.message})") from e
        except sdk.NotFoundError as e:
            raise ProviderError(f"anthropic: unknown model '{model}' ({e.message})") from e
        except sdk.RateLimitError as e:
            raise ProviderError(f"anthropic: rate limited ({e.message})") from e
        except sdk.APIStatusError as e:
            raise ProviderError(f"anthropic: API error {e.status_code}: {e.message}") from e
        except sdk.APIConnectionError as e:
            raise ProviderError(f"anthropic: connection error: {e}") from e
        latency = int((time.perf_counter() - t0) * 1000)

        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            why = getattr(details, "explanation", None) or "no explanation"
            raise ProviderError(f"anthropic: model refused the request ({why})")
        if resp.stop_reason == "max_tokens":
            raise ProviderError(f"anthropic: response truncated at max_tokens={max_tokens}")

        text = "".join(b.text for b in resp.content if b.type == "text")
        return Completion(
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            latency_ms=latency,
        )

    def list_models(self) -> list[str]:
        try:
            return sorted(m.id for m in self.client.models.list())
        except self._sdk.AuthenticationError as e:
            raise ProviderError(f"anthropic: invalid API key ({e.message})") from e
        except self._sdk.APIError as e:
            raise ProviderError(f"anthropic: {e}") from e
