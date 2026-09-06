"""Standalone architect/editor loop built on the Engine.

Used by `bicameral run`, the eval harness, and the TUI's Run tab. Inside Claude
Code the same engine is driven by the MCP server instead, with Claude Code
itself acting as the architect.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .engine import Attempt, Engine, OrchestratorError, RunConfig, dedupe_lessons
from .providers import LLM, ProviderError
from .schemas import Plan, Step
from .store import Store
from .workspace import Workspace

__all__ = ["Orchestrator", "OrchestratorError", "RunConfig", "RunResult", "StepResult", "run_log"]


@dataclass
class StepResult:
    idx: int
    title: str
    kind: str
    model: str
    role: str
    attempts: int
    ok: bool
    verified: bool
    feedback: str = ""
    diff: str = ""
    cost_usd: float = 0.0


@dataclass
class RunResult:
    run_id: int
    success: bool
    task_kind: str
    plan_summary: str
    steps: list[StepResult]
    cost_usd: float
    input_tokens: int
    output_tokens: int
    duration_s: float
    verify_command: str | None
    final_verify_ok: bool | None
    lessons_learned: list[str] = field(default_factory=list)
    failure_reason: str = ""

    @property
    def attempts(self) -> int:
        return sum(s.attempts for s in self.steps)


class Orchestrator:
    def __init__(self, llm: LLM, store: Store, workspace: Workspace, cfg: RunConfig, log=None):
        self.engine = Engine(llm, store, workspace, cfg, log)
        self.store = store
        self.ws = workspace
        self.cfg = cfg
        self.log = self.engine.log

    @property
    def cost(self) -> float:
        """Total spend accounted so far, in USD; account-backed backends report 0."""
        return self.engine.cost

    def execute_step(self, idx: int, total: int, step: Step, plan_summary: str, lessons, verify: str | None, enforce_verify: bool, run_id: int) -> StepResult:
        eng = self.engine
        choice = eng.route(step)
        self.log(f"Step {idx + 1}/{total}: {step.title} [{step.kind}] -> {choice.role} ({choice.model}); {choice.reason}")
        examples = eng.examples_for(step)
        if examples:
            self.log(f"    retrieved {len(examples)} past example(s)")

        feedback = ""
        attempts = 0
        cost = 0.0
        last: Attempt | None = None
        while attempts < self.cfg.max_attempts:
            attempts += 1
            last = eng.edit(choice.model, step, plan_summary, examples, lessons, feedback)
            cost += last.cost_usd
            if not last.applied:
                feedback = last.error
                self.log(f"    attempt {attempts}: {last.error[:200]}")
                if last.blocked:
                    break
                continue

            res = eng.verify(verify)
            verify_output = res.output if res else None
            verify_ok = res.ok if res else True
            if last.concerns:
                self.log(f"    editor concerns: {last.concerns[:200]}")
            reviewer = eng.reviewer_for(choice.model)
            review, rc = eng.review(step, last.diff, verify_output, reviewer=reviewer)
            cost += rc
            self.log(f"    review by {reviewer}: {review.verdict}" + (f" - {review.feedback[:200]}" if review.verdict == "reject" else ""))

            if review.verdict == "reject":
                eng.rollback(last)
                feedback = review.feedback or "; ".join(review.issues)
                continue
            if verify and not verify_ok and enforce_verify:
                eng.rollback(last)
                feedback = f"The verification command `{verify}` failed after your edit:\n{verify_output}"
                continue

            verified = bool(verify) and verify_ok
            eng.record_success(step, choice.model, last, verified, bool(verify), run_id)
            return StepResult(idx, step.title, step.kind, choice.model, choice.role, attempts, True, verified, "", last.diff, cost)

        eng.record_failure(step, choice.model)
        return StepResult(idx, step.title, step.kind, choice.model, choice.role, attempts, False, False, feedback, last.diff if last else "", cost)

    def run(self, task: str) -> RunResult:
        t0 = time.time()
        cfg, eng = self.cfg, self.engine
        self.log(f"architect={cfg.architect_model} editor={cfg.editor_model} learning={'on' if cfg.learning else 'off'}")

        lessons = eng.recall(task)
        if lessons:
            self.log(f"recalled {len(lessons)} lesson(s) from past runs")
        lesson_ids = [l.id for l in lessons if l.id is not None]

        run_id = self.store.create_run(
            task=task, workspace=str(self.ws.root), architect_model=cfg.architect_model,
            editor_model=cfg.editor_model, learning=int(cfg.learning),
        )
        try:
            plan = eng.plan(task, lessons)
        except (ProviderError, OrchestratorError) as e:
            self.store.finish_run(run_id, success=0, summary=f"planning failed: {e}", duration_s=time.time() - t0, cost_usd=eng.cost)
            raise

        verify = eng.resolve_verify(plan.verify_command)
        if eng.two_models:
            try:
                critique, _ = eng.critique(task, plan, verify)
                if critique.concerns:
                    self.log(f"editor critique: {critique.as_text()[:600]}")
                    plan = eng.plan(task, lessons, critique=critique.as_text())
                    verify = eng.resolve_verify(plan.verify_command)
                else:
                    self.log("editor critique: no concerns")
            except (ProviderError, OrchestratorError) as e:
                self.log(f"critique skipped: {e}")
        self.log(f"plan [{plan.task_kind}]: {plan.summary}")
        for i, s in enumerate(plan.steps):
            self.log(f"  {i + 1}. {s.title} ({s.kind}, suggested: {s.suggested_role}) files: {', '.join(s.files) or '-'}")
        self.log(f"verify: {verify or '(none)'}")

        baseline_ok: bool | None = None
        if verify:
            baseline_ok = self.ws.run(verify).ok
            self.log(f"baseline verification: {'passing' if baseline_ok else 'failing'}")

        steps = plan.steps[: cfg.max_steps]
        results: list[StepResult] = []
        success = True
        failure_reason = ""
        for i, step in enumerate(steps):
            is_last = i == len(steps) - 1
            enforce = bool(verify) and (bool(baseline_ok) or is_last)
            try:
                res = self.execute_step(i, len(steps), step, plan.summary, lessons, verify, enforce, run_id)
            except ProviderError as e:
                res = StepResult(i, step.title, step.kind, cfg.editor_model, "editor", 0, False, False, str(e))
            results.append(res)
            self.store.add_step(
                run_id, step_idx=i, title=step.title, kind=step.kind, model=res.model, role=res.role,
                attempts=res.attempts, accepted=int(res.ok), verified=int(res.verified), feedback=res.feedback[:2000],
                cost_usd=res.cost_usd,
            )
            if not res.ok:
                success = False
                failure_reason = f"step {i + 1} '{step.title}' failed after {res.attempts} attempt(s): {res.feedback[:300]}"
                self.log(f"  step {i + 1} failed; stopping")
                break

        final_ok: bool | None = None
        if verify:
            final_ok = self.ws.run(verify).ok
            self.log(f"final verification: {'pass' if final_ok else 'FAIL'}")
            if success and not final_ok:
                success = False
                failure_reason = failure_reason or f"final verification `{verify}` failed"

        lessons_learned: list[str] = []
        if cfg.learning:
            eng.credit_lessons(lesson_ids, success)
            try:
                log_text = run_log(task, plan, results, success, failure_reason)
                new_lessons = eng.reflect(log_text)
                if eng.two_models:
                    new_lessons = dedupe_lessons(new_lessons + eng.reflect(log_text, model=cfg.editor_model))
                eng.remember(new_lessons, run_id)
                lessons_learned = [l.text for l in new_lessons]
                for text in lessons_learned:
                    self.log(f"  lesson: {text}")
            except ProviderError as e:
                self.log(f"reflection skipped: {e}")

        duration = time.time() - t0
        self.store.finish_run(
            run_id, task_kind=plan.task_kind, success=int(success), steps_total=len(steps),
            steps_ok=sum(1 for r in results if r.ok), cost_usd=eng.cost, input_tokens=eng.input_tokens,
            output_tokens=eng.output_tokens, duration_s=duration, summary=plan.summary if success else failure_reason,
        )
        return RunResult(
            run_id=run_id, success=success, task_kind=plan.task_kind, plan_summary=plan.summary, steps=results,
            cost_usd=eng.cost, input_tokens=eng.input_tokens, output_tokens=eng.output_tokens, duration_s=duration,
            verify_command=verify, final_verify_ok=final_ok, lessons_learned=lessons_learned, failure_reason=failure_reason,
        )


def run_log(task: str, plan: Plan, results: list[StepResult], success: bool, failure_reason: str) -> str:
    lines = [f"Task: {task}", f"Task kind: {plan.task_kind}", f"Plan: {plan.summary}", f"Outcome: {'success' if success else 'failure'}"]
    if failure_reason:
        lines.append(f"Failure: {failure_reason}")
    for r in results:
        lines.append(
            f"- Step {r.idx + 1} '{r.title}' [{r.kind}] executed by {r.role} ({r.model}): "
            f"{'accepted' if r.ok else 'FAILED'} after {r.attempts} attempt(s)"
            + ("; verified" if r.verified else "")
            + (f"; last feedback: {r.feedback[:400]}" if r.feedback else "")
        )
    return "\n".join(lines)
