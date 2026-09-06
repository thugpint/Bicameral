"""A scripted provider so the orchestrator can be tested without network access."""

from __future__ import annotations

import json
from typing import Any, Callable

from bicameral.providers import LLM, Completion

Script = list[dict[str, Any]] | Callable[[str], dict[str, Any]]

ARCHITECT = "claude-test-architect"  # resolves to the anthropic provider slot
EDITOR = "gpt-test-editor"  # resolves to the openai provider slot


class FakeProvider:
    name = "fake"

    def __init__(self, scripts: dict[str, Script]):
        self.scripts = scripts
        self.calls: list[tuple[str, str, str]] = []  # (schema_name, model, user prompt)

    DEFAULTS: dict[str, dict[str, Any]] = {
        "critique": {"assessment": "sound", "concerns": []},
        "reflect": {"lessons": []},
    }

    def complete(self, model, system, user, *, json_schema=None, schema_name="response", effort=None, max_tokens=16000):
        self.calls.append((schema_name, model, user))
        handler = self.scripts.get(schema_name)
        if handler is None:
            if schema_name not in self.DEFAULTS:
                raise KeyError(schema_name)
            return Completion(json.dumps(self.DEFAULTS[schema_name]), 100, 50, 1)
        if callable(handler):
            payload = handler(user)
        else:
            payload = handler.pop(0) if len(handler) > 1 else handler[0]
        return Completion(json.dumps(payload), 100, 50, 1)

    def list_models(self):
        return [ARCHITECT, EDITOR]

    def calls_for(self, schema_name: str) -> list[tuple[str, str, str]]:
        return [c for c in self.calls if c[0] == schema_name]


def fake_llm(scripts: dict[str, Script]) -> tuple[LLM, FakeProvider]:
    fp = FakeProvider(scripts)
    return LLM({"anthropic": fp, "openai": fp}), fp


def plan(steps: list[dict[str, Any]], kind: str = "bugfix", verify: str = "", summary: str = "do the thing") -> dict[str, Any]:
    return {"status": "plan", "files_needed": [], "task_kind": kind, "summary": summary, "verify_command": verify, "steps": steps}


def step(title: str, files: list[str], kind: str = "bugfix", role: str = "editor", acceptance: str = "tests pass", pin: bool = False) -> dict[str, Any]:
    return {
        "id": 1, "title": title, "description": title, "kind": kind, "files": files,
        "acceptance": acceptance, "suggested_role": role, "rationale": "", "pin": pin,
    }


def critique(*concerns: tuple[int, str, str], assessment: str = "") -> dict[str, Any]:
    return {"assessment": assessment, "concerns": [{"step": s, "issue": i, "suggestion": g} for s, i, g in concerns]}


def edits(pairs: list[tuple[str, str, str]], new_files: list[tuple[str, str]] | None = None, concerns: str = "") -> dict[str, Any]:
    return {
        "status": "edits", "files_needed": [], "concerns": concerns,
        "explanation": "",
        "edits": [{"path": p, "search": s, "replace": r} for p, s, r in pairs],
        "new_files": [{"path": p, "content": c} for p, c in (new_files or [])],
    }


def accept() -> dict[str, Any]:
    return {"verdict": "accept", "feedback": "", "issues": []}


def reject(feedback: str) -> dict[str, Any]:
    return {"verdict": "reject", "feedback": feedback, "issues": [feedback]}


def lessons(*texts: str) -> dict[str, Any]:
    return {"lessons": [{"lesson": t, "applies_to": ["bugfix"], "role": "editor", "confidence": 0.8} for t in texts]}
