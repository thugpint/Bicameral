from __future__ import annotations

import time
from typing import Any

from .base import Completion, ProviderError

_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4")
_EXCLUDE_MARKERS = ("realtime", "audio", "tts", "transcribe", "image", "embedding", "moderation", "search-preview")


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str):
        import openai

        self._sdk = openai
        self.client = openai.OpenAI(api_key=api_key)

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
            "instructions": system,
            "input": user,
            "max_output_tokens": max_tokens,
        }
        if effort:
            # OpenAI accepts low/medium/high; clamp anything stronger to high.
            kwargs["reasoning"] = {"effort": effort if effort in ("low", "medium", "high") else "high"}
        if json_schema:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": json_schema,
                    "strict": True,
                }
            }

        sdk = self._sdk
        t0 = time.perf_counter()
        try:
            resp = self.client.responses.create(**kwargs)
        except sdk.AuthenticationError as e:
            raise ProviderError(f"openai: invalid API key ({e})") from e
        except sdk.NotFoundError as e:
            raise ProviderError(f"openai: unknown model '{model}' ({e})") from e
        except sdk.RateLimitError as e:
            raise ProviderError(f"openai: rate limited ({e})") from e
        except sdk.APIStatusError as e:
            raise ProviderError(f"openai: API error {e.status_code}: {e}") from e
        except sdk.APIConnectionError as e:
            raise ProviderError(f"openai: connection error: {e}") from e
        latency = int((time.perf_counter() - t0) * 1000)

        if getattr(resp, "status", "completed") == "incomplete":
            reason = getattr(getattr(resp, "incomplete_details", None), "reason", "unknown")
            raise ProviderError(f"openai: incomplete response ({reason})")

        usage = resp.usage
        return Completion(
            text=resp.output_text or "",
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            latency_ms=latency,
        )

    def list_models(self) -> list[str]:
        try:
            ids = [m.id for m in self.client.models.list()]
        except self._sdk.AuthenticationError as e:
            raise ProviderError(f"openai: invalid API key ({e})") from e
        except self._sdk.APIError as e:
            raise ProviderError(f"openai: {e}") from e
        return sorted(
            m for m in ids
            if m.startswith(_CHAT_PREFIXES) and not any(x in m for x in _EXCLUDE_MARKERS)
        )
