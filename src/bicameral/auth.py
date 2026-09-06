"""Which backends can run right now, and how the user signs in to the ones that can't.

Four backends:
- claude-cli   headless Claude Code on the user's Anthropic account (`claude auth login`)
- codex-cli    OpenAI Codex CLI on the user's ChatGPT account (`codex login`)
- anthropic    Anthropic API with an API key or an `ant auth login` OAuth profile
- openai       OpenAI API with an API key
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import credentials
from .providers import claude_cli, codex_cli


@dataclass
class Backend:
    id: str
    label: str
    kind: str  # "account" or "api"
    available: bool
    detail: str
    login_hint: str
    installed: bool = True


def anthropic_profile_dir() -> Path:
    override = os.environ.get("ANTHROPIC_CONFIG_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "Anthropic"
    return Path.home() / ".config" / "anthropic"


def has_anthropic_profile() -> bool:
    cred_dir = anthropic_profile_dir() / "credentials"
    return cred_dir.is_dir() and any(cred_dir.glob("*.json"))


def status_claude_cli() -> Backend:
    s = claude_cli.auth_status()
    if not s.get("installed"):
        return Backend("claude-cli", "Claude Code (your Anthropic account)", "account", False,
                       "`claude` is not installed", "Install Claude Code, then run: claude auth login", installed=False)
    if s.get("loggedIn"):
        return Backend("claude-cli", "Claude Code (your Anthropic account)", "account", True,
                       f"signed in via {s.get('authMethod')}", "")
    return Backend("claude-cli", "Claude Code (your Anthropic account)", "account", False,
                   "installed but not signed in", "Run: claude auth login")


def status_codex_cli() -> Backend:
    s = codex_cli.auth_status()
    if not s.get("installed"):
        detail = "`codex` is not installed" + (" (a ChatGPT sign-in exists in ~/.codex)" if s.get("loggedIn") else "")
        return Backend("codex-cli", "Codex CLI (your ChatGPT account)", "account", False, detail,
                       "Install the Codex desktop app or run: npm i -g @openai/codex, then: codex login", installed=False)
    if s.get("loggedIn"):
        return Backend("codex-cli", "Codex CLI (your ChatGPT account)", "account", True,
                       f"signed in via {s.get('authMethod')}", "")
    return Backend("codex-cli", "Codex CLI (your ChatGPT account)", "account", False,
                   "installed but not signed in", "Run: codex login")


def status_anthropic_api() -> Backend:
    src = credentials.key_source("anthropic")
    if src:
        return Backend("anthropic", "Anthropic API", "api", True, f"API key from {src}", "")
    if has_anthropic_profile():
        return Backend("anthropic", "Anthropic API", "api", True, "OAuth profile (ant auth login)", "")
    return Backend("anthropic", "Anthropic API", "api", False, "no API key",
                   "bicameral login anthropic  (or `ant auth login` for an OAuth profile)")


def status_openai_api() -> Backend:
    src = credentials.key_source("openai")
    if src:
        return Backend("openai", "OpenAI API", "api", True, f"API key from {src}", "")
    return Backend("openai", "OpenAI API", "api", False, "no API key", "bicameral login openai")


def backends() -> list[Backend]:
    return [status_claude_cli(), status_codex_cli(), status_anthropic_api(), status_openai_api()]


def available_ids() -> list[str]:
    return [b.id for b in backends() if b.available]
