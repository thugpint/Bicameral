"""Git-backed checkpoints and commits, deterministic gates, TDD guards, disputes, repo lessons, review mode."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from bicameral import gitops, paths
from bicameral.cli import main
from bicameral.mcp_server import ARCHITECT_ID, Bicameral, LessonInput, StepInput
from bicameral.memory import RepoLessons
from bicameral.providers import LLM
from bicameral.router import Router
from bicameral.store import Store
from fake import EDITOR, FakeProvider, edits, reject
from test_orchestrator import BUGGY, FIXED, VERIFY

STEP = StepInput(id=1, title="Fix median for even-length input", description="average the two middle values",
                 kind="bugfix", files=["stats.py"], acceptance="test_median_even passes", suggested_role="architect")


@pytest.fixture(autouse=True)
def follow_suggestion(monkeypatch):
    monkeypatch.setattr(Router, "SUGGESTION_PRIOR", 1000.0)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def git_repo(median_repo: Path) -> Path:
    _git(median_repo, "init", "-q", "-b", "main")
    _git(median_repo, "config", "user.name", "tester")
    _git(median_repo, "config", "user.email", "tester@example.com")
    _git(median_repo, "add", "-A")
    _git(median_repo, "commit", "-q", "-m", "fixture")
    return median_repo


def _run_id(text: str) -> int:
    m = re.search(r"run_id: (\d+)", text)
    assert m, text
    return int(m.group(1))


def make(scripts, store=None):
    fp = FakeProvider(scripts)
    llm = LLM({"openai": fp})
    store = store or Store(":memory:")
    return Bicameral(store=store, llm_factory=lambda: llm), fp, store


def fix(repo: Path) -> None:
    p = repo / "stats.py"
    p.write_text(p.read_text("utf-8").replace(BUGGY, FIXED), "utf-8")


# -- gitops ----------------------------------------------------------------------


def test_checkpoint_and_restore_cover_new_and_changed_files(git_repo):
    _git(git_repo, "add", "-N", ".")  # the user's own index state must survive untouched
    index_before = _git(git_repo, "ls-files", "-s")
    sha = gitops.checkpoint(git_repo, "refs/bicameral/test", "before")
    fix(git_repo)
    (git_repo / "junk.txt").write_text("x", "utf-8")
    gitops.restore(git_repo, sha)
    assert BUGGY in (git_repo / "stats.py").read_text("utf-8")
    assert not (git_repo / "junk.txt").exists()
    assert _git(git_repo, "ls-files", "-s") == index_before
    assert _git(git_repo, "rev-parse", "refs/bicameral/test") == sha
    assert ".git_disabled" not in {p.name for p in git_repo.iterdir()}


def test_diff_includes_untracked_files_and_hunk_ranges_parse(git_repo):
    fix(git_repo)
    (git_repo / "new.py").write_text("a = 1\nb = 2\n", "utf-8")
    unified = gitops.diff(git_repo)
    ranges = gitops.hunk_ranges(unified)
    assert "stats.py" in ranges and ranges["new.py"] == [(1, 2)]
    start, end = ranges["stats.py"][0]
    assert start <= end


def test_is_repo_false_outside_git(median_repo):
    assert not gitops.is_repo(median_repo)


# -- MCP flow in a git repo ---------------------------------------------------------


def test_run_checkpoints_and_cli_restore(git_repo):
    app, fp, store = make({}, store=Store(paths.db_path()))
    run_id = _run_id(app.begin("fix median", str(git_repo), "self", "bugfix", "s", [STEP], VERIFY))
    app.execute(run_id, 1)
    fix(git_repo)
    (git_repo / "left-behind.txt").write_text("interrupted", "utf-8")
    points = store.checkpoints_for(run_id)
    assert len(points) == 1 and points[0]["ref"] == f"refs/bicameral/run-{run_id}/step-1-attempt-1"
    store.close()

    assert main(["restore", str(run_id), "--path", str(git_repo)]) == 0
    assert BUGGY in (git_repo / "stats.py").read_text("utf-8")
    assert not (git_repo / "left-behind.txt").exists()


def test_commit_per_step_with_provenance_and_cli_undo(git_repo):
    app, fp, store = make({}, store=Store(paths.db_path()))
    run_id = _run_id(app.begin("fix median", str(git_repo), "self", "bugfix", "s", [STEP], VERIFY, commit=True))
    app.execute(run_id, 1)
    fix(git_repo)
    app.check(run_id, 1)
    out = app.review(run_id, 1, "accept")
    assert "committed" in out
    sha = store.steps_for(run_id)[0]["commit_sha"]
    assert sha and _git(git_repo, "rev-parse", "HEAD") == sha
    body = _git(git_repo, "log", "-1", "--format=%B")
    assert f"Bicameral-Run: {run_id}" in body and f"Bicameral-Author: {ARCHITECT_ID}" in body
    assert _git(git_repo, "log", "-1", "--format=%an") == "tester"  # the user's identity, not ours
    fin = app.finish(run_id, True)
    assert f"bicameral undo {run_id}" in fin
    store.close()

    assert main(["undo", str(run_id), "--path", str(git_repo)]) == 0
    assert BUGGY in (git_repo / "stats.py").read_text("utf-8")


def test_unverified_step_is_not_committed(git_repo):
    """With a red baseline an intermediate step may be accepted unverified; that never becomes a commit."""
    no_op = edits([("stats.py", "Tiny statistics helpers.", "Tiny statistics helpers")])
    real_fix = edits([("stats.py", BUGGY, FIXED)])
    app, fp, store = make({"edit": [no_op, real_fix]})
    first = StepInput(**{**STEP.model_dump(), "suggested_role": "editor", "title": "touch docstring"})
    second = StepInput(**{**STEP.model_dump(), "id": 2, "suggested_role": "editor"})
    run_id = _run_id(app.begin("fix median", str(git_repo), EDITOR, "bugfix", "s", [first, second], VERIFY, commit=True))
    assert "Verification: FAIL" in app.execute(run_id, 1)
    out = app.review(run_id, 1, "accept")
    assert "accepted (unverified)" in out and "committed" not in out
    assert "Verification: PASS" in app.execute(run_id, 2)
    assert "committed" in app.review(run_id, 2, "accept")
    rows = store.steps_for(run_id)
    assert rows[0]["commit_sha"] is None and rows[1]["commit_sha"]
    assert _git(git_repo, "rev-list", "--count", "HEAD") == "2"  # fixture + one step commit


# -- deterministic gates -------------------------------------------------------------


def test_failing_gate_blocks_acceptance(median_repo):
    app, fp, store = make({})
    gate = '{python} -c "raise SystemExit(3)"'
    run_id = _run_id(app.begin("fix median", str(median_repo), "self", "bugfix", "s", [STEP], VERIFY, gates=[gate]))
    app.execute(run_id, 1)
    fix(median_repo)
    out = app.check(run_id, 1)
    assert "BLOCKING" in out and "gate failed" in out and "Verification: PASS" in out
    assert app.review(run_id, 1, "accept").startswith("refused: deterministic checks")
    assert "rolled back" in app.review(run_id, 1, "reject", "gate failed")
    assert BUGGY in (median_repo / "stats.py").read_text("utf-8")


def test_gate_that_cannot_run_is_a_note_not_a_pass(median_repo):
    app, fp, store = make({})
    run_id = _run_id(app.begin("fix median", str(median_repo), "self", "bugfix", "s", [STEP], VERIFY,
                               gates=["definitely-not-a-command-xyz --flag", '{python} -c "pass"']))
    app.execute(run_id, 1)
    fix(median_repo)
    out = app.check(run_id, 1)
    assert "gate could not run" in out and "not a pass" in out and "BLOCKING" not in out
    assert "accepted (verified)" in app.review(run_id, 1, "accept")
    gates = json.loads(store.steps_for(run_id)[0]["gates"])
    assert [g["ran"] for g in gates] == [False, True] and gates[1]["ok"]


# -- TDD guards: protected files and red-first steps ------------------------------------


def test_protected_files_block_acceptance(median_repo):
    step = StepInput(**{**STEP.model_dump(), "protect": ["tests"]})
    app, fp, store = make({})
    run_id = _run_id(app.begin("fix median", str(median_repo), "self", "bugfix", "s", [step], VERIFY))
    app.execute(run_id, 1)
    fix(median_repo)
    test_file = next((median_repo / "tests").glob("test_*.py"))
    test_file.write_text(test_file.read_text("utf-8") + "\n# tampered\n", "utf-8")
    out = app.check(run_id, 1)
    assert "protected files modified: tests/" in out
    assert app.review(run_id, 1, "accept").startswith("refused: deterministic checks")


def test_red_first_step_must_leave_tests_failing(median_repo):
    red = StepInput(id=1, title="Add failing test", description="write the test first", kind="test",
                    files=["stats.py"], acceptance="new test fails", suggested_role="architect", expect_red=True)
    app, fp, store = make({})
    run_id = _run_id(app.begin("tdd median", str(median_repo), "self", "bugfix", "s", [red], VERIFY))
    app.execute(run_id, 1)
    fix(median_repo)  # green: wrong for a red-first step
    out = app.check(run_id, 1)
    assert "must leave it failing" in out
    assert app.review(run_id, 1, "accept").startswith("refused")
    app.review(run_id, 1, "reject", "tests must be red")
    app.execute(run_id, 1)
    p = median_repo / "stats.py"
    p.write_text(p.read_text("utf-8") + "\n# still red\n", "utf-8")
    out = app.check(run_id, 1)
    assert "Verification is red, as this tests-first step requires" in out
    assert "accepted" in app.review(run_id, 1, "accept")


# -- scope check and disputes ---------------------------------------------------------


def test_scope_check_reports_files_outside_the_declared_set(median_repo):
    class FakeAgent(FakeProvider):
        def edit_in_place(self, model, root, prompt, effort=None):
            from bicameral.providers import AgentEditResult

            fix(root)
            (root / "scratch.txt").write_text("notes", "utf-8")
            return AgentEditResult(summary="fixed median. Concerns: the acceptance criterion is vague", cost_usd=0.0)

    fp = FakeAgent({})
    app = Bicameral(store=Store(":memory:"), llm_factory=lambda: LLM({"codex-cli": fp}))
    step = StepInput(**{**STEP.model_dump(), "suggested_role": "editor"})
    run_id = _run_id(app.begin("fix median", str(median_repo), "codex:gpt-5-codex", "bugfix", "s", [step], VERIFY))
    out = app.execute(run_id, 1)
    assert "touched outside the declared files: scratch.txt" in out
    assert "EDITOR CONCERNS" in out and "acceptance criterion is vague" in out


def test_overruling_the_second_opinion_needs_a_reason_and_is_recorded(median_repo):
    app, fp, store = make({"review": [reject("even-length branch still wrong")]})
    run_id = _run_id(app.begin("fix median", str(median_repo), EDITOR, "bugfix", "s", [STEP], VERIFY))
    app.execute(run_id, 1)
    fix(median_repo)
    assert f"Second opinion from {EDITOR}: REJECT" in app.check(run_id, 1)
    assert app.review(run_id, 1, "accept").startswith("refused")
    out = app.review(run_id, 1, "accept", "the tests pass and the reviewer misread the even branch")
    assert "accepted (verified)" in out and "dispute" in out
    d = store.disputes_for(run_id)
    assert len(d) == 1 and d[0]["objector"] == EDITOR and d[0]["resolver"] == ARCHITECT_ID and d[0]["verified"] == 1
    assert "disputes" in app.finish(run_id, True)


# -- repo-committed lessons -----------------------------------------------------------


def test_lessons_are_mirrored_into_the_repo_and_recalled(median_repo):
    app, fp, store = make({"edit": [edits([("stats.py", BUGGY, FIXED)])]})
    run_id = _run_id(app.begin("fix median", str(median_repo), EDITOR, "bugfix", "s", [STEP], VERIFY))
    app.execute(run_id, 1)
    app.review(run_id, 1, "accept")
    out = app.finish(run_id, True, [LessonInput(lesson="name the failing test in bugfix steps", applies_to=["bugfix"], role="architect")])
    assert RepoLessons.REL_PATH in out
    text = (median_repo / RepoLessons.REL_PATH).read_text("utf-8")
    assert "name the failing test in bugfix steps" in text and text.startswith("# Bicameral lessons")

    # a teammate adds a line by hand; a fresh install (empty store) still sees it at recall time
    with open(median_repo / RepoLessons.REL_PATH, "a", encoding="utf-8") as f:
        f.write("- run the slow suite only on the last step\n")
    fresh, _, _ = make({})
    recalled = fresh.recall("fix median", str(median_repo))
    assert "committed in this repository" in recalled and "run the slow suite only on the last step" in recalled


def test_repo_lessons_file_is_stable_and_capped(tmp_path):
    from bicameral.schemas import Lesson

    rl = RepoLessons(tmp_path)
    many = [Lesson(text=f"lesson {i}", applies_to=["bugfix"], role="any", confidence=0.5, id=i, score=0.0) for i in range(50)]
    assert rl.write(many) == RepoLessons.CAP
    first = rl.path.read_text("utf-8")
    assert rl.write(many) == RepoLessons.CAP and rl.path.read_text("utf-8") == first
    assert rl.read()[0] == "lesson 10"


# -- review-only mode -----------------------------------------------------------------


def test_review_diff_drops_ungrounded_findings(git_repo):
    fix(git_repo)
    grounded_line = gitops.hunk_ranges(gitops.diff(git_repo))["stats.py"][0][0]
    findings = {
        "verdict": "request_changes", "summary": "one real issue",
        "findings": [
            {"file": "stats.py", "line": grounded_line, "severity": "medium", "issue": "no guard for empty input", "suggestion": "raise ValueError"},
            {"file": "nope.py", "line": 3, "severity": "high", "issue": "hallucinated", "suggestion": ""},
            {"file": "stats.py", "line": 9999, "severity": "high", "issue": "line outside the diff", "suggestion": ""},
        ],
    }
    app, fp, store = make({"review_diff": [findings]})
    out = app.review_diff(str(git_repo), EDITOR)
    assert "verdict: request_changes" in out
    assert "stats.py:" in out and "no guard for empty input" in out
    assert "hallucinated" not in out and "outside the diff" not in out
    assert "2 finding(s) dropped" in out
    assert fp.calls_for("review_diff")[0][1] == EDITOR and "+    mid = len(s) // 2" in fp.calls_for("review_diff")[0][2]
    assert store.runs() == []


def test_review_diff_with_nothing_changed(git_repo, median_repo):
    app, fp, store = make({})
    assert app.review_diff(str(git_repo), EDITOR).startswith("nothing to review")
    plain = median_repo.parent / "plain"
    shutil.copytree(median_repo, plain, ignore=shutil.ignore_patterns(".git"))
    assert "needs a git repository" in app.review_diff(str(plain), EDITOR)


def test_store_migrates_old_databases(tmp_path):
    import sqlite3

    from bicameral.store import SCHEMA

    db = tmp_path / "old.db"
    old = SCHEMA.replace("CREATE TABLE IF NOT EXISTS checkpoints", "CREATE TABLE IF NOT EXISTS _skip_checkpoints")
    con = sqlite3.connect(db)
    con.executescript(old.split("CREATE TABLE IF NOT EXISTS routing")[0])  # only runs + steps, as an old release had them
    con.commit()
    con.close()
    with sqlite3.connect(db) as con:
        assert "commit_sha" not in {r[1] for r in con.execute("PRAGMA table_info(steps)")}
    s = Store(db)
    assert {"commit_sha", "touched", "gates"} <= {r[1] for r in s.conn.execute("PRAGMA table_info(steps)")}
    s.close()
