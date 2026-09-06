"""Step-level mechanics shared by the standalone orchestrator and the MCP server.

The engine knows how to plan, route, run one edit attempt (via search/replace
for API backends or in-place for agentic backends), verify, review, roll back,
record outcomes, and reflect. It holds no loop of its own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import prompts
from .edits import EditError, apply_edits, unified_diff
from .memory import Example, Examples, Memory
from .providers import LLM, ProviderError
from .router import Choice, Router
from .schemas import (
    CRITIQUE_SCHEMA,
    EDIT_SCHEMA,
    PLAN_SCHEMA,
    REFLECT_SCHEMA,
    REVIEW_SCHEMA,
    TASK_KINDS,
    Critique,
    EditResult,
    Lesson,
    Plan,
    Review,
    Step,
)
from .store import Store
from .workspace import CommandResult, Workspace

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
class Attempt:
    """One edit attempt: what changed, how to undo it, and why it failed if it did."""

    applied: bool
    error: str = ""
    blocked: bool = False
    snapshot: dict[str, str | None] = field(default_factory=dict)  # before-state of touched paths
    touched: list[str] = field(default_factory=list)
    diff: str = ""
    summary: str = ""
    concerns: str = ""  # the editor's disagreement with the step, if any
    cost_usd: float = 0.0
    verify_ok: bool | None = None  # filled in once verification has run on this attempt


class Engine:
    def __init__(self, llm: LLM, store: Store, workspace: Workspace, cfg: RunConfig, log: Logger | None = None):
        self.llm = llm
        self.store = store
        self.ws = workspace
        self.cfg = cfg
        self.log: Logger = log or (lambda _: None)
        self.router = Router(store, learning=cfg.learning)
        self.memory = Memory(store)
        self.examples = Examples(store)
        self.cost = 0.0
        self.input_tokens = 0
        self.output_tokens = 0

    # -- accounting ----------------------------------------------------------

    def _account(self, name: str, stats) -> float:
        cost = stats.cost_usd or 0.0
        self.cost += cost
        self.input_tokens += stats.input_tokens
        self.output_tokens += stats.output_tokens
        cost_s = f"${cost:.4f}" if stats.cost_usd is not None else "$?"
        self.log(f"    {name} <- {stats.model}: {stats.input_tokens}+{stats.output_tokens} tok, {cost_s}, {stats.latency_ms} ms")
        return cost

    def call(self, model: str, system: str, user: str, schema: dict[str, Any], name: str, effort: str) -> tuple[dict[str, Any], float]:
        data, stats = self.llm.complete_json(model, system, user, schema, name, effort=effort)
        return data, self._account(name, stats)

    def effort_for(self, model: str) -> str:
        return self.cfg.architect_effort if model == self.cfg.architect_model else self.cfg.editor_effort

    @property
    def two_models(self) -> bool:
        return bool(self.cfg.editor_model) and self.cfg.editor_model not in ("self", self.cfg.architect_model)

    def reviewer_for(self, author: str) -> str:
        """The model that did not write the diff reviews it; with one model there is no choice."""
        if self.two_models and author == self.cfg.architect_model:
            return self.cfg.editor_model
        return self.cfg.architect_model

    # -- memory ----------------------------------------------------------------

    def recall(self, task: str, k: int = 5) -> list[Lesson]:
        return self.memory.retrieve(task, k=k) if self.cfg.learning else []

    def examples_for(self, step: Step, k: int = 2) -> list[Example]:
        return self.examples.retrieve(step.description, step.kind, k=k) if self.cfg.learning else []

    # -- planning --------------------------------------------------------------

    def plan(self, task: str, lessons: list[Lesson], critique: str = "") -> Plan:
        overview = self.ws.overview()
        files: dict[str, str] = {}
        data: dict[str, Any] = {}
        for _ in range(self.cfg.max_file_rounds + 1):
            user = prompts.build_plan_prompt(task, overview, files, lessons, critique)
            data, _ = self.call(self.cfg.architect_model, prompts.ARCHITECT_SYSTEM, user, PLAN_SCHEMA, "plan", self.cfg.architect_effort)
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

    def critique(self, task: str, plan: Plan, verify: str | None) -> tuple[Critique, float]:
        """The Editor reads the plan before anything is edited. Only meaningful with two models."""
        files = self.ws.read_many(sorted({f for s in plan.steps for f in s.files})[:8], max_chars_per_file=6000)
        user = prompts.build_critique_prompt(task, plan, verify or "", self.ws.overview(), files)
        data, cost = self.call(self.cfg.editor_model, prompts.CRITIC_SYSTEM, user, CRITIQUE_SCHEMA, "critique", self.cfg.editor_effort)
        return Critique.from_dict(data), cost

    def route(self, step: Step) -> Choice:
        candidates = {"architect": self.cfg.architect_model, "editor": self.cfg.editor_model}
        return self.router.choose(step.kind, candidates, step.suggested_role, pinned=step.pin)

    # -- editing ---------------------------------------------------------------

    def edit(self, model: str, step: Step, plan_summary: str, examples: list[Example], lessons: list[Lesson], feedback: str) -> Attempt:
        if self.llm.can_edit_in_place(model):
            return self._edit_in_place(model, step, plan_summary, examples, lessons, feedback)
        return self._edit_search_replace(model, step, plan_summary, examples, lessons, feedback)

    def _edit_in_place(self, model: str, step: Step, plan_summary: str, examples: list[Example], lessons: list[Lesson], feedback: str) -> Attempt:
        before = self.ws.snapshot_tree()
        prompt = prompts.build_agent_edit_prompt(step, plan_summary, examples, lessons, feedback)
        res, stats = self.llm.edit_in_place(model, self.ws.root, prompt, self.effort_for(model))
        cost = self._account("edit(agent)", stats)
        changed = self.ws.changed_since(before)
        if not changed:
            return Attempt(False, "the editor agent finished without changing any file: " + res.summary[:400], summary=res.summary, cost_usd=cost)
        snap: dict[str, str | None] = {p: before.get(p) for p in changed}
        diff = unified_diff(snap, changed)
        concerns = ""
        marker = res.summary.lower().find("concerns:")
        if marker >= 0:
            concerns = res.summary[marker + len("concerns:"):].strip()
        return Attempt(True, snapshot=snap, touched=list(changed), diff=diff, summary=res.summary, concerns=concerns, cost_usd=cost)

    def _edit_search_replace(self, model: str, step: Step, plan_summary: str, examples: list[Example], lessons: list[Lesson], feedback: str) -> Attempt:
        effort = self.effort_for(model)
        files = self.ws.read_many(step.files)
        result = EditResult("blocked", [], "no response", [], [])
        cost = 0.0
        for _ in range(self.cfg.max_file_rounds + 1):
            user = prompts.build_edit_prompt(step, plan_summary, files, examples, lessons, feedback)
            data, c = self.call(model, prompts.EDITOR_SYSTEM, user, EDIT_SCHEMA, "edit", effort)
            cost += c
            result = EditResult.from_dict(data)
            wanted = [f for f in result.files_needed if f and f not in files]
            if result.status == "need_files" and wanted:
                self.log(f"    editor asked for {len(wanted)} file(s): {', '.join(wanted[:6])}")
                files.update(self.ws.read_many(wanted))
                continue
            break

        if result.status == "blocked":
            return Attempt(False, f"editor reported the step as blocked: {result.explanation}", blocked=True, cost_usd=cost)
        if result.status == "need_files" or (not result.edits and not result.new_files):
            return Attempt(False, "You returned no edits. Provide search/replace edits or new_files for this step.", cost_usd=cost)

        touched = list(dict.fromkeys(
            p.replace("\\", "/") for p in ([e.path for e in result.edits] + [n.path for n in result.new_files]) if p
        ))
        snap = self.ws.snapshot(touched)
        try:
            apply_edits(self.ws.root, result.edits, result.new_files)
        except EditError as e:
            self.ws.restore(snap)
            return Attempt(False, f"Your edits could not be applied: {e}", cost_usd=cost)
        diff = unified_diff(snap, self.ws.current(touched))
        return Attempt(True, snapshot=snap, touched=touched, diff=diff, summary=result.explanation, concerns=result.concerns, cost_usd=cost)

    def rollback(self, attempt: Attempt) -> None:
        if attempt.snapshot:
            self.ws.restore(attempt.snapshot)

    # -- verification and review --------------------------------------------------

    def verify(self, command: str | None) -> CommandResult | None:
        if not command:
            return None
        res = self.ws.run(command)
        self.log(f"    verify: {'pass' if res.ok else f'FAIL (exit {res.code})'}")
        return res

    def resolve_verify(self, plan_verify: str) -> str | None:
        if self.cfg.verify_command is not None:
            return self.cfg.verify_command or None
        return plan_verify or self.ws.detect_test_command()

    def review(self, step: Step, diff: str, verify_output: str | None, reviewer: str | None = None) -> tuple[Review, float]:
        model = reviewer or self.cfg.architect_model
        user = prompts.build_review_prompt(step, diff, verify_output)
        data, cost = self.call(model, prompts.REVIEWER_SYSTEM, user, REVIEW_SCHEMA, "review", self.effort_for(model))
        return Review.from_dict(data), cost

    # -- outcomes ------------------------------------------------------------------

    def record_success(self, step: Step, model: str, attempt: Attempt, verified: bool, has_verify: bool, run_id: int | None) -> None:
        self.router.record(step.kind, model, True)
        if self.cfg.learning and (verified or not has_verify):
            self.examples.add(step.kind, f"{step.title}. {step.description}", attempt.touched, attempt.diff, model, run_id)

    def record_failure(self, step: Step, model: str) -> None:
        self.router.record(step.kind, model, False)

    # -- reflection ----------------------------------------------------------------

    def reflect(self, run_log: str, model: str | None = None) -> list[Lesson]:
        model = model or self.cfg.architect_model
        data, _ = self.call(model, prompts.REFLECT_SYSTEM, prompts.build_reflect_prompt(run_log), REFLECT_SCHEMA, "reflect", self.effort_for(model))
        return lessons_from_dicts(data.get("lessons", []))

    def remember(self, lessons: list[Lesson], run_id: int | None) -> list[int]:
        return self.memory.add(lessons, run_id) if self.cfg.learning else []

    def credit_lessons(self, lesson_ids: list[int], success: bool) -> None:
        if self.cfg.learning:
            self.memory.feedback(lesson_ids, success)


def dedupe_lessons(lessons: list[Lesson]) -> list[Lesson]:
    """Two minds reflecting on one run often say the same thing; keep the first wording."""
    seen: set[str] = set()
    out: list[Lesson] = []
    for l in lessons:
        key = " ".join(l.text.lower().split())
        if key and key not in seen:
            seen.add(key)
            out.append(l)
    return out


def lessons_from_dicts(items: list[dict[str, Any]]) -> list[Lesson]:
    out: list[Lesson] = []
    for d in items:
        text = str(d.get("lesson", d.get("text", ""))).strip()
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


__all__ = [
    "Attempt", "Engine", "OrchestratorError", "ProviderError", "RunConfig", "dedupe_lessons", "lessons_from_dicts",
]
