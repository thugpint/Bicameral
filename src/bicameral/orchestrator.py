"""The architect/editor loop: plan, route, edit, review, verify, record, reflect."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import prompts
from .edits import EditError, apply_edits, unified_diff
from .memory import Examples, Memory
from .providers import LLM, ProviderError
from .router import Router
from .schemas import (
    EDIT_SCHEMA,
    PLAN_SCHEMA,
    REFLECT_SCHEMA,
    REVIEW_SCHEMA,
    TASK_KINDS,
    EditResult,
    Lesson,
    Plan,
    Review,
    Step,
)
from .store import Store
from .workspace import Workspace

Logger = Callable[[str], None]


class OrchestratorError(RuntimeError):
    pass


@dataclass
class RunConfig:
    architect_model: str
    editor_model: str
    learning: bool = True
    verify_command: str | None = None  # None = auto-detect, "" = disabled
    max_attempts: int = 3
    architect_effort: str = "high"
    editor_effort: str = "medium"
    max_steps: int = 12
    max_file_rounds: int = 2


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
    def __init__(self, llm: LLM, store: Store, workspace: Workspace, cfg: RunConfig, log: Logger | None = None):
        self.llm = llm
        self.store = store
        self.ws = workspace
        self.cfg = cfg
        self.log: Logger = log or (lambda _: None)
        self.router = Router(store, learning=cfg.learning)
        self.memory = Memory(store)
        self.examples = Examples(store)
        self._cost = 0.0
        self._in = 0
        self._out = 0
        self._step_cost = 0.0

    # -- model calls -------------------------------------------------------

    def _call(self, model: str, system: str, user: str, schema: dict[str, Any], name: str, effort: str) -> dict[str, Any]:
        data, stats = self.llm.complete_json(model, system, user, schema, name, effort=effort)
        cost = stats.cost_usd or 0.0
        self._cost += cost
        self._step_cost += cost
        self._in += stats.input_tokens
        self._out += stats.output_tokens
        cost_s = f"${cost:.4f}" if stats.cost_usd is not None else "$?"
        self.log(f"    {name} <- {model}: {stats.input_tokens}+{stats.output_tokens} tok, {cost_s}, {stats.latency_ms} ms")
        return data

    # -- phases --------------------------------------------------------------

    def plan(self, task: str, lessons: list[Lesson]) -> Plan:
        overview = self.ws.overview()
        files: dict[str, str] = {}
        data: dict[str, Any] = {}
        for _ in range(self.cfg.max_file_rounds + 1):
            user = prompts.build_plan_prompt(task, overview, files, lessons)
            data = self._call(self.cfg.architect_model, prompts.ARCHITECT_SYSTEM, user, PLAN_SCHEMA, "plan", self.cfg.architect_effort)
            wanted = [f for f in data.get("files_needed", []) if f and f not in files]
            if data.get("status") == "need_files" and wanted:
                self.log(f"    architect asked for {len(wanted)} file(s): {', '.join(wanted[:6])}")
                files.update(self.ws.read_many(wanted))
                continue
            break
        plan = Plan.from_dict(data)
        if not plan.steps:
            raise OrchestratorError("architect produced a plan with no steps")
        return plan

    def edit(self, model: str, step: Step, plan_summary: str, files: dict[str, str], examples: list, lessons: list[Lesson], feedback: str) -> EditResult:
        effort = self.cfg.architect_effort if model == self.cfg.architect_model else self.cfg.editor_effort
        result = EditResult("blocked", [], "no response", [], [])
        for _ in range(self.cfg.max_file_rounds + 1):
            user = prompts.build_edit_prompt(step, plan_summary, files, examples, lessons, feedback)
            data = self._call(model, prompts.EDITOR_SYSTEM, user, EDIT_SCHEMA, "edit", effort)
            result = EditResult.from_dict(data)
            wanted = [f for f in result.files_needed if f and f not in files]
            if result.status == "need_files" and wanted:
                self.log(f"    editor asked for {len(wanted)} file(s): {', '.join(wanted[:6])}")
                files.update(self.ws.read_many(wanted))
                continue
            return result
        return result

    def review(self, step: Step, diff: str, verify_output: str | None) -> Review:
        user = prompts.build_review_prompt(step, diff, verify_output)
        data = self._call(self.cfg.architect_model, prompts.REVIEWER_SYSTEM, user, REVIEW_SCHEMA, "review", self.cfg.architect_effort)
        return Review.from_dict(data)

    def reflect(self, run_log: str) -> list[Lesson]:
        data = self._call(self.cfg.architect_model, prompts.REFLECT_SYSTEM, prompts.build_reflect_prompt(run_log), REFLECT_SCHEMA, "reflect", self.cfg.architect_effort)
        out = []
        for d in data.get("lessons", []):
            text = str(d.get("lesson", "")).strip()
            if not text:
                continue
            kinds = [k for k in d.get("applies_to", []) if k in TASK_KINDS]
            role = d.get("role") if d.get("role") in ("architect", "editor", "router", "any") else "any"
            try:
                conf = max(0.0, min(1.0, float(d.get("confidence", 0.5))))
            except (TypeError, ValueError):
                conf = 0.5
            out.append(Lesson(text=text, applies_to=kinds, role=role, confidence=conf))
        return out

    # -- step execution ------------------------------------------------------

    def _touched_paths(self, result: EditResult, step: Step) -> list[str]:
        paths = [e.path.replace("\\", "/") for e in result.edits] + [n.path.replace("\\", "/") for n in result.new_files]
        return list(dict.fromkeys(p for p in paths if p))

    def execute_step(
        self,
        idx: int,
        total: int,
        step: Step,
        plan_summary: str,
        lessons: list[Lesson],
        verify: str | None,
        enforce_verify: bool,
        run_id: int,
    ) -> StepResult:
        self._step_cost = 0.0
        candidates = {"architect": self.cfg.architect_model, "editor": self.cfg.editor_model}
        choice = self.router.choose(step.kind, candidates, step.suggested_role)
        self.log(f"Step {idx + 1}/{total}: {step.title} [{step.kind}] -> {choice.role} ({choice.model}); {choice.reason}")

        examples = self.examples.retrieve(step.description, step.kind) if self.cfg.learning else []
        if examples:
            self.log(f"    retrieved {len(examples)} past example(s)")
        files = self.ws.read_many(step.files)
        feedback = ""
        attempts = 0
        verified = False
        last_diff = ""

        while attempts < self.cfg.max_attempts:
            attempts += 1
            result = self.edit(choice.model, step, plan_summary, files, examples, lessons, feedback)
            if result.status == "blocked":
                feedback = f"editor reported the step as blocked: {result.explanation}"
                self.log(f"    attempt {attempts}: blocked - {result.explanation[:200]}")
                break
            if result.status == "need_files" or (not result.edits and not result.new_files):
                feedback = "You returned no edits. Provide search/replace edits or new_files for this step."
                self.log(f"    attempt {attempts}: no edits returned")
                continue

            touched = self._touched_paths(result, step)
            snap = self.ws.snapshot(touched)
            try:
                apply_edits(self.ws.root, result.edits, result.new_files)
            except EditError as e:
                self.ws.restore(snap)
                feedback = f"Your edits could not be applied: {e}"
                self.log(f"    attempt {attempts}: edit failed to apply - {e}")
                continue
            last_diff = unified_diff(snap, self.ws.current(touched))
            files = self.ws.read_many(step.files + [p for p in touched if p not in step.files])

            verify_output: str | None = None
            verify_ok = True
            if verify:
                res = self.ws.run(verify)
                verify_ok = res.ok
                verify_output = res.output
                self.log(f"    verify: {'pass' if res.ok else f'FAIL (exit {res.code})'}")

            review = self.review(step, last_diff, verify_output)
            self.log(f"    review: {review.verdict}" + (f" - {review.feedback[:200]}" if review.verdict == "reject" else ""))

            if review.verdict == "reject":
                self.ws.restore(snap)
                files = self.ws.read_many(step.files)
                feedback = review.feedback or "; ".join(review.issues)
                continue
            if verify and not verify_ok and enforce_verify:
                self.ws.restore(snap)
                files = self.ws.read_many(step.files)
                feedback = f"The verification command `{verify}` failed after your edit:\n{verify_output}"
                continue

            verified = bool(verify) and verify_ok
            self.router.record(step.kind, choice.model, True)
            if self.cfg.learning and (verified or not verify):
                self.examples.add(step.kind, f"{step.title}. {step.description}", touched, last_diff, choice.model, run_id)
            return StepResult(idx, step.title, step.kind, choice.model, choice.role, attempts, True, verified, "", last_diff, self._step_cost)

        self.router.record(step.kind, choice.model, False)
        return StepResult(idx, step.title, step.kind, choice.model, choice.role, attempts, False, False, feedback, last_diff, self._step_cost)

    # -- top level -----------------------------------------------------------

    def run(self, task: str) -> RunResult:
        t0 = time.time()
        cfg = self.cfg
        self.log(f"architect={cfg.architect_model} editor={cfg.editor_model} learning={'on' if cfg.learning else 'off'}")

        lessons = self.memory.retrieve(task, k=5) if cfg.learning else []
        if lessons:
            self.log(f"recalled {len(lessons)} lesson(s) from past runs")
        lesson_ids = [l.id for l in lessons if l.id is not None]

        run_id = self.store.create_run(
            task=task, workspace=str(self.ws.root), architect_model=cfg.architect_model,
            editor_model=cfg.editor_model, learning=int(cfg.learning),
        )

        try:
            plan = self.plan(task, lessons)
        except (ProviderError, OrchestratorError) as e:
            self.store.finish_run(run_id, success=0, summary=f"planning failed: {e}", duration_s=time.time() - t0, cost_usd=self._cost)
            raise

        verify = cfg.verify_command if cfg.verify_command is not None else (plan.verify_command or self.ws.detect_test_command())
        verify = verify or None
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
            # A failing baseline means intermediate steps may legitimately leave tests red;
            # only the last step (or any step when the baseline was green) must make them pass.
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
            self.memory.feedback(lesson_ids, success)
            try:
                new_lessons = self.reflect(self._run_log(task, plan, results, success, failure_reason))
                self.memory.add(new_lessons, run_id)
                lessons_learned = [l.text for l in new_lessons]
                if lessons_learned:
                    self.log("lessons learned:")
                    for text in lessons_learned:
                        self.log(f"  - {text}")
            except ProviderError as e:
                self.log(f"reflection skipped: {e}")

        duration = time.time() - t0
        self.store.finish_run(
            run_id, task_kind=plan.task_kind, success=int(success), steps_total=len(steps),
            steps_ok=sum(1 for r in results if r.ok), cost_usd=self._cost, input_tokens=self._in,
            output_tokens=self._out, duration_s=duration, summary=plan.summary if success else failure_reason,
        )
        return RunResult(
            run_id=run_id, success=success, task_kind=plan.task_kind, plan_summary=plan.summary, steps=results,
            cost_usd=self._cost, input_tokens=self._in, output_tokens=self._out, duration_s=duration,
            verify_command=verify, final_verify_ok=final_ok, lessons_learned=lessons_learned, failure_reason=failure_reason,
        )

    @staticmethod
    def _run_log(task: str, plan: Plan, results: list[StepResult], success: bool, failure_reason: str) -> str:
        lines = [f"Task: {task}", f"Task kind: {plan.task_kind}", f"Plan: {plan.summary}", f"Outcome: {'success' if success else 'failure'}"]
        if failure_reason:
            lines.append(f"Failure: {failure_reason}")
        for r in results:
            lines.append(
                f"- Step {r.idx + 1} '{r.title}' [{r.kind}] executed by {r.role} ({r.model}): "
                f"{'accepted' if r.ok else 'FAILED'} after {r.attempts} attempt(s)"
                + (f"; verified" if r.verified else "")
                + (f"; last feedback: {r.feedback[:400]}" if r.feedback else "")
            )
        return "\n".join(lines)
