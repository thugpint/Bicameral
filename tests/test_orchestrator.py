from __future__ import annotations

import pytest

from bicameral.orchestrator import Orchestrator, RunConfig
from bicameral.workspace import Workspace
from fake import ARCHITECT, EDITOR, accept, critique, edits, fake_llm, lessons, plan, reject, step

VERIFY = "{python} -m pytest -q"
BUGGY = "    return s[len(s) // 2]"
FIXED = (
    "    mid = len(s) // 2\n"
    "    if len(s) % 2:\n"
    "        return s[mid]\n"
    "    return (s[mid - 1] + s[mid]) / 2"
)
GOOD_EDIT = edits([("stats.py", BUGGY, FIXED)])
BAD_EDIT = edits([("stats.py", "this text is not in the file", FIXED)])
PLAN = plan([step("Fix median for even-length input", ["stats.py"])], verify=VERIFY)


def make(repo, scripts, learning=True, attempts=3):
    llm, fp = fake_llm(scripts)
    store_ = __import__("bicameral.store", fromlist=["Store"]).Store(":memory:")
    cfg = RunConfig(ARCHITECT, EDITOR, learning=learning, max_attempts=attempts)
    return Orchestrator(llm, store_, Workspace(repo), cfg), fp, store_


def test_happy_path_records_everything(median_repo):
    orch, fp, store = make(
        median_repo,
        {"plan": [PLAN], "edit": [GOOD_EDIT], "review": [accept()],
         "reflect": [lessons("keep bugfix steps to one file"), lessons("Keep bugfix steps to one file")]},  # both minds, same lesson
    )
    result = orch.run("median is wrong for even-length lists")

    assert result.success and result.final_verify_ok
    assert result.steps[0].verified and result.steps[0].attempts == 1
    chosen = result.steps[0].model  # with no routing data the bandit may explore either arm
    assert chosen in (ARCHITECT, EDITOR)
    assert result.lessons_learned == ["keep bugfix steps to one file"]
    assert "(s[mid - 1] + s[mid]) / 2" in (median_repo / "stats.py").read_text("utf-8")

    # the editor critiques the plan first, and both minds reflect at the end
    assert [c[0] for c in fp.calls] == ["plan", "critique", "edit", "review", "reflect", "reflect"]
    assert fp.calls_for("plan")[0][1] == ARCHITECT
    assert fp.calls_for("critique")[0][1] == EDITOR
    assert fp.calls_for("review")[0][1] == (EDITOR if chosen == ARCHITECT else ARCHITECT)  # whoever did not write it
    assert [c[1] for c in fp.calls_for("reflect")] == [ARCHITECT, EDITOR]
    assert "stats.py" in fp.calls_for("edit")[0][2]  # file contents were provided
    assert "+    mid = len(s) // 2" in fp.calls_for("review")[0][2]  # reviewer saw the diff

    run = store.runs()[0]
    assert run["success"] == 1 and run["steps_ok"] == 1 and run["task_kind"] == "bugfix"
    assert store.routing_stats("bugfix", chosen) == (1, 0)
    assert len(store.lessons()) == 1
    assert len(store.examples()) == 1


def test_unapplicable_edit_is_fed_back_and_retried(median_repo):
    orch, fp, _ = make(median_repo, {"plan": [PLAN], "edit": [BAD_EDIT, GOOD_EDIT], "review": [accept()], "reflect": [{"lessons": []}]})
    result = orch.run("fix median")
    assert result.success and result.steps[0].attempts == 2
    second_prompt = fp.calls_for("edit")[1][2]
    assert "could not be applied" in second_prompt and "not found" in second_prompt


def test_reviewer_reject_rolls_back_then_retry_succeeds(median_repo):
    original = (median_repo / "stats.py").read_text("utf-8")
    seen: list[str] = []

    def review(user: str):
        seen.append(user)
        return reject("median must average the two middle values, not return one of them") if len(seen) == 1 else accept()

    orch, fp, store = make(median_repo, {"plan": [PLAN], "edit": [GOOD_EDIT], "review": review, "reflect": [{"lessons": []}]})
    result = orch.run("fix median")
    assert result.success and result.steps[0].attempts == 2
    assert "average the two middle values" in fp.calls_for("edit")[1][2]
    assert (median_repo / "stats.py").read_text("utf-8") != original


def test_reviewer_reject_restores_file_when_all_attempts_fail(median_repo):
    original = (median_repo / "stats.py").read_text("utf-8")
    orch, _, store = make(
        median_repo, {"plan": [PLAN], "edit": [GOOD_EDIT], "review": [reject("no")], "reflect": [{"lessons": []}]}, attempts=2
    )
    result = orch.run("fix median")
    assert not result.success
    assert result.steps[0].attempts == 2
    assert "failed after 2 attempt" in result.failure_reason
    assert (median_repo / "stats.py").read_text("utf-8") == original
    assert store.routing_stats("bugfix", result.steps[0].model) == (0, 1)
    assert store.runs()[0]["success"] == 0


def test_verification_failure_is_enforced_on_last_step(median_repo):
    no_op = edits([("stats.py", "Tiny statistics helpers.", "Tiny statistics helpers")])
    orch, fp, _ = make(median_repo, {"plan": [PLAN], "edit": [no_op, GOOD_EDIT], "review": [accept()], "reflect": [{"lessons": []}]})
    result = orch.run("fix median")
    assert result.success and result.steps[0].attempts == 2
    assert "verification command" in fp.calls_for("edit")[1][2]


