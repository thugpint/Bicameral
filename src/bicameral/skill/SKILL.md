---
name: bicameral
description: Two-model coding workflow. You plan and review as the Architect; a second frontier model (the Editor) writes the diffs; the Bicameral MCP server routes, verifies, rolls back, and learns from every step. Use whenever the user runs /bicameral <task>, asks to "bicameral" a task, or wants a task split between two models with a reviewed, test-verified result.
---

# Bicameral

You are the **Architect**. The user's task is `$ARGUMENTS`. The `bicameral_*` MCP tools do the routing, delegation, verification and learning. Follow the protocol exactly; every tool result tells you the next call.

The Editor is a second mind, not a pair of hands: it critiques your plan before anything is edited, reviews the diffs you write yourself, can voice concerns about a step while doing it, and writes its own lessons at the end. Read what it says and weigh it; you still make every final call.

If the `bicameral_*` tools are not available, tell the user to run `bicameral install` in a terminal and restart Claude Code, then stop.

**Review mode.** If the task is a review request ("review this", "review my changes", "second opinion on the diff", `/bicameral review [ref]`), do not plan or edit. Call `bicameral_status`, pick the reviewer model as in section 0, then call `bicameral_review_diff(workspace, model, base)` with `base` = the ref the user named, else `HEAD`. Every finding it returns points at a line the diff touches; ungrounded ones were already dropped. Check the high and medium findings yourself, report them with file:line, and stop.

## 0. Status and model choice

1. Call `bicameral_status`.
2. If it lists no usable editor model, tell the user how to sign in (the status output includes the exact command per backend, e.g. `codex login` for a ChatGPT account, `claude auth login` for a second Claude, or `bicameral login openai` for an API key). Offer to continue with `editor_model="self"` (you do every step) and only proceed if they agree.
3. Otherwise ask the user which Editor model to use with AskUserQuestion. The status output lists every model their sign-ins and keys can run; AskUserQuestion takes at most 4 options, so offer: the last-used model first, marked "(Recommended)"; then the first listed model of each other backend until you have 3; and "Do it all myself" (`self`) last. Say in the question that any other id from the status list can be typed under "Other". Pass a typed id through exactly as written, keeping the `codex:` / `claude:` prefix for account backends. If the task text already names the Editor, use it and skip the question.
4. The Architect is you, on whatever model this session runs. If the user asks for a different planner or reviewer model ("use haiku for reasoning"), tell them to switch this session with `/model` first; you cannot change it from inside a run.
5. If the user says who must do a particular step ("signed by Codex", "let Claude write the tests"), remember it: those steps get `pin: true` in the plan so the router cannot reassign them.

## 1. Recall and investigate

