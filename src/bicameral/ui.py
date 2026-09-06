"""Terminal helpers: model picker and result formatting. No third-party UI deps."""

from __future__ import annotations

import sys

from . import config, models
from .orchestrator import RunResult


def _price(m: models.ModelSpec) -> str:
    if m.account_backed:
        return " your account "
    if m.input_per_m is None:
        return "  price n/a  "
    return f"${m.input_per_m:>5.2f}/${m.output_per_m:<6.2f}"


def list_models(available: list[str]) -> list[models.ModelSpec]:
    specs = [m for m in models.all_models() if m.provider in available]
    for i, m in enumerate(specs, 1):
        print(f"  {i:>2}. {m.id:<28} {m.provider:<10} {_price(m)} per 1M  {m.note}")
    return specs


def _resolve(answer: str, specs: list[models.ModelSpec], default: str | None) -> str | None:
    answer = answer.strip()
    if not answer:
        return default
    if answer.isdigit() and 1 <= int(answer) <= len(specs):
        return specs[int(answer) - 1].id
    for m in specs:
        if m.id == answer:
            return m.id
    if answer.startswith(("claude", "codex", "gpt", "o1", "o3", "o4")):
        return answer  # allow ids we have not catalogued
    return None


def pick_models(available: list[str]) -> tuple[str, str]:
    """Interactively choose the architect (reasoning) and editor (coding) models."""
    if not available:
        raise SystemExit("no backends available; sign in with `bicameral login claude|codex|anthropic|openai` or open the TUI")
    cfg = config.load()
    print("Available models:")
    specs = list_models(available)
    ids = [m.id for m in specs]

    def default_for(key: str, prefer: tuple[str, ...]) -> str:
        last = cfg.get(key)
        if last and (last in ids or last.startswith(("claude", "gpt", "o"))):
            return last
        for p in prefer:
            if p in ids:
                return p
        return ids[0]

    arch_default = default_for("last_architect", ("claude:opus", "claude-opus-5", "claude-fable-5-1", "gpt-5", "o3"))
    edit_default = default_for("last_editor", ("codex:gpt-5-codex", "claude:sonnet", "claude-sonnet-5", "gpt-5-codex", "gpt-5"))

    while True:
        arch = _resolve(input(f"Architect model (plans + reviews) [{arch_default}]: "), specs, arch_default)
        if arch:
            break
        print("  pick a number or a model id")
    while True:
        edit = _resolve(input(f"Editor model (writes the diffs) [{edit_default}]: "), specs, edit_default)
        if edit:
            break
        print("  pick a number or a model id")
    config.update(last_architect=arch, last_editor=edit)
    return arch, edit


def resolve_models(architect: str | None, editor: str | None, available: list[str]) -> tuple[str, str]:
    """Use explicit flags, else the interactive picker on a TTY, else the last-used pair."""
    if architect and editor:
        config.update(last_architect=architect, last_editor=editor)
        return architect, editor
    if sys.stdin.isatty():
        picked = pick_models(available)
        return architect or picked[0], editor or picked[1]
    cfg = config.load()
    arch = architect or cfg.get("last_architect")
    edit = editor or cfg.get("last_editor")
    if not arch or not edit:
        raise SystemExit("no models chosen; pass --architect and --editor (no TTY for the picker)")
    return arch, edit


def print_result(result: RunResult) -> None:
    print()
    print("=" * 60)
    print(f"{'SUCCESS' if result.success else 'FAILED'}  [{result.task_kind}] {result.plan_summary}")
    for s in result.steps:
        mark = "ok " if s.ok else "err"
        ver = "verified" if s.verified else ("unverified" if s.ok else "")
        print(f"  {mark} step {s.idx + 1}: {s.title}  <- {s.role} ({s.model}), {s.attempts} attempt(s) {ver}")
        if not s.ok and s.feedback:
            print(f"      {s.feedback[:300]}")
    if result.failure_reason and not result.success:
        print(f"reason: {result.failure_reason}")
    if result.verify_command:
        print(f"verification: {result.verify_command} -> {'pass' if result.final_verify_ok else 'FAIL'}")
    print(f"cost: ${result.cost_usd:.4f}  tokens: {result.input_tokens} in / {result.output_tokens} out  time: {result.duration_s:.1f}s")
    if result.lessons_learned:
        print("lessons stored for next time:")
        for text in result.lessons_learned:
            print(f"  - {text}")
    print("=" * 60)
