"""Model catalog with pricing and capability flags.

The static catalog is a starting point; `bicameral models --refresh` pulls the
live model list from each provider and stores extra ids in config. Unknown ids
still work: provider and capabilities are inferred from the id prefix.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    id: str
    display: str
    input_per_m: float | None = None  # USD per 1M input tokens
    output_per_m: float | None = None  # USD per 1M output tokens
    supports_effort: bool = True
    note: str = ""


CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec("anthropic", "claude-fable-5-1", "Claude Fable 5.1", 10.0, 50.0, True, "strongest reasoning"),
    ModelSpec("anthropic", "claude-opus-5", "Claude Opus 5", 5.0, 25.0, True, "frontier reasoning + coding"),
    ModelSpec("anthropic", "claude-sonnet-5", "Claude Sonnet 5", 2.0, 10.0, True, "fast, strong coding"),
    ModelSpec("anthropic", "claude-haiku-4-5", "Claude Haiku 4.5", 1.0, 5.0, False, "cheap and fast"),
    ModelSpec("openai", "gpt-5", "GPT-5", 1.25, 10.0, True, "frontier reasoning"),
    ModelSpec("openai", "gpt-5-codex", "GPT-5 Codex", 1.25, 10.0, True, "agentic coding"),
    ModelSpec("openai", "gpt-5-mini", "GPT-5 mini", 0.25, 2.0, True, "cheap reasoning"),
    ModelSpec("openai", "o3", "o3", 2.0, 8.0, True, "reasoning"),
    ModelSpec("openai", "o4-mini", "o4-mini", 1.1, 4.4, True, "cheap reasoning"),
)

_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def infer_provider(model_id: str) -> str:
    return "anthropic" if model_id.startswith("claude") else "openai"


def _infer_effort(provider: str, model_id: str) -> bool:
    if provider == "anthropic":
        return "haiku" not in model_id
    return model_id.startswith(_OPENAI_REASONING_PREFIXES)


def all_models() -> list[ModelSpec]:
    """Static catalog plus any ids discovered through `models --refresh`."""
    out = list(CATALOG)
    known = {m.id for m in out}
    extra = config.load().get("extra_models") or {}
    for provider, ids in extra.items():
        for mid in ids:
            if mid not in known:
                out.append(ModelSpec(provider, mid, mid, None, None, _infer_effort(provider, mid), "discovered"))
                known.add(mid)
    return out


def find(model_id: str) -> ModelSpec:
    for m in all_models():
        if m.id == model_id:
            return m
    provider = infer_provider(model_id)
    return ModelSpec(provider, model_id, model_id, None, None, _infer_effort(provider, model_id), "unknown")


def estimate_cost(spec: ModelSpec, input_tokens: int, output_tokens: int) -> float | None:
    if spec.input_per_m is None or spec.output_per_m is None:
        return None
    return (input_tokens * spec.input_per_m + output_tokens * spec.output_per_m) / 1_000_000