def test_learning_off_skips_memory_examples_and_reflection(median_repo):
    orch, fp, store = make(median_repo, {"plan": [PLAN], "edit": [GOOD_EDIT], "review": [accept()]}, learning=False)
    result = orch.run("fix median")
    assert result.success
    assert [c[0] for c in fp.calls] == ["plan", "critique", "edit", "review"]
    assert store.lessons() == [] and store.examples() == []


def test_lessons_and_examples_are_injected_next_run(median_repo, tmp_path):
    from bicameral.store import Store

    llm, fp = fake_llm({"plan": [PLAN], "edit": [GOOD_EDIT], "review": [accept()], "reflect": [lessons("always read the test file first")]})
    store = Store(":memory:")
    cfg = RunConfig(ARCHITECT, EDITOR)
    assert Orchestrator(llm, store, Workspace(median_repo), cfg).run("fix median for even length").success

    import shutil

    repo2 = tmp_path / "repo2"
    shutil.copytree(median_repo.parent / "repo", repo2)
    (repo2 / "stats.py").write_text((repo2 / "stats.py").read_text("utf-8").replace(FIXED, BUGGY), "utf-8")
    fp.calls.clear()
    assert Orchestrator(llm, store, Workspace(repo2), cfg).run("fix median for even length again").success
    assert "always read the test file first" in fp.calls_for("plan")[0][2]
    edit_prompt = fp.calls_for("edit")[0][2]
    assert "Examples of accepted edits" in edit_prompt and "+    mid = len(s) // 2" in edit_prompt
    lesson = store.lessons()[0]
    assert lesson.uses == 1 and lesson.score == 1.0


def test_architect_can_request_files_before_planning(median_repo):
    calls = []

    def plan_fn(user: str):
        calls.append(user)
        if len(calls) == 1:
            return {**PLAN, "status": "need_files", "files_needed": ["tests/test_stats.py"]}
        return PLAN

    orch, fp, _ = make(median_repo, {"plan": plan_fn, "edit": [GOOD_EDIT], "review": [accept()], "reflect": [{"lessons": []}]})
    assert orch.run("fix median").success
    assert len(calls) == 2 and "test_median_even" in calls[1]


def test_blocked_step_fails_fast(median_repo):
    blocked = {"status": "blocked", "files_needed": [], "explanation": "file is generated", "edits": [], "new_files": []}
    orch, fp, _ = make(median_repo, {"plan": [PLAN], "edit": [blocked], "review": [accept()], "reflect": [{"lessons": []}]})
    result = orch.run("fix median")
    assert not result.success and result.steps[0].attempts == 1
    assert "blocked" in result.failure_reason


def test_empty_plan_raises(median_repo):
    from bicameral.orchestrator import OrchestratorError

    orch, _, store = make(median_repo, {"plan": [plan([])]})
    with pytest.raises(OrchestratorError):
        orch.run("fix median")
    assert store.runs()[0]["success"] == 0


def test_editor_critique_triggers_one_replan(median_repo):
    vague = plan([step("Fix it", ["stats.py"])], verify=VERIFY, summary="vague")
    orch, fp, _ = make(
        median_repo,
        {"plan": [vague, PLAN], "critique": [critique((1, "does not name the failing test", "mention test_median_even"))],
         "edit": [GOOD_EDIT], "review": [accept()]},
        learning=False,
    )
    assert orch.run("fix median").success
    plans = fp.calls_for("plan")
    assert len(plans) == 2
    assert "critique of your previous plan" in plans[1][2] and "does not name the failing test" in plans[1][2]
    assert fp.calls_for("critique")[0][1] == EDITOR


def test_architect_authored_step_is_reviewed_by_editor(median_repo, monkeypatch):
    from bicameral.router import Router

    monkeypatch.setattr(Router, "SUGGESTION_PRIOR", 1000.0)
    mine = plan([step("Fix median", ["stats.py"], role="architect")], verify=VERIFY)
    orch, fp, _ = make(median_repo, {"plan": [mine], "edit": [GOOD_EDIT], "review": [accept()]}, learning=False)
    result = orch.run("fix median")
    assert result.success and result.steps[0].role == "architect"
    assert fp.calls_for("edit")[0][1] == ARCHITECT
    assert fp.calls_for("review")[0][1] == EDITOR


def test_pinned_step_is_never_rerouted(median_repo, monkeypatch):
    from bicameral.router import Router

    monkeypatch.setattr(Router, "SUGGESTION_PRIOR", 0.0)  # maximal exploration
    pinned = plan([step("Fix median", ["stats.py"], role="editor", pin=True)], verify=VERIFY)
    for _ in range(5):
        orch, fp, _ = make(median_repo, {"plan": [pinned], "edit": [GOOD_EDIT], "review": [accept()]}, learning=False)
        result = orch.run("fix median")
        assert result.success and result.steps[0].model == EDITOR
        p = median_repo / "stats.py"
        p.write_text(p.read_text("utf-8").replace(FIXED, BUGGY), "utf-8")
