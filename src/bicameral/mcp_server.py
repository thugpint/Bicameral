"""MCP server that Claude Code drives through the /bicameral skill.

Claude Code is the Architect: it plans, reviews and reflects using the user's
own Anthropic account. This server owns everything that needs to be measured
or learned: routing, delegation to the Editor backend, verification, rollback,
per-step outcome logging, lessons and examples.

Run with: python -m bicameral.mcp_server   (stdio transport)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from . import __version__, auth, config, gitops, models, paths
from .edits import unified_diff
from .engine import Attempt, Engine, RunConfig, dedupe_lessons, lessons_from_dicts
from .evals.harness import format_report
from .memory import Examples, Memory, RepoLessons
from .orchestrator import StepResult, run_log
from .providers import LLM, ProviderError, build_providers
from .router import Choice
from .schemas import ROLES, TASK_KINDS, Plan, Step
from .store import Store
from .workspace import Workspace

ARCHITECT_ID = "claude-code"  # the host session; billed to the user's account, not to us

INSTRUCTIONS = (
    "Bicameral: two-model coding loop. You (the host) are the Architect. Call bicameral_status, then "
    "bicameral_recall, plan, bicameral_critique (the Editor's read of your plan), bicameral_begin, and for each step "
    "bicameral_execute -> (bicameral_check if you edited yourself) -> bicameral_review; finish with bicameral_finish. "
    "The /bicameral skill has the full protocol."
)


class StepInput(BaseModel):
    id: int = Field(description="1-based step number")
    title: str
    description: str = Field(description="What to change and why, precise enough for another model to execute")
    kind: str = Field(description=f"One of: {', '.join(TASK_KINDS)}")
    files: list[str] = Field(default_factory=list, description="Repo-relative paths this step will touch")
    acceptance: str = Field(description="Concrete criterion the reviewer can check")
    suggested_role: str = Field(default="editor", description="'editor' (delegate) or 'architect' (do it yourself)")
    pin: bool = Field(default=False, description="True when the user named who must do this step; the router will not override suggested_role")
    protect: list[str] = Field(default_factory=list, description="Paths this step must not modify (TDD: the test files, so the implementation cannot edit them)")
    expect_red: bool = Field(default=False, description="True for a tests-first step: it must leave verification failing")
    rationale: str = ""


class LessonInput(BaseModel):
    lesson: str
    applies_to: list[str] = Field(default_factory=list, description=f"Subset of: {', '.join(TASK_KINDS)}")
    role: str = Field(default="any", description="architect | editor | router | any")
    confidence: float = 0.6


@dataclass
class Session:
    run_id: int
    task: str
    ws: Workspace
    engine: Engine
    cfg: RunConfig
    plan: Plan
    verify: str | None
    baseline_ok: bool | None
    lesson_ids: list[int]
    two_models: bool = True
    commits: dict[int, str] = field(default_factory=dict)
    started: float = field(default_factory=time.time)
    routes: dict[int, Choice] = field(default_factory=dict)
    attempts: dict[int, int] = field(default_factory=dict)
    pending: dict[int, Attempt] = field(default_factory=dict)  # applied, awaiting review
    self_snapshots: dict[int, dict[str, str]] = field(default_factory=dict)
    results: dict[int, StepResult] = field(default_factory=dict)
    feedback: dict[int, str] = field(default_factory=dict)

    def step(self, step_id: int) -> Step:
        for s in self.plan.steps:
            if s.id == step_id:
                return s
        raise ValueError(f"unknown step id {step_id}; valid: {[s.id for s in self.plan.steps]}")

    def enforce_verify(self, step_id: int) -> bool:
        if not self.verify or self.step(step_id).expect_red:
            return False
        is_last = step_id == self.plan.steps[-1].id
        return bool(self.baseline_ok) or is_last


class Bicameral:
    """Tool implementations, kept free of MCP plumbing so they can be tested directly."""

    def __init__(self, store: Store | None = None, llm_factory=None):
        self._store = store
        self._llm_factory = llm_factory or (lambda: LLM(build_providers()))
        self.sessions: dict[int, Session] = {}

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(paths.db_path())
        return self._store

    # -- status / recall --------------------------------------------------------

    def status(self) -> str:
        cfg = config.load()
        lines = [f"Bicameral {__version__}", "", "Editor backends:"]
        for b in auth.backends():
            mark = "ready" if b.available else "unavailable"
            lines.append(f"- {b.id:<10} {mark:<12} {b.label}: {b.detail}" + (f"  -> {b.login_hint}" if b.login_hint else ""))
        avail = {b.id for b in auth.backends() if b.available}
        usable = [m for m in models.all_models() if m.provider in avail]
        lines.append("")
        lines.append("Editor models you can choose right now:")
        if usable:
            for m in usable:
                price = "your account" if m.account_backed else (f"${m.input_per_m}/${m.output_per_m} per 1M" if m.input_per_m is not None else "price n/a")
                lines.append(f"- {m.id:<22} {m.display} ({price})")
        else:
            lines.append("- none: sign in to a backend first (see hints above), or route every step to yourself")
        lines.append("Any other id the account supports works too: prefix it with codex: or claude: for the account backends.")
        lines.append(f"\nLast editor model: {cfg.get('last_editor') or '(none yet)'}")
        lines.append("The Architect is this session's model; the user changes it with /model, not here.")
        self.store.mark_interrupted(list(self.sessions))
        runs = self.store.runs(limit=1000)
        done = [r for r in runs if r["success"] is not None]
        if done:
            ok = sum(r["success"] for r in done)
            lines.append(f"History: {ok}/{len(done)} runs succeeded; {len(self.store.lessons())} lessons stored")
        return "\n".join(lines)

    def recall(self, task: str, workspace: str = "") -> str:
        store = self.store
        lessons = Memory(store).retrieve(task, k=6)
        examples = Examples(store).retrieve(task, "feature", k=3)
        out = ["## Lessons from past runs (apply the ones that fit)"]
        out += [f"- ({lesson.role}; {', '.join(lesson.applies_to) or 'any'}; score {lesson.score:+.1f}) {lesson.text}" for lesson in lessons] or ["- none yet"]
        if workspace:
            try:
                repo_lessons = RepoLessons(Workspace(workspace).root).read()
            except NotADirectoryError:
                repo_lessons = []
            known = {lesson.text.strip() for lesson in lessons}
            extra = [t for t in repo_lessons if t not in known]
            if extra:
                out.append(f"\n## Lessons committed in this repository ({RepoLessons.REL_PATH})")
                out += [f"- {t}" for t in extra[:12]]
        out.append("\n## Similar accepted edits")
        for e in examples:
            out.append(f"- [{e.kind}] {e.description[:160]} (files: {', '.join(e.files)})")
        if not examples:
            out.append("- none yet")
        table = store.routing_table()
        if table:
            out.append("\n## Routing track record (step kind: model accepted/total)")
            for r in table:
                out.append(f"- {r['kind']}: {r['model']} {r['successes']}/{r['successes'] + r['failures']}")
        return "\n".join(out)

    # -- run lifecycle ----------------------------------------------------------

    def _prepare(self, workspace: str, editor_model: str, task_kind: str, summary: str,
                 steps: list[StepInput], verify_command: str, learning: bool,
                 gates: list[str] | None = None, commit: bool = False) -> tuple[Workspace, Plan, Engine, RunConfig] | str:
        try:
            ws = Workspace(workspace)
        except NotADirectoryError:
            return f"error: workspace is not a directory: {workspace}"
        if not steps:
            return "error: a plan needs at least one step"
        if task_kind not in TASK_KINDS:
            task_kind = "feature"
        plan = Plan(task_kind=task_kind, summary=summary, verify_command=verify_command,
                    steps=[Step.from_dict(s.model_dump(), i) for i, s in enumerate(steps)])
        for i, s in enumerate(plan.steps):
            s.id = i + 1
            if s.suggested_role not in ROLES:
                s.suggested_role = "editor"

        user_cfg = config.load()
        cfg = RunConfig(
            architect_model=ARCHITECT_ID, editor_model=editor_model, learning=learning,
            verify_command=verify_command or None, max_attempts=int(user_cfg.get("max_attempts", 3)),
            editor_effort=str(user_cfg.get("editor_effort", "medium")),
            gates=[g for g in (gates or []) if g.strip()], commit=commit,
        )
        llm = self._llm_factory()
        if editor_model not in ("", "self", ARCHITECT_ID):
            try:
                llm.provider_for(editor_model)
            except ProviderError as e:
                return f"error: {e}"
        return ws, plan, Engine(llm, self.store, ws, cfg), cfg

    def critique(self, task: str, workspace: str, editor_model: str, task_kind: str, summary: str,
                 steps: list[StepInput], verify_command: str = "") -> str:
        prepared = self._prepare(workspace, editor_model, task_kind, summary, steps, verify_command, True)
        if isinstance(prepared, str):
            return prepared
        ws, plan, engine, cfg = prepared
        if not engine.two_models:
            return "no editor model configured, so there is no second mind to ask. Call bicameral_begin."
        verify = engine.resolve_verify(verify_command)
        try:
            critique, _ = engine.critique(task, plan, verify)
        except ProviderError as e:
            return f"critique unavailable ({e}). Call bicameral_begin with your plan as it stands."
        if not critique.concerns:
            return f"{editor_model} read the plan and has no concerns" + (f": {critique.assessment}" if critique.assessment else ".") + "\nCall bicameral_begin."
        return (f"{editor_model} read the plan and raised {len(critique.concerns)} concern(s):\n{critique.as_text()}\n\n"
                "Revise the steps where it is right (you may also disagree), then call bicameral_begin with the final plan.")

    def review_diff(self, workspace: str, model: str, base: str = "HEAD", context: str = "") -> str:
        """Review-only mode: an independent model reviews an existing diff; ungrounded findings are dropped."""
        try:
            ws = Workspace(workspace)
        except NotADirectoryError:
            return f"error: workspace is not a directory: {workspace}"
        if not gitops.is_repo(ws.root):
            return "error: review mode needs a git repository (the diff is taken against a git ref)"
        try:
            unified = gitops.diff(ws.root, base or "HEAD")
        except gitops.GitError as e:
            return f"error: {e}"
        if not unified:
            return f"nothing to review: the working tree matches {base or 'HEAD'}"
        llm = self._llm_factory()
        try:
            llm.provider_for(model)
        except ProviderError as e:
            return f"error: {e}"
        cfg = RunConfig(architect_model=ARCHITECT_ID, editor_model=model, learning=False, verify_command="")
        engine = Engine(llm, self.store, ws, cfg)
        try:
            review, cost = engine.review_diff(model, unified, context)
        except ProviderError as e:
            return f"error: {e}"
        files = sorted(gitops.hunk_ranges(unified))
        return (f"Review of {len(files)} changed file(s) against {base or 'HEAD'} by {model} (${cost:.4f}):\n"
                + review.as_text()
                + "\n\nEvery finding above points at a line the diff touches. Check the high and medium ones yourself before relaying them.")

    def begin(self, task: str, workspace: str, editor_model: str, task_kind: str, summary: str,
              steps: list[StepInput], verify_command: str = "", learning: bool = True,
              gates: list[str] | None = None, commit: bool = False) -> str:
        prepared = self._prepare(workspace, editor_model, task_kind, summary, steps, verify_command, learning, gates, commit)
        if isinstance(prepared, str):
            return prepared
        ws, plan, engine, cfg = prepared
        self_only = not engine.two_models
        if not self_only:
            config.update(last_editor=editor_model)
        self.store.mark_interrupted(list(self.sessions))
        lessons = engine.recall(task)
        run_id = self.store.create_run(task=task, workspace=str(ws.root), architect_model=ARCHITECT_ID,
                                       editor_model=editor_model, learning=int(learning), task_kind=task_kind)
        verify = engine.resolve_verify(verify_command)
        baseline = ws.run(verify).ok if verify else None
        session = Session(run_id, task, ws, engine, cfg, plan, verify, baseline, [lesson.id for lesson in lessons if lesson.id is not None],
                          two_models=not self_only)
        for s in plan.steps:
            if self_only:
                session.routes[s.id] = Choice(ARCHITECT_ID, "architect", "no editor model configured")
            else:
                session.routes[s.id] = engine.route(s)
        self.sessions[run_id] = session

        out = [f"run_id: {run_id}", f"workspace: {ws.root}",
               f"verify: {verify or '(none)'}" + (f"  baseline: {'passing' if baseline else 'FAILING'}" if verify else ""),
               f"gates: {', '.join(cfg.gates) if cfg.gates else '(none)'}",
               "checkpoints: git refs under refs/bicameral/ (restorable with `bicameral restore`)" if engine.is_repo
               else "checkpoints: in-memory only (not a git repository; an interrupted run cannot be restored later)",
               f"commit per step: {'yes' if cfg.commit else 'no'}",
               f"learning: {'on' if learning else 'off'}", "", "Routing:"]
        for s in plan.steps:
            c = session.routes[s.id]
            who = "YOU edit it yourself" if c.role == "architect" else f"delegate to {c.model}"
            out.append(f"- step {s.id} [{s.kind}] {s.title} -> {who}  ({c.reason})")
        out.append("\nNext: call bicameral_execute(run_id, step_id=1). Handle steps strictly in order.")
        if verify and not baseline:
            out.append("The baseline is failing, so intermediate steps may leave verification red; the final step must make it pass.")
        return "\n".join(out)

    def execute(self, run_id: int, step_id: int, feedback: str = "") -> str:
        s = self.sessions.get(run_id)
        if not s:
            return f"error: unknown run_id {run_id}; call bicameral_begin first"
        try:
            step = s.step(step_id)
        except ValueError as e:
            return f"error: {e}"
        if step_id in s.results:
            return f"error: step {step_id} is already finished ({'ok' if s.results[step_id].ok else 'failed'})"
        if step_id in s.pending:
            return f"error: step {step_id} has an applied edit awaiting bicameral_review"
        prior = [x.id for x in s.plan.steps if x.id < step_id and x.id not in s.results]
        if prior:
            return f"error: finish step(s) {prior} before step {step_id}"
        if s.attempts.get(step_id, 0) >= s.cfg.max_attempts:
            return self._fail_step(s, step_id, feedback or "attempt limit reached")

        choice = s.routes[step_id]
        s.attempts[step_id] = s.attempts.get(step_id, 0) + 1
        fb = feedback or s.feedback.get(step_id, "")
        attempt_no = s.attempts[step_id]

        s.engine.checkpoint(s.run_id, step_id, attempt_no)
        if choice.role == "architect":
            s.self_snapshots[step_id] = s.ws.snapshot_tree()
            return (
                f"step {step_id} attempt {attempt_no}/{s.cfg.max_attempts}: routed to YOU ({choice.reason}).\n"
                f"Edit the files yourself now. Step: {step.title}\n{step.description}\nAcceptance: {step.acceptance}\n"
                + (f"Feedback to address: {fb}\n" if fb else "")
                + "Keep the change minimal. When done, call bicameral_check(run_id, step_id) to diff and verify."
            )

        examples = s.engine.examples_for(step)
        lessons = s.engine.memory.retrieve(s.task, k=5) if s.cfg.learning else []
        try:
            attempt = s.engine.edit(choice.model, step, s.plan.summary, examples, lessons, fb)
        except ProviderError as e:
            attempt = Attempt(False, f"editor backend error: {e}")
        if not attempt.applied:
            s.feedback[step_id] = attempt.error
            if attempt.blocked or s.attempts[step_id] >= s.cfg.max_attempts:
                return self._fail_step(s, step_id, attempt.error)
            return (f"step {step_id} attempt {attempt_no}: the editor produced no applicable edit: {attempt.error}\n"
                    f"Call bicameral_execute again (attempts left: {s.cfg.max_attempts - attempt_no}); add feedback if you can sharpen the step.")
        s.pending[step_id] = attempt
        return self._present(s, step_id, attempt, f"delegated to {choice.model}", second_opinion=False)

    def check(self, run_id: int, step_id: int) -> str:
        s = self.sessions.get(run_id)
        if not s:
            return f"error: unknown run_id {run_id}"
        before = s.self_snapshots.get(step_id)
        if before is None:
            return f"error: step {step_id} was not routed to you, or bicameral_execute was not called first"
        changed = s.ws.changed_since(before)
        if not changed:
            return f"step {step_id}: no files changed since bicameral_execute. Make the edit, then call bicameral_check again."
        s.self_snapshots.pop(step_id, None)
        snap: dict[str, str | None] = {p: before.get(p) for p in changed}
        attempt = Attempt(True, snapshot=snap, touched=list(changed), diff=unified_diff(snap, changed))
        s.pending[step_id] = attempt
        return self._present(s, step_id, attempt, "edited by you", second_opinion=True)

    def _present(self, s: Session, step_id: int, attempt: Attempt, who: str, second_opinion: bool) -> str:
        step = s.step(step_id)
        res = s.engine.verify(s.verify)
        verify_text = "(no verification command)"
        if res is not None:
            verify_text = f"{'PASS' if res.ok else f'FAIL (exit {res.code})'}\n```\n{res.output[-3000:]}\n```"
            attempt.verify_ok = res.ok
        enforce = s.enforce_verify(step_id)
        diff = attempt.diff if len(attempt.diff) <= 20000 else attempt.diff[:20000] + "\n... (diff truncated)"
        s.engine.inspect(step, attempt, res)
        checks = ""
        if attempt.blocking:
            checks += "BLOCKING (acceptance will be refused; reject with this as feedback):\n" + "".join(f"- {b}\n" for b in attempt.blocking)
        if attempt.notes:
            checks += "Checks:\n" + "".join(f"- {n}\n" for n in attempt.notes)
        if attempt.gates and not attempt.blocking and all(g["ran"] and g["ok"] for g in attempt.gates):
            checks += f"Gates: {len(attempt.gates)} passed.\n"
        if step.expect_red and res is not None and not res.ok:
            checks += "Verification is red, as this tests-first step requires.\n"
        opinion = ""
        if second_opinion and s.two_models and not attempt.blocking:
            # You wrote this one, so the other mind reviews it before you judge your own work.
            try:
                review, _ = s.engine.review(step, attempt.diff, res.output if res else None, reviewer=s.cfg.editor_model)
                attempt.second_opinion = review
                opinion = (f"\nSecond opinion from {s.cfg.editor_model}: {review.as_text()}\n"
                           "You make the final call. To accept over a rejection you must say why in feedback; the dispute is recorded.\n")
            except ProviderError as e:
                opinion = f"\nSecond opinion unavailable ({e}).\n"
        return (
            f"step {step_id} attempt {s.attempts[step_id]}/{s.cfg.max_attempts} ({who}); files: {', '.join(attempt.touched)}\n"
            + (f"editor summary: {attempt.summary[:600]}\n" if attempt.summary else "")
            + (f"EDITOR CONCERNS about this step (address or answer them in your review): {attempt.concerns[:800]}\n" if attempt.concerns else "")
            + f"\nAcceptance criterion: {step.acceptance}\n\n```diff\n{diff}\n```\n\nVerification: {verify_text}\n"
            + ("Verification MUST pass for this step to be accepted.\n" if enforce and res is not None else "")
            + checks
            + opinion
            + "\nReview the diff against the acceptance criterion, then call bicameral_review(run_id, step_id, verdict, feedback)."
        )

    def review(self, run_id: int, step_id: int, verdict: str, feedback: str = "") -> str:
        s = self.sessions.get(run_id)
        if not s:
            return f"error: unknown run_id {run_id}"
        attempt = s.pending.get(step_id)
        if attempt is None:
            return f"error: step {step_id} has no applied edit to review; call bicameral_execute first"
        step = s.step(step_id)
        choice = s.routes[step_id]
        verify_ok = attempt.verify_ok
        verdict = verdict.strip().lower()

        if verdict == "accept" and s.enforce_verify(step_id) and verify_ok is False:
            return ("refused: verification failed and this step must pass it. Reject with feedback describing the failure "
                    "(the edit will be rolled back), or fix the cause in a further attempt.")
        if verdict == "accept" and attempt.blocking:
            return "refused: deterministic checks block this edit:\n" + "\n".join(f"- {b}" for b in attempt.blocking) + "\nReject with this as feedback (the edit will be rolled back)."
        overruled = attempt.second_opinion is not None and attempt.second_opinion.verdict == "reject"
        if verdict == "accept" and overruled and not feedback.strip():
            return (f"refused: {s.cfg.editor_model} rejected this diff ({attempt.second_opinion.feedback[:300]}). "
                    "To accept anyway, pass feedback stating why it is wrong; the disagreement is recorded either way.")

        if verdict == "accept":
            s.pending.pop(step_id)
            verified = bool(s.verify) and verify_ok is True
            s.engine.record_success(step, choice.model, attempt, verified, bool(s.verify), s.run_id)
            if overruled:
                self.store.add_dispute(s.run_id, step_id, author=choice.model, objector=s.cfg.editor_model,
                                       objection=attempt.second_opinion.feedback or "; ".join(attempt.second_opinion.issues),
                                       resolution=feedback.strip(), resolver=ARCHITECT_ID, verified=verify_ok)
            reviewer = ARCHITECT_ID if choice.role == "editor" else (s.cfg.editor_model if s.two_models else ARCHITECT_ID)
            sha = s.engine.commit_step(s.run_id, step, attempt, choice.model, reviewer) if (verified or not s.verify) else None
            if sha:
                s.commits[step_id] = sha
            result = StepResult(step.id - 1, step.title, step.kind, choice.model, choice.role, s.attempts[step_id], True, verified, "", attempt.diff, attempt.cost_usd)
            s.results[step_id] = result
            self.store.add_step(s.run_id, step_idx=step.id - 1, title=step.title, kind=step.kind, model=choice.model, role=choice.role,
                                attempts=result.attempts, accepted=1, verified=int(verified), feedback="", cost_usd=attempt.cost_usd,
                                commit_sha=sha, touched=json.dumps(attempt.touched), gates=json.dumps(attempt.gates))
            nxt = [x.id for x in s.plan.steps if x.id not in s.results]
            return (f"step {step_id} accepted ({'verified' if verified else 'unverified'})"
                    + (f", committed {sha[:10]}" if sha else "")
                    + (f"; dispute with {s.cfg.editor_model} recorded" if overruled else "") + ".\n"
                    + (f"Next: bicameral_execute(run_id, step_id={nxt[0]})." if nxt else "All steps done. Call bicameral_finish(run_id, success=true, lessons=[...])."))

        # reject
        s.pending.pop(step_id)
        s.engine.rollback(attempt)
        s.feedback[step_id] = feedback or "rejected by reviewer"
        left = s.cfg.max_attempts - s.attempts[step_id]
        if left <= 0:
            return self._fail_step(s, step_id, feedback or "rejected by reviewer")
        return (f"step {step_id} rejected and rolled back; {left} attempt(s) left.\n"
                f"Call bicameral_execute(run_id, step_id={step_id}, feedback=...) with concrete guidance.")

    def _fail_step(self, s: Session, step_id: int, why: str) -> str:
        step = s.step(step_id)
        choice = s.routes[step_id]
        s.pending.pop(step_id, None)
        s.engine.record_failure(step, choice.model)
        result = StepResult(step.id - 1, step.title, step.kind, choice.model, choice.role, s.attempts.get(step_id, 0), False, False, why)
        s.results[step_id] = result
        self.store.add_step(s.run_id, step_idx=step.id - 1, title=step.title, kind=step.kind, model=choice.model, role=choice.role,
                            attempts=result.attempts, accepted=0, verified=0, feedback=why[:2000], cost_usd=0.0)
        return (f"step {step_id} FAILED after {result.attempts} attempt(s): {why[:400]}\n"
                "Stop executing further steps. Call bicameral_finish(run_id, success=false, lessons=[...]) and report to the user.")

    def finish(self, run_id: int, success: bool, lessons: list[LessonInput] | None = None) -> str:
        s = self.sessions.pop(run_id, None)
        if not s:
            return f"error: unknown run_id {run_id}"
        for attempt in list(s.pending.values()):
            s.engine.rollback(attempt)  # never leave an unreviewed edit behind
        final_ok: bool | None = None
        if s.verify:
            final_ok = s.ws.run(s.verify).ok
            if success and not final_ok:
                success = False
        results = [s.results[x.id] for x in s.plan.steps if x.id in s.results]
        failure = next((f"step {r.idx + 1} '{r.title}' failed: {r.feedback[:200]}" for r in results if not r.ok), "")
        if not success and not failure and final_ok is False:
            failure = f"final verification `{s.verify}` failed"

        new_lessons = lessons_from_dicts([lesson.model_dump() for lesson in (lessons or [])])
        log_text = run_log(s.task, s.plan, results, success, failure)
        editor_lessons = []
        if s.two_models and s.cfg.learning:
            try:
                editor_lessons = s.engine.reflect(log_text, model=s.cfg.editor_model)
            except ProviderError:
                editor_lessons = []
        known = {" ".join(lesson.text.lower().split()) for lesson in new_lessons}
        editor_lessons = [lesson for lesson in dedupe_lessons(editor_lessons) if " ".join(lesson.text.lower().split()) not in known]
        s.engine.credit_lessons(s.lesson_ids, success)
        s.engine.remember(new_lessons + editor_lessons, s.run_id)
        repo_written = RepoLessons(s.ws.root).write(self.store.lessons_for_workspace(str(s.ws.root))) if s.cfg.learning else 0
        duration = time.time() - s.started
        self.store.finish_run(
            s.run_id, task_kind=s.plan.task_kind, success=int(success), steps_total=len(s.plan.steps),
            steps_ok=sum(1 for r in results if r.ok), cost_usd=s.engine.cost, input_tokens=s.engine.input_tokens,
            output_tokens=s.engine.output_tokens, duration_s=duration, summary=s.plan.summary if success else failure,
        )
        lines = [f"run {s.run_id}: {'SUCCESS' if success else 'FAILED'}" + (f" - {failure}" if failure else "")]
        for r in results:
            lines.append(f"- step {r.idx + 1} {r.title}: {'ok' if r.ok else 'failed'} by {r.role} ({r.model}), {r.attempts} attempt(s)" + (", verified" if r.verified else ""))
        if s.verify:
            lines.append(f"final verification: {'pass' if final_ok else 'FAIL'}")
        lines.append(f"editor cost: ${s.engine.cost:.4f} ({s.engine.input_tokens}+{s.engine.output_tokens} tok); duration {duration:.0f}s")
        if new_lessons:
            lines.append("lessons stored: " + "; ".join(lesson.text for lesson in new_lessons))
        if editor_lessons:
            lines.append(f"lessons from {s.cfg.editor_model}: " + "; ".join(lesson.text for lesson in editor_lessons))
        if repo_written:
            lines.append(f"{repo_written} lesson(s) for this repo mirrored into {RepoLessons.REL_PATH} (commit it to share them)")
        disputes = self.store.disputes_for(s.run_id)
        if disputes:
            lines.append("disputes (the other mind objected, you overruled):")
            lines += [f"- step {d['step_id']}: {d['objector']} said: {d['objection'][:160]} | you said: {d['resolution'][:160]}"
                      + (" | verification: pass" if d["verified"] else (" | verification: FAIL" if d["verified"] == 0 else "")) for d in disputes]
        if s.commits:
            lines.append("commits: " + ", ".join(f"step {k} -> {v[:10]}" for k, v in sorted(s.commits.items()))
                         + f". `bicameral undo {s.run_id}` reverts them.")
        if s.engine.is_repo:
            lines.append(f"checkpoints: `bicameral restore {s.run_id}` puts the tree back to before this run.")
        changed = s.ws.run("git status --short", timeout=30)
        if changed.ok and changed.output:
            lines.append("\nUncommitted changes in the working tree:\n```\n" + changed.output[-2000:] + "\n```")
            lines.append("Tell the user these files changed and that `git diff` shows the full change; they commit when happy.")
        lines.append("\nRun log for your reflection:\n" + log_text)
        return "\n".join(lines)

    def stats(self) -> str:
        store = self.store
        runs = [r for r in store.runs(limit=1000) if r["success"] is not None]
        lines = []
        for learning in (1, 0):
            subset = [r for r in runs if r["learning"] == learning]
            if subset:
                ok = sum(r["success"] for r in subset)
                lines.append(f"learning {'on' if learning else 'off'}: {ok}/{len(subset)} runs succeeded ({ok / len(subset):.0%})")
        table = store.routing_table()
        if table:
            lines.append("\nrouting (kind x model -> accepted/total):")
            lines += [f"  {r['kind']:<12} {r['model']:<24} {r['successes']}/{r['successes'] + r['failures']}" for r in table]
        lines.append("\n" + format_report(store))
        top = store.lessons()[:8]
        if top:
            lines.append("\ntop lessons:")
            lines += [f"  [{lesson.score:+.1f}] ({lesson.role}) {lesson.text}" for lesson in top]
        return "\n".join(lines) or "no data yet"


# -- MCP wiring --------------------------------------------------------------------

_app = Bicameral()


def build_server():
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("bicameral", instructions=INSTRUCTIONS, version=__version__)

    @server.tool(description="Backends, sign-in state, editor models available right now, and history. Call first.")
    def bicameral_status() -> str:
        return _app.status()

    @server.tool(description=("Lessons, similar accepted edits and routing track record relevant to a task, plus lessons committed "
                              "in the repository's .bicameral/lessons.md. Call before planning."))
    def bicameral_recall(task: str, workspace: str = "") -> str:
        return _app.recall(task, workspace)

    @server.tool(description=("Review-only mode: an independent model reviews the working tree's diff against a git ref "
                              "(default HEAD) and returns findings with file:line, each verified to point at a line the diff "
                              "touches. No plan, no edits. Use when the user asks for a review or a second opinion on a change."))
    def bicameral_review_diff(workspace: str, model: str, base: str = "HEAD", context: str = "") -> str:
        return _app.review_diff(workspace, model, base, context)

    @server.tool(description=("Ask the editor model to critique your draft plan before anything is edited: under-specified steps, "
                              "wrong files, missing steps, unverifiable acceptance criteria. Same arguments as bicameral_begin. "
                              "Revise where it is right, then call bicameral_begin."))
    def bicameral_critique(task: str, workspace: str, editor_model: str, task_kind: str, summary: str,
                           steps: list[StepInput], verify_command: str = "") -> str:
        return _app.critique(task, workspace, editor_model, task_kind, summary, steps, verify_command)

    @server.tool(description=("Register your plan and start a run. Routes each step to you or to the editor model "
                              "(set pin=true on a step to forbid the router from overriding suggested_role), "
                              "runs the baseline verification, and returns the routing. editor_model may be 'self' to do every step yourself."))
    def bicameral_begin(task: str, workspace: str, editor_model: str, task_kind: str, summary: str,
                        steps: list[StepInput], verify_command: str = "", learning: bool = True,
                        gates: list[str] | None = None, commit: bool = False) -> str:
        """gates: deterministic commands (lint, typecheck, build) run on every applied edit before review; a failing
        gate blocks acceptance. commit: commit each accepted, verified step with Bicameral-Author/Reviewer trailers."""
        return _app.begin(task, workspace, editor_model, task_kind, summary, steps, verify_command, learning, gates, commit)

    @server.tool(description=("Execute one step. Delegated steps are edited by the editor model and come back as a diff plus "
                              "verification output; steps routed to you return instructions to edit yourself. Pass feedback on retries."))
    def bicameral_execute(run_id: int, step_id: int, feedback: str = "") -> str:
        return _app.execute(run_id, step_id, feedback)

    @server.tool(description=("After editing a step yourself: diff the workspace against the snapshot, run verification, "
                              "and get the editor model's second opinion on your diff."))
    def bicameral_check(run_id: int, step_id: int) -> str:
        return _app.check(run_id, step_id)

    @server.tool(description="Accept or reject the applied edit for a step. Rejection rolls the files back; feedback goes to the next attempt.")
    def bicameral_review(run_id: int, step_id: int, verdict: str, feedback: str = "") -> str:
        return _app.review(run_id, step_id, verdict, feedback)

    @server.tool(description="Close the run: final verification, outcome logging, lesson storage and scoring. Returns the summary to report.")
    def bicameral_finish(run_id: int, success: bool, lessons: list[LessonInput] | None = None) -> str:
        return _app.finish(run_id, success, lessons)

    @server.tool(description="Success rates, routing table, eval results and top lessons.")
    def bicameral_stats() -> str:
        return _app.stats()

    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