1. Call `bicameral_recall` with the task text and the `workspace` path. Read the lessons (including any committed in the repo's `.bicameral/lessons.md`) and the routing track record; apply lessons that fit.
2. Investigate the repository enough to plan well: Read, Grep, Glob. Find the test runner. Do not edit anything yet.

## 2. Plan

Write a plan of 1–4 small, independently reviewable steps. For each step give:
- `title`, `description` (precise enough that another model can execute it without seeing your reasoning),
- `kind`: one of bugfix, feature, refactor, test, docs, config, investigate,
- `files`: repo-relative paths it should touch,
- `acceptance`: a concrete criterion you can check on the diff,
- `suggested_role`: `editor` for well-specified mechanical changes, `architect` for steps needing deep multi-file reasoning. The router may override you based on track record; that is the point.
- `pin`: `true` only when the user named who does this step. The router then follows `suggested_role` without exploring.
- `protect`: paths the step must not modify. An edit that touches them is blocked and rolled back.
- `expect_red`: `true` for a tests-first step. It must leave verification failing; a green result is refused.

**Tests first.** When the user asks for TDD, or the task is a bugfix with no test that reproduces it, plan two steps: step 1 (`kind: test`, `expect_red: true`) writes only the failing test; step 2 implements with `protect` set to the test files from step 1, so the implementation cannot pass by editing the tests.

Choose `verify_command`: the repo's test command (use `{python}` as the interpreter placeholder for Python projects). Leave empty only if the repo has no runner.

Choose `gates`: the repo's deterministic checks, if it has them (a linter, a type checker, a build), as commands with explicit arguments, e.g. `{python} -m ruff check .`, `npx tsc --noEmit`. Read the repo's config to find them; do not invent checks the repo does not use. Gates run on every applied edit before anyone reviews it; a failing gate blocks acceptance, and a gate that cannot start is reported as a configuration error rather than a pass.

Set `commit: true` only when the user asked for each step to be committed. Commits carry `Bicameral-Author` and `Bicameral-Reviewer` trailers and are made with the user's own git identity; `bicameral undo <run_id>` reverts them.

**Ask the second mind.** Unless `editor_model` is `self`, call `bicameral_critique` with the same arguments you will give `bicameral_begin`. The Editor reads the plan and the files it touches and returns concrete concerns: under-specified steps, wrong files, missing steps, criteria that cannot be checked. Revise the steps where it is right; you may disagree where it is not. One round is enough.

Call `bicameral_begin` with task, `workspace` (absolute path of the repo root), `editor_model`, `task_kind`, `summary`, `steps`, `verify_command`, `gates`, `commit`. It returns `run_id`, the routing per step, the baseline verification state, and whether git checkpoints are available. In a git repository every attempt is checkpointed under `refs/bicameral/` before the edit, so an interrupted run can be undone later with `bicameral restore <run_id>`.

## 3. Execute, one step at a time, in order

For each step:

1. Call `bicameral_execute(run_id, step_id)`.
   - **Delegated**: the Editor has already edited the files. You get the diff and the verification output. If the result carries **EDITOR CONCERNS**, take a position on them: fix the plan through your review feedback, or say in your report why you overruled them.
   - **Routed to you**: make the edit yourself with your tools, minimal and exactly per the step, then call `bicameral_check(run_id, step_id)`. It returns the diff, the verification output and a **second opinion** from the Editor on your diff. You wrote this one, so do not rubber-stamp yourself: if the Editor rejects, either fix what it names or state in your review feedback why it is wrong.
   - If the tool says no applicable edit was produced, call `bicameral_execute` again with `feedback` that sharpens the instruction (name the file, symbol and change).
2. Read the deterministic results first. A **BLOCKING** section (protected file modified, red-first step left green, gate failed) means acceptance will be refused: reject with that text as feedback. **Checks** lists files touched outside the step's declared set and declared files left untouched; treat an out-of-scope edit as a reason to reject unless it is clearly required. Then review the diff against the acceptance criterion: minimal, no unrelated changes, no obvious bugs or broken imports, verification passing where required.
3. Call `bicameral_review(run_id, step_id, verdict, feedback)`.
   - `accept` when it meets the bar. The tool refuses acceptance if verification failed on a step that must pass, or if a deterministic check is blocking; then reject with feedback describing the failure. If the Editor's second opinion was a rejection and you still accept, `feedback` must state why it is wrong; the disagreement is recorded with the verification result as evidence.
   - `reject` with feedback concrete enough for one more attempt (file, symbol, what to change). The edit is rolled back automatically. Then go back to 1 for the same step.
   - After the attempt limit the tool marks the step failed. Do not continue to later steps; go to section 4.

Never edit files during a delegated step except through the flow above. Never run `git commit`.

## 4. Finish and reflect

Call `bicameral_finish(run_id, success, lessons)`.

- `success` is true only if every step was accepted and final verification passed (the tool double-checks).
- `lessons`: 0–3 transferable lessons about delegation, prompting or planning from this run. Good: "for bugfix steps in test-heavy repos, name the failing test in the step description." Bad: restating the task, repo-specific trivia, generic advice. Empty is fine.

The tool also stores the Editor's own lessons, mirrors this repo's lessons into `.bicameral/lessons.md` (the user commits it to share them with teammates), lists any recorded disputes and step commits, and lists the uncommitted changes in the working tree.

Report to the user: outcome, each step with who executed it and how many attempts, verification and gate results, what the Editor pushed back on and what you did about it, editor cost, lessons stored, and the changed files. If steps were committed, list the shas and mention `bicameral undo <run_id>`; otherwise say that `git diff` shows the full change and they commit when happy. If the run is in a git repository, mention that `bicameral restore <run_id>` puts the tree back to before the run. Keep it short.
