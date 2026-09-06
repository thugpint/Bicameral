"""Local API-key storage.

Neither Anthropic nor OpenAI offer a public third-party OAuth flow, so "login"
means storing an API key locally. Keys are read from the environment first
(ANTHROPIC_API_KEY / OPENAI_API_KEY), then from ~/.bicameral/credentials.json.
"""

from __future__ import annotations

import json
import os
import stat

from .paths import credentials_path

PROVIDERS: tuple[str, ...] = ("anthropic", "openai")
ENV_VARS: dict[str, str] = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def _load() -> dict[str, str]:
    p = credentials_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
    except json.JSONDecodeError:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def _save(data: dict[str, str]) -> None:
    p = credentials_path()
    p.write_text(json.dumps(data, indent=2), "utf-8")
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def get_key(provider: str) -> str | None:
    env = os.environ.get(ENV_VARS.get(provider, ""))
    if env:
        return env
    return _load().get(provider)


def key_source(provider: str) -> str | None:
    if os.environ.get(ENV_VARS.get(provider, "")):
        return "env"
    if _load().get(provider):
        return "file"
    return None


def set_key(provider: str, key: str) -> None:
    data = _load()
    data[provider] = key.strip()
    _save(data)


def clear_key(provider: str) -> None:
    data = _load()
    data.pop(provider, None)
    _save(data)


def logged_in() -> list[str]:
    return [p for p in PROVIDERS if get_key(p)]
