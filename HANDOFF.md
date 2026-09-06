# Bicameral — handoff

Written 2026-09-06 at commit `a80acf0` on `main`. Everything below is what is actually true of the code, not what was intended.

## 1. What this is, in three sentences

Bicameral splits a coding task between two frontier models: an **Architect** that plans and reviews, and an **Editor** that writes the diffs. Its primary form is a Claude Code **skill** (`/bicameral`) plus a **local MCP server**, where Claude Code itself is the Architect on the user's Anthropic account and the Editor runs on the user's ChatGPT or Claude account through the Codex and Claude CLIs. The part that makes it more than "the split everyone builds" is the measurement: a reviewer gate with test verification and rollback, per-step outcome logging, a learned router, scored lessons, retrieved examples, and an eval harness with a learning-off baseline.

Repo: https://github.com/Devilz06/Bicameral (public, MIT). The owner publishes as **devilz06 only**. Every commit must use `user.name devilz06` / `user.email devilz06@users.noreply.github.com` (already set as repo-local config) and no real name or personal email may appear anywhere in the tree. Grep before every push.

## 2. State of the build

| Area | Status |
|---|---|
| Standalone loop (`bicameral run`, evals, TUI Run tab) | working, tested offline |
| MCP server + `/bicameral` skill | working, tested offline through the `Bicameral` class; tool listing verified through the real `MCPServer` |
| Headless Claude Code backend (`claude -p`) | request/response envelope confirmed live on this machine; full call could not complete because this machine's `claude` OAuth token was expired |
| Codex CLI backend (`codex exec`) | **only tested through a fake subprocess**; `codex` is not installed on the dev machine (a ChatGPT sign-in exists in `~/.codex/auth.json`) |
| Anthropic / OpenAI API backends | written against the current SDK surfaces (`anthropic` 1.4, `openai` 3.8); not exercised with real keys |
| TUI | mounts, tabs switch, populated with sample data and inspected visually (overview + setup tabs) |
| Install into Claude Code (`bicameral install`) | writes the skill and runs `claude mcp add`; tested with a fake runner. **Not run on the owner's machine**; nothing is registered in their Claude Code yet |
| Tests | 54 pass, ~40 s, all offline (`.venv/Scripts/python -m pytest -q`) |

No live end-to-end run of `/bicameral` inside Claude Code has happened yet. That is the first thing to do (section 8).

## 3. Architecture

```
Claude Code session (Architect, user's Anthropic account)
   │  /bicameral skill  (src/bicameral/skill/SKILL.md)
   ▼
MCP server  (src/bicameral/mcp_server.py, stdio, launched by Claude Code)
   │  Bicameral class: status / recall / begin / execute / check / review / finish / stats
   ▼
Engine  (src/bicameral/engine.py)  ── shared with the standalone Orchestrator
   ├─ Router      Thompson-sampling bandit over (step kind, model)        router.py
   ├─ Memory      lessons: BM25 retrieval + track-record score, pruning   memory.py
   ├─ Examples    accepted+verified diffs as few-shot examples             memory.py
   ├─ edit        search/replace (API backends) or in-place (CLI agents)   edits.py / workspace.py
   ├─ verify      runs the repo's test command                             workspace.py
   └─ Store       SQLite: runs, steps, routing, lessons, examples, eval_runs   store.py
Providers  (src/bicameral/providers/)
   anthropic_provider  API key or `ant auth login` OAuth profile (zero-arg client)
   openai_provider     API key, Responses API, strict JSON schema
   claude_cli          `claude -p --output-format json` on the user's account; in-place edits with acceptEdits
   codex_cli           `codex exec` on the user's ChatGPT account; in-place edits with --full-auto
auth.py     which backends are usable right now + the exact sign-in command for each
install.py  writes ~/.claude/skills/bicameral/SKILL.md, runs `claude mcp add -s user bicameral -- <python> -m bicameral.mcp_server`
tui/        Textual app (app.py + app.tcss), 7 tabs
cli.py      `bicameral` = TUI; subcommands install/uninstall/mcp/login/logout/status/models/run/eval/stats/lessons
evals/      harness + 3 fixture tasks (bugfix, feature, refactor), each a repo with failing tests
```

Two loops share the Engine:

- **Standalone** (`orchestrator.py`): both roles are model calls. Used by `bicameral run`, `bicameral eval`, and the TUI. Needs an architect model id (e.g. `claude:opus` or `claude-opus-5`).
- **Hosted** (`mcp_server.py`): the Architect is the live Claude Code session, recorded under the model id `claude-code`. The server only ever calls the Editor.

## 4. The MCP protocol (what the skill drives)

Order is enforced server-side; every tool reply names the next call.

