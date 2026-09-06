from __future__ import annotations

import asyncio
import subprocess

from bicameral import install
from bicameral.store import Store


def test_skill_install_and_uninstall(tmp_path):
    target = install.install_skill(tmp_path)
    assert target == tmp_path / "bicameral" / "SKILL.md"
    text = target.read_text("utf-8")
    assert text.startswith("---\nname: bicameral") and "bicameral_begin" in text and "AskUserQuestion" in text
    assert install.uninstall_skill(tmp_path) is True
    assert not target.exists()
    assert install.uninstall_skill(tmp_path) is False


def test_mcp_registration_command(monkeypatch):
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        if args[1:3] == ["mcp", "get"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")
        return subprocess.CompletedProcess(args, 0, stdout="Added stdio MCP server bicameral", stderr="")

    monkeypatch.setattr(install, "find_executable", lambda: "claude-fake")
    out = install.install_mcp(runner=runner)
    assert "Added" in out
    add = [c for c in calls if c[1:3] == ["mcp", "add"]][0]
    assert add[3:6] == ["-s", "user", "bicameral"]
    assert add[6] == "--" and add[-2:] == ["-m", "bicameral.mcp_server"]


def test_mcp_registration_without_claude_gives_manual_instructions(monkeypatch):
    monkeypatch.setattr(install, "find_executable", lambda: None)
    assert "claude mcp add -s user bicameral" in install.install_mcp()
    assert install.state().claude_found is False


def test_tui_mounts_and_switches_tabs(monkeypatch):
    from textual.widgets import DataTable, TabbedContent

    from bicameral import auth
    from bicameral.tui.app import BicameralApp

    monkeypatch.setattr(auth, "backends", lambda: [
        auth.Backend("codex-cli", "Codex CLI (your ChatGPT account)", "account", True, "signed in via chatgpt", ""),
        auth.Backend("claude-cli", "Claude Code (your Anthropic account)", "account", False, "not signed in", "claude auth login"),
    ])
    monkeypatch.setattr(install, "state", lambda runner=None: install.InstallState(True, False, True))
    store = Store(":memory:")
    rid = store.create_run(task="fix it", workspace="x", architect_model="claude-code", editor_model="codex:gpt-5-codex", learning=1)
    store.finish_run(rid, success=1, task_kind="bugfix", steps_total=1, steps_ok=1, cost_usd=0.0, duration_s=1.0, summary="ok")
    store.record_routing("bugfix", "codex:gpt-5-codex", True)

    async def drive():
        app = BicameralApp(store=store, workspace=".")
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.pause(0.5)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.theme == "bicameral"
            assert app.query_one("#backends", DataTable).row_count == 2
            assert app.query_one("#routing", DataTable).row_count == 1
            for key, tab in (("2", "setup"), ("3", "models"), ("4", "run"), ("5", "evals"), ("6", "runs"), ("7", "lessons"), ("1", "overview")):
                await pilot.press(key)
                await pilot.pause()
                assert app.query_one(TabbedContent).active == tab
            assert app.query_one("#runs-table", DataTable).row_count == 1

    asyncio.run(drive())
