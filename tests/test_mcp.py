"""The MCP tool surface, driven the way the /bicameral skill drives it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bicameral.mcp_server import ARCHITECT_ID, Bicameral, LessonInput, StepInput
from bicameral.providers import LLM, AgentEditResult
from bicameral.router import Router
from bicameral.store import Store
from fake import EDITOR, FakeProvider, critique, edits, lessons, reject
from test_orchestrator import BUGGY, FIXED, VERIFY

STEP = StepInput(id=1, title="Fix median for even-length input", description="average the two middle values",
                 kind="bugfix", files=["stats.py"], acceptance="test_median_even passes", suggested_role="editor")


@pytest.fixture(autouse=True)
def follow_architect_suggestion(monkeypatch):
    """The bandit explores on a fresh install; these tests need deterministic delegation."""
    monkeypatch.setattr(Router, "SUGGESTION_PRIOR", 1000.0)


def _run_id(text: str) -> int:
    m = re.search(r"run_id: (\d+)", text)
    assert m, text
    return int(m.group(1))


def make(scripts, provider_key="openai"):
    fp = FakeProvider(scripts)
    llm = LLM({provider_key: fp})
    store = Store(":memory:")
    return Bicameral(store=store, llm_factory=lambda: llm), fp, store


def test_full_delegated_flow(median_repo):
    app, fp, store = make({"edit": [edits([("stats.py", BUGGY, FIXED)])]})

    status = app.status()
    assert "Editor backends" in status
    assert "none yet" in app.recall("fix median")

    out = app.begin("fix median", str(median_repo), EDITOR, "bugfix", "fix median", [STEP], VERIFY)
    run_id = _run_id(out)
    assert "baseline: FAILING" in out and f"delegate to {EDITOR}" in out

    out = app.execute(run_id, 1)
    assert "+    mid = len(s) // 2" in out and "Verification: PASS" in out
    assert "MUST pass" in out  # last step with failing baseline

    out = app.review(run_id, 1, "accept")
    assert "accepted (verified)" in out and "bicameral_finish" in out

    out = app.finish(run_id, True, [LessonInput(lesson="name the failing test in bugfix steps", applies_to=["bugfix"], role="architect")])
    assert out.startswith(f"run {run_id}: SUCCESS")
    assert "final verification: pass" in out

    run = store.runs()[0]
    assert run["success"] == 1 and run["architect_model"] == ARCHITECT_ID and run["editor_model"] == EDITOR
    assert store.routing_stats("bugfix", EDITOR) == (1, 0)
    assert [l.text for l in store.lessons()] == ["name the failing test in bugfix steps"]
    assert len(store.examples()) == 1
    assert app.sessions == {}
    assert "learning on: 1/1" in app.stats()


def test_reject_rolls_back_and_retries_with_feedback(median_repo):
    original = (median_repo / "stats.py").read_text("utf-8")
    app, fp, store = make({"edit": [edits([("stats.py", BUGGY, FIXED)])]})
    run_id = _run_id(app.begin("fix median", str(median_repo), EDITOR, "bugfix", "s", [STEP], VERIFY))
    app.execute(run_id, 1)
    out = app.review(run_id, 1, "reject", "use statistics.median semantics")
    assert "rolled back" in out and "2 attempt(s) left" in out
    assert (median_repo / "stats.py").read_text("utf-8") == original

    out = app.execute(run_id, 1)
    assert "attempt 2/3" in out
    assert "use statistics.median semantics" in fp.calls_for("edit")[1][2]
    assert "accepted" in app.review(run_id, 1, "accept")


def test_accept_refused_when_required_verification_fails(median_repo):
    no_op = edits([("stats.py", "Tiny statistics helpers.", "Tiny statistics helpers")])
    app, fp, store = make({"edit": [no_op]})
    run_id = _run_id(app.begin("fix median", str(median_repo), EDITOR, "bugfix", "s", [STEP], VERIFY))
    out = app.execute(run_id, 1)
    assert "Verification: FAIL" in out
    assert app.review(run_id, 1, "accept").startswith("refused")
    out = app.review(run_id, 1, "reject", "tests still fail")
    assert "rolled back" in out
    assert "Tiny statistics helpers." in (median_repo / "stats.py").read_text("utf-8")


def test_step_fails_after_attempt_limit_and_finish_reports_failure(median_repo):
    blocked = {"status": "blocked", "files_needed": [], "explanation": "cannot", "edits": [], "new_files": []}
    app, fp, store = make({"edit": [blocked]})
    run_id = _run_id(app.begin("fix median", str(median_repo), EDITOR, "bugfix", "s", [STEP], VERIFY))
    out = app.execute(run_id, 1)
    assert "FAILED" in out and "bicameral_finish" in out
    out = app.finish(run_id, False)
    assert "FAILED" in out and store.runs()[0]["success"] == 0
    assert store.routing_stats("bugfix", EDITOR) == (0, 1)


def test_self_routed_step_uses_check(median_repo):
    app, fp, store = make({})
    run_id = _run_id(app.begin("fix median", str(median_repo), "self", "bugfix", "s", [STEP], VERIFY))
    out = app.execute(run_id, 1)
    assert "routed to YOU" in out and "bicameral_check" in out
    assert "no files changed" in app.check(run_id, 1)
    p = median_repo / "stats.py"
    p.write_text(p.read_text("utf-8").replace(BUGGY, FIXED), "utf-8")
    out = app.check(run_id, 1)
    assert "edited by you" in out and "+    mid = len(s) // 2" in out and "Verification: PASS" in out
    assert "accepted (verified)" in app.review(run_id, 1, "accept")
    assert store.routing_stats("bugfix", ARCHITECT_ID) == (1, 0)
    assert "SUCCESS" in app.finish(run_id, True)


class FakeAgent(FakeProvider):
    """An in-place editing backend (like Claude Code / Codex) that just fixes the bug."""

    def edit_in_place(self, model, root: Path, prompt: str, effort=None) -> AgentEditResult:
        self.calls.append(("agent", model, prompt))
        p = root / "stats.py"
        p.write_text(p.read_text("utf-8").replace(BUGGY, FIXED), "utf-8")
        (root / "scratch.txt").write_text("notes", "utf-8")
        return AgentEditResult(summary="fixed median; left a note", cost_usd=0.0)


def test_in_place_backend_diff_and_rollback(median_repo):
    fp = FakeAgent({})
    app = Bicameral(store=Store(":memory:"), llm_factory=lambda: LLM({"codex-cli": fp}))
    run_id = _run_id(app.begin("fix median", str(median_repo), "codex:gpt-5-codex", "bugfix", "s", [STEP], VERIFY))
    out = app.execute(run_id, 1)
    assert "delegated to codex:gpt-5-codex" in out
    assert "files: scratch.txt, stats.py" in out and "editor summary: fixed median" in out
    assert "Acceptance criterion" in fp.calls[0][2] and "editing files in place" in fp.calls[0][2]
    app.review(run_id, 1, "reject", "do not leave scratch files")
    assert not (median_repo / "scratch.txt").exists()
    assert BUGGY in (median_repo / "stats.py").read_text("utf-8")


def test_begin_validates_inputs(tmp_path, median_repo):
    app, fp, store = make({})
    assert app.begin("t", str(tmp_path / "nope"), EDITOR, "bugfix", "s", [STEP]).startswith("error: workspace")
    assert app.begin("t", str(median_repo), EDITOR, "bugfix", "s", []).startswith("error: a plan")
    assert "not available" in app.begin("t", str(median_repo), "claude:sonnet", "bugfix", "s", [STEP])
    assert app.execute(999, 1).startswith("error: unknown run_id")


def test_finish_rolls_back_unreviewed_edit(median_repo):
    original = (median_repo / "stats.py").read_text("utf-8")
    app, fp, store = make({"edit": [edits([("stats.py", BUGGY, FIXED)])]})
    run_id = _run_id(app.begin("fix median", str(median_repo), EDITOR, "bugfix", "s", [STEP], VERIFY))
    app.execute(run_id, 1)
    assert FIXED in (median_repo / "stats.py").read_text("utf-8")
    out = app.finish(run_id, True)
    assert "FAILED" in out  # no accepted steps and final verification fails after rollback
    assert (median_repo / "stats.py").read_text("utf-8") == original


def test_mcp_server_exposes_tools():
    from bicameral.mcp_server import build_server

    server = build_server()
    import asyncio

    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert {"bicameral_status", "bicameral_recall", "bicameral_critique", "bicameral_begin", "bicameral_execute",
            "bicameral_check", "bicameral_review", "bicameral_finish", "bicameral_stats"} <= names


MINE = StepInput(id=1, title="Fix median for even-length input", description="average the two middle values",
                 kind="bugfix", files=["stats.py"], acceptance="test_median_even passes", suggested_role="architect")


def test_critique_asks_the_editor_before_begin(median_repo):
    app, fp, store = make({"critique": [critique((1, "step does not say which test fails", "name test_median_even"))]})
    out = app.critique("fix median", str(median_repo), EDITOR, "bugfix", "s", [STEP], VERIFY)
    assert "1 concern(s)" in out and "which test fails" in out and "bicameral_begin" in out
    assert fp.calls_for("critique")[0][1] == EDITOR
    assert "stats.py" in fp.calls_for("critique")[0][2]  # it saw the files the plan touches
    assert store.runs() == []  # nothing recorded yet

    app2, fp2, _ = make({"critique": [critique(assessment="clear and small")]})
    assert "no concerns" in app2.critique("fix median", str(median_repo), EDITOR, "bugfix", "s", [STEP], VERIFY)
    assert "no editor model" in app2.critique("fix median", str(median_repo), "self", "bugfix", "s", [STEP], VERIFY)


def test_self_authored_step_gets_editor_second_opinion(median_repo):
    app, fp, store = make({"review": [reject("even-length branch still wrong")]})
    run_id = _run_id(app.begin("fix median", str(median_repo), EDITOR, "bugfix", "s", [MINE], VERIFY))
    assert "routed to YOU" in app.execute(run_id, 1)
    p = median_repo / "stats.py"
    p.write_text(p.read_text("utf-8").replace(BUGGY, FIXED), "utf-8")
    out = app.check(run_id, 1)
    assert f"Second opinion from {EDITOR}: REJECT: even-length branch still wrong" in out
    assert "final call" in out
    assert fp.calls_for("review")[0][1] == EDITOR and "+    mid = len(s) // 2" in fp.calls_for("review")[0][2]
    assert "accepted (verified)" in app.review(run_id, 1, "accept")  # the architect still decides


def test_delegated_step_shows_editor_concerns(median_repo):
    app, fp, store = make({"edit": [edits([("stats.py", BUGGY, FIXED)], concerns="the acceptance criterion names a test that does not exist")]})
    run_id = _run_id(app.begin("fix median", str(median_repo), EDITOR, "bugfix", "s", [STEP], VERIFY))
    out = app.execute(run_id, 1)
    assert "EDITOR CONCERNS" in out and "does not exist" in out
    assert "Second opinion" not in out  # the architect reviews delegated steps itself


def test_pinned_step_routing_is_reported(median_repo, monkeypatch):
    monkeypatch.setattr(Router, "SUGGESTION_PRIOR", 0.0)
    pinned = StepInput(**{**STEP.model_dump(), "pin": True})
    app, fp, store = make({})
    out = app.begin("fix median", str(median_repo), EDITOR, "bugfix", "s", [pinned], VERIFY)
    assert f"delegate to {EDITOR}  (pinned by the architect)" in out


def test_orphaned_runs_are_marked_interrupted(median_repo):
    app, fp, store = make({})
    stale = store.create_run(task="old", workspace=".", architect_model=ARCHITECT_ID, editor_model="self", learning=1)
    live = _run_id(app.begin("fix median", str(median_repo), "self", "bugfix", "s", [STEP], VERIFY))
    app.status()
    rows = {r["id"]: r for r in store.runs()}
    assert rows[stale]["summary"] == "interrupted" and rows[stale]["success"] is None
    assert rows[live]["summary"] is None  # a run this server still owns is left alone


def test_finish_stores_editor_lessons_too(median_repo):
    app, fp, store = make({"edit": [edits([("stats.py", BUGGY, FIXED)])], "reflect": [lessons("ask for the test file up front")]})
    run_id = _run_id(app.begin("fix median", str(median_repo), EDITOR, "bugfix", "s", [STEP], VERIFY))
    app.execute(run_id, 1)
    app.review(run_id, 1, "accept")
    out = app.finish(run_id, True, [LessonInput(lesson="architect lesson", applies_to=["bugfix"], role="architect")])
    assert f"lessons from {EDITOR}: ask for the test file up front" in out
    assert fp.calls_for("reflect")[0][1] == EDITOR
    assert sorted(l.text for l in store.lessons()) == ["architect lesson", "ask for the test file up front"]