1. `bicameral_status` — backends, sign-in state, editor models usable now, last editor, history.
2. `bicameral_recall(task)` — relevant lessons, similar accepted edits, routing track record.
3. `bicameral_begin(task, workspace, editor_model, task_kind, summary, steps[], verify_command, learning)` — validates the plan, creates the run, routes every step (bandit), runs baseline verification. `editor_model="self"` routes everything to the Architect. Returns `run_id` and the routing.
4. Per step, in order:
   - `bicameral_execute(run_id, step_id, feedback="")` — delegated: Editor edits, server diffs, runs verification, returns diff + output. Self-routed: server snapshots the tree and tells Claude to edit, then `bicameral_check(run_id, step_id)` diffs and verifies.
   - `bicameral_review(run_id, step_id, verdict, feedback)` — `accept` records success, stores the example, writes the step row. `reject` rolls back to the snapshot and counts an attempt. Acceptance is **refused** when verification failed on a step that must pass. After `max_attempts` (3) the step is failed and later steps are blocked.
5. `bicameral_finish(run_id, success, lessons[])` — rolls back any unreviewed edit, runs final verification (can flip `success` to false), stores lessons, credits/debits the lessons that were in context, closes the run.

Invariants worth keeping:

- A step must pass verification if the baseline was green **or** it is the last step. With a red baseline, intermediate steps may leave tests red.
- Every applied edit has a snapshot; nothing is ever left applied without a review (finish rolls back leftovers).
- Session state lives in memory in the server process, keyed by `run_id`. If Claude Code restarts the server mid-run, the run is orphaned in the DB with `success NULL`. Acceptable for now; see gaps.

## 5. Learning, and how to prove it works

- **Router** (`router.py`): arm = (step kind, model id). Beta(1+succ, 1+fail), the Architect's suggested role gets `SUGGESTION_PRIOR` (1.0) pseudo-successes. On a fresh install it follows the suggestion ~75% of the time and explores otherwise. Outcome = reviewer accepted AND (verified or no verify command). Tests that need determinism monkeypatch `SUGGESTION_PRIOR` to 1000.
- **Lessons** (`memory.py`): written by the Architect at the end of a run (standalone: reflect call; hosted: the skill asks Claude for 0–3). Retrieved by BM25 relevance + 0.3 × track record. In-context lessons get +1.0 on a successful run, −0.5 on failure. `prune()` deletes at ≤ −2.0.
- **Examples**: only diffs that were accepted *and* verified (or had no verify command). Retrieved by BM25 with a +1 bonus for the same step kind, shown to the Editor.
- **Measuring**: `bicameral eval --baseline --runs N` then `bicameral eval --runs N`, then `bicameral stats` (or the TUI Overview/Evals tabs). `eval_runs` rows carry `learning` 0/1 so the comparison is direct. The trend query batches learning-on runs in fives.

The headline claim ("gets measurably better") is **unproven** until someone runs the suite with real backends. All numbers so far come from scripted fakes.

## 6. Backends and sign-in, with the sharp edges

| Backend | Detection | Sign-in | Notes |
|---|---|---|---|
| `claude-cli` | `shutil.which("claude")` + `claude auth status` JSON (`loggedIn`, `authMethod`) | `claude auth login` | Prompt goes over **stdin** (Windows argv limit). Completions: `-p --output-format json --no-session-persistence --tools "" --model X [--system-prompt] [--json-schema] [--effort]`. Edits: `--permission-mode acceptEdits --allowedTools Read Edit Write MultiEdit Glob Grep LS`, cwd = repo. Env vars `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` are stripped so it runs from inside a Claude Code session. Reply envelope: `{is_error, result, structured_output?, total_cost_usd, usage}`. |
| `codex-cli` | `shutil.which("codex")` + `~/.codex/auth.json` (`OPENAI_API_KEY` or `tokens.access_token`; `auth_mode` "chatgpt") | `codex login` after `npm i -g @openai/codex` | **Unverified flags**: `exec --skip-git-repo-check -C DIR --sandbox read-only -m MODEL -o FILE [--output-schema FILE] [-c model_reasoning_effort="x"]`, edits use `--full-auto`. Prompt over stdin, no system-prompt flag so it is prepended. First live run should confirm each of these. |
| `anthropic` | key in env/file, or `%APPDATA%\Anthropic\credentials\*.json` (`~/.config/anthropic/credentials` elsewhere) | `bicameral login anthropic` or `ant auth login` | Structured output via `output_config.format`, effort via `output_config.effort` (not on Haiku). Refusal stop reason raises `ProviderError`. No fallbacks param. |
| `openai` | key in env/file | `bicameral login openai` | Responses API, `text.format` json_schema strict, `reasoning.effort` only for o-series/gpt-5 ids. |

Model ids: `claude:<alias>` and `codex:<model>` select the CLI backends; bare ids go to the APIs. `models.find()` infers provider and capability flags for unknown ids. Pricing is a snapshot; account backends report $0 (subscription).

On the owner's machine right now: `claude` is installed but its OAuth token was expired; `codex` is not installed; a ChatGPT sign-in exists in `~/.codex`; no API keys stored.

