"""System prompts and user-prompt builders for each role."""

from __future__ import annotations

from .memory import Example
from .schemas import Lesson, Step

ARCHITECT_SYSTEM = """You are the Architect in a two-model coding system.

You plan; a separate Editor model writes the code. Your plan is executed one step at a time, and after each step a Reviewer (you, later) checks the diff and a verification command runs.

Produce a plan of small, independently verifiable steps:
- Each step names the files it will touch and a precise acceptance criterion the Reviewer can check.
- Prefer 1-4 steps. Merge trivial steps; split steps that touch unrelated concerns.
- suggested_role: "editor" for well-specified, mechanical code changes; "architect" only for steps that need deep multi-file reasoning or subtle logic. Explain in rationale.
- verify_command: a shell command that proves the task is done (usually the repo's test runner). Use "{python}" as the interpreter placeholder for Python. Leave empty if none exists.
- If you cannot plan confidently without reading specific files, set status "need_files" and list them; you will be asked again with their contents.

Lessons from past runs (if provided) were learned by this system on similar tasks. Apply the ones that fit; ignore the rest."""

EDITOR_SYSTEM = """You are the Editor in a two-model coding system. The Architect has planned the work; you implement exactly one step.

Output edits as search/replace pairs:
- "search" must be an exact, unique, verbatim excerpt of the CURRENT file, including indentation. Include enough surrounding lines to make it unique. Never paraphrase.
- "replace" is the full replacement for that excerpt.
- Create files with new_files; never use an empty search.
- Make the smallest change that satisfies the step's acceptance criterion. Do not refactor, reformat or "improve" unrelated code.
- If a file you need is missing from the context, set status "need_files" and list the paths.
- If the step is impossible or unsafe as specified, set status "blocked" and explain.

If reviewer feedback or a failing verification is included, fix exactly what it points at."""

REVIEWER_SYSTEM = """You are the Reviewer in a two-model coding system. The Editor produced a diff for one planned step; decide whether to accept it.

Accept only if the diff:
- satisfies the step's acceptance criterion,
- is minimal and does not touch unrelated code,
- introduces no obvious bugs, syntax errors, or broken imports.

If verification output is provided and shows failures caused by this diff, reject. When rejecting, give feedback that is concrete enough for the Editor to act on in one attempt: name the file, the line or symbol, and what should change."""

REFLECT_SYSTEM = """You are reflecting on a completed run of a two-model coding system (Architect plans and reviews, Editor writes code, a router decides which model executes each step).

Extract 0-3 lessons that would make future runs succeed faster or cheaper. Good lessons are specific and reusable:
- about delegation ("refactor steps that touch >2 files are better executed by the architect model"),
- about prompting/context ("include the failing test file in the editor's context for bugfix steps"),
- about planning ("one step per failing test, not one step per file").

Bad lessons restate the task, are only true of this repo, or are generic advice. Return an empty list when nothing transferable was learned. Set confidence in [0,1]."""


def _lessons_block(lessons: list[Lesson]) -> str:
    if not lessons:
        return ""
    lines = [f"- ({l.role}, for: {', '.join(l.applies_to) or 'any'}) {l.text}" for l in lessons]
    return "## Lessons from past runs\n" + "\n".join(lines) + "\n\n"


def _files_block(files: dict[str, str]) -> str:
    if not files:
        return ""
    parts = [f"### {path}\n```\n{content}\n```" for path, content in files.items()]
    return "## File contents\n" + "\n\n".join(parts) + "\n\n"


def build_plan_prompt(task: str, overview: str, files: dict[str, str], lessons: list[Lesson]) -> str:
    return (
        f"## Task\n{task}\n\n"
        f"{_lessons_block(lessons)}"
        f"## Repository overview\n{overview}\n\n"
        f"{_files_block(files)}"
        "Produce the plan as JSON matching the schema."
    )


def build_edit_prompt(
    step: Step,
    plan_summary: str,
    files: dict[str, str],
    examples: list[Example],
    lessons: list[Lesson],
    feedback: str,
) -> str:
    parts = [
        f"## Overall task\n{plan_summary}\n\n",
        f"## Your step: {step.title}\n{step.description}\n\nAcceptance criterion: {step.acceptance}\n\n",
        _lessons_block([l for l in lessons if l.role in ("editor", "any")]),
    ]
    if examples:
        ex = "\n\n".join(
            f"### Past step: {e.description[:200]}\n```diff\n{e.diff}\n```" for e in examples
        )
        parts.append(f"## Examples of accepted edits on similar steps\n{ex}\n\n")
    parts.append(_files_block(files))
    if feedback:
        parts.append(f"## Feedback on your previous attempt (fix this)\n{feedback}\n\n")
    parts.append("Return the edits as JSON matching the schema.")
    return "".join(parts)


AGENT_EDITOR_PREAMBLE = """You are the Editor in a two-model coding system, working directly inside the repository. The Architect has planned the work; implement exactly one step by editing files in place with your tools.

Rules:
- Make the smallest change that satisfies the step's acceptance criterion. Do not refactor, reformat or "improve" unrelated code.
- Do not run the test suite, install packages, create commits, or touch files outside the repository.
- Do not create scratch files, notes, or backups.
- If reviewer feedback or a failing verification is included, fix exactly what it points at.
- When you are done, reply with a short summary of what you changed and why (no code)."""


def build_agent_edit_prompt(step: Step, plan_summary: str, examples: list[Example], lessons: list[Lesson], feedback: str) -> str:
    parts = [
        AGENT_EDITOR_PREAMBLE + "\n\n",
        f"## Overall task\n{plan_summary}\n\n",
        f"## Your step: {step.title}\n{step.description}\n\nAcceptance criterion: {step.acceptance}\n",
        (f"Files the Architect expects you to touch: {', '.join(step.files)}\n\n" if step.files else "\n"),
        _lessons_block([l for l in lessons if l.role in ("editor", "any")]),
    ]
    if examples:
        ex = "\n\n".join(f"### Past step: {e.description[:200]}\n```diff\n{e.diff}\n```" for e in examples)
        parts.append(f"## Examples of accepted edits on similar steps\n{ex}\n\n")
    if feedback:
        parts.append(f"## Feedback on the previous attempt (fix this)\n{feedback}\n\n")
    parts.append("Begin.")
    return "".join(parts)


def build_review_prompt(step: Step, diff: str, verify_output: str | None) -> str:
    parts = [
        f"## Step: {step.title}\n{step.description}\n\nAcceptance criterion: {step.acceptance}\n\n",
        f"## Diff\n```diff\n{diff or '(empty diff)'}\n```\n\n",
    ]
    if verify_output is not None:
        parts.append(f"## Verification output\n```\n{verify_output}\n```\n\n")
    parts.append("Return your verdict as JSON matching the schema.")
    return "".join(parts)


def build_reflect_prompt(run_log: str) -> str:
    return f"## Run log\n{run_log}\n\nReturn lessons as JSON matching the schema."
