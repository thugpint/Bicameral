"""Model catalog: what each signed-in backend can run right now.

Model ids carry an optional backend prefix:
- `claude:<model>`  headless Claude Code on the user's Anthropic account
- `codex:<model>`   Codex CLI on the user's ChatGPT account
- anything else     the Anthropic or OpenAI API, inferred from the id

Where a backend can tell us what the account can see, that list wins over the
static catalog:
- codex-cli reads the Codex CLI's own model cache (~/.codex/models_cache.json)
- anthropic / openai ask the API (`bicameral models --refresh`, or whenever a
  key is saved) and the ids are kept in config
Claude Code has no list command, so it gets the aliases the CLI accepts. Any
id the user types is accepted as-is; the backend says if it does not exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from . import config

BACKEND_PREFIXES = {"claude": "claude-cli", "codex": "codex-cli"}
ACCOUNT_PROVIDERS = ("claude-cli", "codex-cli")
API_PROVIDERS = ("anthropic", "openai")
PROVIDER_ORDER = ("claude-cli", "codex-cli", "anthropic", "openai")


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    id: str
    display: str
    input_per_m: float | None = None  # USD per 1M input tokens
    output_per_m: float | None = None  # USD per 1M output tokens
    supports_effort: bool = True
    note: str = ""

    @property
    def account_backed(self) -> bool:
        return self.provider in ACCOUNT_PROVIDERS


CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec("claude-cli", "claude:fable", "Claude Fable via Claude Code", 0.0, 0.0, True, "your Anthropic account"),
    ModelSpec("claude-cli", "claude:opus", "Claude Opus via Claude Code", 0.0, 0.0, True, "your Anthropic account"),
    ModelSpec("claude-cli", "claude:sonnet", "Claude Sonnet via Claude Code", 0.0, 0.0, True, "your Anthropic account"),
    ModelSpec("claude-cli", "claude:haiku", "Claude Haiku via Claude Code", 0.0, 0.0, True, "your Anthropic account"),
    ModelSpec("codex-cli", "codex:gpt-5-codex", "GPT-5 Codex via Codex CLI", 0.0, 0.0, True, "your ChatGPT account"),
    ModelSpec("codex-cli", "codex:gpt-5", "GPT-5 via Codex CLI", 0.0, 0.0, True, "your ChatGPT account"),
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

_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5", "gpt-6")


def split_backend(model_id: str) -> tuple[str | None, str]:
    """('claude-cli', 'sonnet') for 'claude:sonnet'; (None, id) for API ids."""
    if ":" in model_id:
        prefix, rest = model_id.split(":", 1)
        if prefix in BACKEND_PREFIXES:
            return BACKEND_PREFIXES[prefix], rest
    return None, model_id


def infer_provider(model_id: str) -> str:
    backend, bare = split_backend(model_id)
    if backend:
        return backend
    return "anthropic" if bare.startswith("claude") else "openai"


def _infer_effort(provider: str, model_id: str) -> bool:
    _, bare = split_backend(model_id)
    if provider in ("anthropic", "claude-cli"):
        return "haiku" not in bare
    if provider == "codex-cli":
        return True
    return bare.startswith(_OPENAI_REASONING_PREFIXES)


def discovered() -> dict[str, list[tuple[str, str]]]:
    """(id, display) per provider, for the backends that can list what the account sees."""
    out: dict[str, list[tuple[str, str]]] = {}
    from .providers.codex_cli import cached_models  # lazy: providers imports this module

    codex = [(f"codex:{slug}", f"{name} via Codex CLI") for slug, name in cached_models()]
    if codex:
        out["codex-cli"] = codex
    extra = config.load().get("extra_models") or {}
    for provider, ids in extra.items():
        if ids:
            out[provider] = [(mid, mid) for mid in ids]
    return out


def all_models() -> list[ModelSpec]:
    """Per provider: the discovered list if there is one, else the static catalog."""
    by_id = {m.id: m for m in CATALOG}
    found = discovered()
    out: list[ModelSpec] = []
    for provider in PROVIDER_ORDER:
        if provider in found:
            for mid, display in found[provider]:
                spec = by_id.get(mid)
                if spec is not None:
                    out.append(spec)
                else:
                    price = (0.0, 0.0) if provider in ACCOUNT_PROVIDERS else (None, None)
                    note = "your account" if provider in ACCOUNT_PROVIDERS else "your key"
                    out.append(ModelSpec(provider, mid, display, price[0], price[1], _infer_effort(provider, mid), note))
        else:
            out.extend(m for m in CATALOG if m.provider == provider)
    return out


def remember(provider: str, ids: list[str]) -> None:
    """Keep an API provider's live model list so it shows without asking again."""
    extra = dict(config.load().get("extra_models") or {})
    extra[provider] = sorted(ids)
    config.update(extra_models=extra)


def refresh(providers: Mapping[str, object]) -> dict[str, int]:
    """Ask every API backend what the key can see and store it. Returns id counts per provider.

    Raises ProviderError from the first backend that fails.
    """
    counts: dict[str, int] = {}
    for name in API_PROVIDERS:
        provider = providers.get(name)
        if provider is None:
            continue
        ids = list(provider.list_models())  # type: ignore[attr-defined]
        remember(name, ids)
        counts[name] = len(ids)
    return counts


def find(model_id: str) -> ModelSpec:
    for m in all_models():
        if m.id == model_id:
            return m
    for m in CATALOG:
        if m.id == model_id:
            return m
    provider = infer_provider(model_id)
    price = (0.0, 0.0) if provider in ACCOUNT_PROVIDERS else (None, None)
    note = "your account" if provider in ACCOUNT_PROVIDERS else "unknown"
    return ModelSpec(provider, model_id, model_id, price[0], price[1], _infer_effort(provider, model_id), note)


def estimate_cost(spec: ModelSpec, input_tokens: int, output_tokens: int) -> float | None:
    if spec.input_per_m is None or spec.output_per_m is None:
        return None
    return (input_tokens * spec.input_per_m + output_tokens * spec.output_per_m) / 1_000_000