## 7. Developer setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e . pytest
.venv/Scripts/python -m pytest -q
```

- `pyproject.toml` sets `testpaths = ["tests"]` on purpose: the eval fixtures under `src/bicameral/evals/tasks/*/fixture/tests` fail by design and must not be collected.
- Fake backends live in `tests/fake.py`. `FakeProvider` scripts replies by schema name (`plan`, `edit`, `review`, `reflect`); `test_mcp.py::FakeAgent` is an in-place editor. Model ids `claude-test-architect` / `gpt-test-editor` map to the anthropic/openai slots.
- TUI screenshots: the session's scratchpad had a `shot.py` that seeds a `Store(":memory:")`, monkeypatches `auth.backends` and `install.state`, runs `app.run_test(size=(140, 46))` and calls `app.save_screenshot("x.svg")`. Recreate it in ~40 lines if needed; view the SVG in a browser.
- Data lives in `~/.bicameral/` (`bicameral.db`, `config.json`, `credentials.json`), overridable with `BICAMERAL_HOME`. Tests set it to a temp dir automatically (`tests/conftest.py`).
- `Store` opens SQLite with `check_same_thread=False` because the TUI and MCP server call it from worker threads.
- Textual gotcha: `App` has private methods like `_prune`; a handler with that name silently breaks shutdown. Prefix handlers distinctly.

## 8. What to do next, in order

1. **First live run.** On a machine with `claude` signed in: `pip install -e .`, `bicameral install`, restart Claude Code, open a small repo with tests, run `/bicameral <trivial task>` with `editor_model="self"`. This proves the skill → MCP → engine path with zero external dependencies. Then repeat with `claude:sonnet` as Editor.
2. **Codex live check.** `npm i -g @openai/codex`, `codex login`, then `bicameral run "…" --architect claude:opus --editor codex:gpt-5-codex --path <fixture copy>`. Fix any flag drift in `providers/codex_cli.py` (both `complete` and `edit_in_place`). Check that `-o` captures the final message and that `--output-schema` exists in the installed version; if not, drop it and rely on the JSON instruction plus the parser's retry.
3. **Baseline numbers.** `bicameral eval --baseline --runs 3` then `bicameral eval --runs 3`, same models. Commit the `stats` output into the README as the first real measurement, even if it is flat. Add 5–10 more fixture tasks; three is too few for a signal.
4. **Run resumption.** Persist MCP session state (plan, routes, attempts, pending snapshot) to the DB so a server restart mid-run can resume or at least clean up. Today an orphaned run stays `success NULL` and any applied-but-unreviewed edit stays on disk.
5. **Skill polish after real use.** Watch how Claude follows `SKILL.md`. Likely tweaks: how it phrases the model question, whether it over-plans, whether it needs a nudge to write lessons that are actually transferable.
6. **Embeddings for retrieval** once there are enough lessons/examples for BM25 to feel weak. The seam is `retrieval.py`; `Memory.retrieve` and `Examples.retrieve` are the only callers.
7. **TUI live views**: a "current run" panel fed by the MCP server (needs a small IPC or the DB polled), and cost tiles that read `claude -p`'s `total_cost_usd` for account backends.

## 9. Decisions already made (do not relitigate without a reason)

- **Claude Code is the Architect in the hosted flow**, not a second API call. It uses the user's subscription, keeps the interactive review in the user's session, and needs no Anthropic key.
- **Account sign-in is first-class; API keys are optional.** Neither vendor offers public third-party OAuth for their APIs, so "login with your account" is implemented by driving the vendors' own CLIs (`claude`, `codex`) with the user's own login.
- **Search/replace for API backends, in-place editing for CLI agents.** Both end in the same snapshot → diff → review → rollback path, so the reviewer and the learning loop see identical shapes.
- **The bandit explores.** It will sometimes send a step to the non-suggested side on purpose. That costs a little and is what makes routing learnable.
- **Verification enforcement rule** (green baseline or last step) exists because multi-step plans on a red baseline would otherwise fail every intermediate step.
- **BM25, not embeddings**, to stay offline and dependency-free until the corpus justifies more.
- **No REPL.** The TUI replaced it; the CLI subcommands remain for scripting.

## 10. Known gaps and risks

- Codex flags unverified (section 6).
- Whole-tree snapshots (`Workspace.snapshot_tree`) read every text file under 400 KB, capped at 4000 files. Fine for normal repos; large monorepos will be slow and should get a git-aware fast path (`git status --porcelain` + `git diff`).
- `claude auth status` is shelled out on every `bicameral_status` / TUI refresh (~1–2 s). Cache it if it becomes annoying.
- The TUI's interactive sign-in uses `App.suspend()`; on terminals where that raises, it falls back to opening a new console window (Windows) and asks the user to press `r`. Untested on the owner's terminal.
- Pricing table will drift; `models --refresh` pulls ids but not prices.
- The `Anthropic()` zero-arg client is constructed whenever an OAuth profile directory exists, even if the profile is stale; errors surface only at first call.
