"""Claude Code and Codex CLI backends, exercised through a fake subprocess runner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bicameral.providers import ProviderError
from bicameral.providers.claude_cli import ClaudeCliProvider, auth_status
from bicameral.providers.codex_cli import CodexCliProvider


class Recorder:
    def __init__(self, stdout: str = "", returncode: int = 0, on_call=None):
        self.stdout, self.returncode, self.on_call = stdout, returncode, on_call
        self.calls: list[dict] = []

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        if self.on_call:
            self.on_call(args, kwargs)
        return subprocess.CompletedProcess(args, self.returncode, stdout=self.stdout, stderr="")


def _envelope(**extra) -> str:
    base = {"type": "result", "is_error": False, "result": "done", "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 20}}
    base.update(extra)
    return json.dumps(base)


def test_claude_complete_uses_stdin_schema_and_no_tools():
    rec = Recorder(_envelope(structured_output={"verdict": "accept", "feedback": "", "issues": []}))
    p = ClaudeCliProvider(executable="claude-fake", runner=rec)
    c = p.complete("claude:sonnet", "SYS", "USER PROMPT", json_schema={"type": "object"}, effort="high")
    args, call = rec.calls[0]["args"], rec.calls[0]
    assert args[0] == "claude-fake" and "-p" in args
    assert call["input"] == "USER PROMPT"
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--system-prompt") + 1] == "SYS"
    assert json.loads(args[args.index("--json-schema") + 1]) == {"type": "object"}
    assert args[args.index("--effort") + 1] == "high"
    assert "CLAUDECODE" not in call["env"]
    assert json.loads(c.text) == {"verdict": "accept", "feedback": "", "issues": []}
    assert c.input_tokens == 100 and c.output_tokens == 20 and c.cost_usd == pytest.approx(0.0123)


def test_claude_complete_falls_back_to_result_text():
    rec = Recorder(_envelope(result='{"a": 1}'))
    c = ClaudeCliProvider(executable="x", runner=rec).complete("claude:opus", "", "hi")
    assert c.text == '{"a": 1}'


def test_claude_error_envelope_raises():
    rec = Recorder(_envelope(is_error=True, result="Failed to authenticate: OAuth session expired"))
    with pytest.raises(ProviderError, match="OAuth session expired"):
        ClaudeCliProvider(executable="x", runner=rec).complete("claude:opus", "", "hi")


def test_claude_non_json_output_raises():
    rec = Recorder("garbage", returncode=1)
    with pytest.raises(ProviderError, match="exited 1"):
        ClaudeCliProvider(executable="x", runner=rec).complete("claude:opus", "", "hi")


def test_claude_edit_in_place_runs_agentically_in_workspace(tmp_path):
    rec = Recorder(_envelope(result="changed stats.py"))
    res = ClaudeCliProvider(executable="x", runner=rec).edit_in_place("claude:sonnet", tmp_path, "PROMPT", effort="medium")
    args, call = rec.calls[0]["args"], rec.calls[0]
    assert call["cwd"] == str(tmp_path) and call["input"] == "PROMPT"
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    assert "--allowedTools" in args and "Edit" in args and "Write" in args
    assert "--tools" not in args
    assert res.summary == "changed stats.py" and res.cost_usd == pytest.approx(0.0123)


def test_claude_auth_status_parses_login():
    rec = Recorder(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
    assert auth_status(executable="x", runner=rec) == {"installed": True, "loggedIn": True, "authMethod": "claude.ai"}


def _codex_writer(text: str, seen: dict | None = None):
    def on_call(args, kwargs):
        out = Path(args[args.index("-o") + 1])
        out.write_text(text, "utf-8")
        if seen is not None and "--output-schema" in args:
            seen["schema"] = json.loads(Path(args[args.index("--output-schema") + 1]).read_text("utf-8"))
    return on_call


def test_codex_complete_writes_schema_and_reads_last_message():
    seen: dict = {}
    rec = Recorder(on_call=_codex_writer('{"lessons": []}', seen))
    p = CodexCliProvider(executable="codex-fake", runner=rec)
    c = p.complete("codex:gpt-5-codex", "SYS", "USER", json_schema={"type": "object"}, effort="low")
    args, call = rec.calls[0]["args"], rec.calls[0]
    assert args[:2] == ["codex-fake", "exec"] and "--skip-git-repo-check" in args
    assert args[args.index("-m") + 1] == "gpt-5-codex"
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert seen["schema"] == {"type": "object"}
    assert 'model_reasoning_effort="low"' in args
    assert call["input"].startswith("SYS\n\nUSER")
    assert c.text == '{"lessons": []}'


def test_codex_edit_in_place_uses_the_workspace_write_sandbox(tmp_path):
    rec = Recorder(on_call=_codex_writer("edited"))
    res = CodexCliProvider(executable="x", runner=rec).edit_in_place("codex:gpt-5", tmp_path, "PROMPT")
    args, call = rec.calls[0]["args"], rec.calls[0]
    assert args[args.index("--sandbox") + 1] == "workspace-write" and "--full-auto" not in args
    assert args[args.index("-C") + 1] == str(tmp_path)
    assert call["cwd"] == str(tmp_path) and call["input"] == "PROMPT"
    assert res.summary == "edited"


def test_codex_nonzero_exit_raises():
    rec = Recorder(returncode=2)
    with pytest.raises(ProviderError, match="exited 2"):
        CodexCliProvider(executable="x", runner=rec).complete("codex:gpt-5", "", "hi")


def test_missing_executables_raise(monkeypatch):
    import bicameral.providers.claude_cli as cl
    import bicameral.providers.codex_cli as cc

    monkeypatch.setattr(cl, "find_executable", lambda: None)
    monkeypatch.setattr(cc, "find_executable", lambda: None)
    with pytest.raises(ProviderError, match="not on PATH"):
        ClaudeCliProvider()
    with pytest.raises(ProviderError, match="not on PATH"):
        CodexCliProvider()
    assert cc.auth_status()["installed"] is False
