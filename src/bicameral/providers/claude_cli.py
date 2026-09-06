"""Editor/architect backend that runs headless Claude Code on the user's Anthropic account.

`claude -p` reads the prompt from stdin, emits a JSON envelope with
`--output-format json`, and can enforce a schema with `--json-schema`. For
editing it runs agentically inside the workspace with file tools allowed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .base import AgentEditResult, Completion, ProviderError

EDIT_TOOLS = ["Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "LS"]
_NESTING_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def find_executable() -> str | None:
    return shutil.which("claude")


def clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for k in _NESTING_VARS:
        env.pop(k, None)
    return env


def bare_model(model: str) -> str:
    return model.split(":", 1)[1] if ":" in model else model


class ClaudeCliProvider:
    name = "claude-cli"

    def __init__(self, executable: str | None = None, runner=subprocess.run):
        self.exe = executable or find_executable()
        if not self.exe:
            raise ProviderError("claude-cli: the `claude` executable is not on PATH")
        self._run = runner

    # -- plumbing ------------------------------------------------------------

    def _invoke(self, args: list[str], stdin: str, cwd: Path | None, timeout: int) -> tuple[dict[str, Any], int]:
        t0 = time.perf_counter()
        try:
            proc = self._run(
                [self.exe, *args], input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(cwd) if cwd else None, env=clean_env(), timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise ProviderError(f"claude-cli: timed out after {timeout}s")
        except OSError as e:
            raise ProviderError(f"claude-cli: could not start `claude`: {e}")
        latency = int((time.perf_counter() - t0) * 1000)
        data = _parse_envelope(proc.stdout)
        if data is None:
            tail = (proc.stderr or proc.stdout or "").strip()[-800:]
            raise ProviderError(f"claude-cli: exited {proc.returncode} without a JSON result: {tail}")
        if data.get("is_error"):
            raise ProviderError(f"claude-cli: {data.get('result') or 'unknown error'}")
        return data, latency

    # -- Provider ------------------------------------------------------------

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
        args = ["-p", "--output-format", "json", "--no-session-persistence", "--tools", "", "--model", bare_model(model)]
        if system:
            args += ["--system-prompt", system]
        if json_schema:
            args += ["--json-schema", json.dumps(json_schema)]
        if effort:
            args += ["--effort", effort]
        data, latency = self._invoke(args, user, None, timeout=1800)
        structured = data.get("structured_output")
        text = json.dumps(structured) if isinstance(structured, (dict, list)) else str(data.get("result") or "")
        usage = data.get("usage") or {}
        return Completion(
            text=text,
            input_tokens=int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            latency_ms=latency,
            cost_usd=_float_or_none(data.get("total_cost_usd")),
        )

    def edit_in_place(self, model: str, root: Path, prompt: str, effort: str | None = None) -> AgentEditResult:
        args = [
            "-p", "--output-format", "json", "--no-session-persistence", "--model", bare_model(model),
            "--permission-mode", "acceptEdits",
        ]
        if effort:
            args += ["--effort", effort]
        args += ["--allowedTools", *EDIT_TOOLS]
        data, latency = self._invoke(args, prompt, root, timeout=3600)
        usage = data.get("usage") or {}
        return AgentEditResult(
            summary=str(data.get("result") or ""),
            input_tokens=int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            latency_ms=latency,
            cost_usd=_float_or_none(data.get("total_cost_usd")),
        )

    def list_models(self) -> list[str]:
        # Claude Code has no list command; these are the aliases `--model` accepts.
        # Any full model name (claude-sonnet-5, ...) works too, prefixed with `claude:`.
        return ["claude:fable", "claude:opus", "claude:sonnet", "claude:haiku"]


def auth_status(executable: str | None = None, runner=subprocess.run) -> dict[str, Any]:
    """Return Claude Code's own view of its login: {loggedIn, authMethod, ...}."""
    exe = executable or find_executable()
    if not exe:
        return {"installed": False, "loggedIn": False, "authMethod": "none"}
    try:
        proc = runner([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                      env=clean_env(), timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"installed": True, "loggedIn": False, "authMethod": "none", "error": str(e)}
    data = _parse_envelope(proc.stdout) or {}
    return {"installed": True, "loggedIn": bool(data.get("loggedIn")), "authMethod": data.get("authMethod", "none")}


def _parse_envelope(stdout: str) -> dict[str, Any] | None:
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _float_or_none(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
