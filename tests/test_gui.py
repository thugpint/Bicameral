from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from bicameral import auth, config, install
from bicameral.gui import server as gui
from bicameral.store import Store


@pytest.fixture
def running(monkeypatch):
    monkeypatch.setattr(auth, "backends", lambda: [
        auth.Backend("claude-cli", "Claude Code (your Anthropic account)", "account", True, "signed in via oauth", ""),
        auth.Backend("codex-cli", "Codex CLI (your ChatGPT account)", "account", False, "not installed", "codex login", installed=False),
    ])
    monkeypatch.setattr(install, "state", lambda runner=None: install.InstallState(True, True, True))
    monkeypatch.setattr(install, "install_all", lambda runner=None: ["skill installed: x", "registered"])
    opened: list[list[str]] = []
    monkeypatch.setattr(gui, "open_in_terminal", lambda cmd: opened.append(cmd))
    monkeypatch.setattr(gui, "find_claude", lambda: "claude-fake")
    monkeypatch.setattr(gui, "find_codex", lambda: None)
    store = Store(":memory:")
    rid = store.create_run(task="fix it", workspace="x", architect_model="claude-code", editor_model="claude:sonnet", learning=1)
    store.finish_run(rid, success=1, task_kind="bugfix", steps_total=1, steps_ok=1, cost_usd=0.0, duration_s=1.0, summary="ok")
    store.add_step(rid, step_idx=0, title="fix median", kind="bugfix", model="claude:sonnet", role="editor", attempts=1,
                   accepted=1, verified=1, feedback="", cost_usd=0.0)
    store.record_routing("bugfix", "claude:sonnet", True)
    state = gui.GuiState(store=store, workspace=".")
    srv = gui.make_server(state)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path: str, body: dict | None = None, token: str | None = state.token):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method="POST" if body is not None else "GET")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Bicameral-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, json.loads(raw) if r.headers.get_content_type() == "application/json" else raw.decode()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield call, state, opened, rid
    srv.shutdown()
    srv.server_close()


def test_index_embeds_token_and_api_needs_it(running):
    call, state, _, _ = running
    status, html = call("/")
    assert status == 200 and state.token in html and "Sign in to Claude" in html
    assert call("/api/state", token=None)[0] == 401
    assert call("/api/state", token="wrong")[0] == 401


def test_state_reports_everything_the_page_needs(running):
    call, _, _, rid = running
    status, s = call("/api/state")
    assert status == 200
    assert s["install"] == {"skill": True, "mcp": True, "claude_found": True}
    assert [b["id"] for b in s["backends"]] == ["claude-cli", "codex-cli"]
    assert any(m["id"] == "claude:sonnet" and m["available"] for m in s["models"])
    assert all(not m["available"] for m in s["models"] if m["provider"] == "codex-cli")
    assert s["stats"]["runs"] == 1 and s["stats"]["on"] == {"ok": 1, "n": 1}
    assert s["stats"]["routing"][0]["kind"] == "bugfix"
    assert s["runs"][0]["id"] == rid and s["runs"][0]["success"] == 1
    _, d = call(f"/api/run/{rid}")
    assert d["steps"][0]["title"] == "fix median" and d["steps"][0]["accepted"] is True


def test_actions(running):
    call, state, opened, _ = running
    assert call("/api/login", {"which": "claude"})[1]["ok"] is True
    assert opened == [["claude-fake", "auth", "login"]]
    assert call("/api/login", {"which": "codex"})[1]["ok"] is False  # not installed
    assert call("/api/install", {})[1]["messages"] == ["skill installed: x", "registered"]
    assert call("/api/defaults", {"editor": "claude:sonnet"})[1]["ok"] is True
    assert config.load()["last_editor"] == "claude:sonnet"
    assert call("/api/run", {"task": "", "path": "."})[1]["ok"] is False
    assert call("/api/run", {"task": "x", "path": "does-not-exist", "architect": "a", "editor": "b"})[1]["ok"] is False
    assert call("/api/key", {"provider": "openai", "key": ""})[1]["ok"] is False
    assert call("/api/nope", {})[0] == 404
