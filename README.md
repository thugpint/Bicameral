# Bicameral

**Two minds, one diff.** A self-improving architect/editor orchestrator that lives inside Claude Code.

Run `/bicameral <task>` and the task is split between two frontier models by strength. **Claude Code is the Architect**: it plans, reviews every diff, and reflects, on your own Anthropic account. The **Editor** is a second model that writes the diffs, on your own account too: OpenAI through the Codex CLI's *Sign in with ChatGPT*, or a second Claude through headless Claude Code. API keys work as well, but nothing requires one.

A local MCP server you host closes the loop, and it is where the edge is:

- **Learned routing** — a Thompson-sampling bandit over (step kind, model) decides whether each step goes to the Editor or stays with the Architect. It starts from the Architect's suggestion and overrides it once the track record says so.
- **Reviewer gate** — every Editor diff is reviewed by the Architect against the step's acceptance criterion *and* your test command. Rejected or failing edits are rolled back automatically and retried with the feedback.
- **Reflective memory** — after every run the Architect writes 0–3 transferable lessons. Lessons are retrieved by relevance for later tasks and scored by whether the runs they were used in succeeded. Losers get pruned.
- **Retrieved examples** — diffs that passed both review and verification are shown to the Editor as few-shot examples on similar steps.
- **Hard evaluation** — fixture repositories with failing tests, hard pass/fail logging per task, and a baseline mode with learning switched off, so "self-improving" is a number, not a vibe.

## Install

```bash
pip install -e .
bicameral install      # writes the /bicameral skill and registers the MCP server with Claude Code
```

Python 3.11+. Then restart Claude Code.

## Sign in

Open the TUI:

```bash
bicameral
```

The **Setup** tab shows every backend and signs you in without leaving the terminal:

| Backend | What it is | How you sign in |
|---|---|---|
| Claude Code | your Anthropic account (the Architect, and optionally a second Claude as Editor) | **Sign in to Claude** → `claude auth login` |
| Codex CLI | your ChatGPT account as the Editor | **Sign in to ChatGPT** → `codex login` (`npm i -g @openai/codex` first) |
| Anthropic API | API key or `ant auth login` OAuth profile | paste a key, or it is picked up from the profile |
| OpenAI API | API key | paste a key |

The same is available from the command line: `bicameral login claude`, `bicameral login codex`, `bicameral login anthropic`, `bicameral login openai`.

## Use it in Claude Code

```
/bicameral add a --dry-run flag to the export command
```

What happens:

1. **Status and model choice** — Claude checks which Editor backends are signed in and asks which Editor model to use (your last choice is recommended). The Architect is whatever model your Claude Code session runs; change it with `/model`.
2. **Recall** — lessons and similar accepted edits from past runs, plus the routing track record, are pulled into context before planning.
3. **Plan** — Claude investigates the repo and registers 1–4 small steps, each with files, an acceptance criterion and a suggested role, plus the test command.
4. **Route, edit, verify, review** — per step, the server picks who executes it. Delegated steps are edited by the Editor model and come back as a diff with the test output; steps routed to Claude are edited by Claude and diffed the same way. Claude reviews against the acceptance criterion and accepts or rejects. Rejections roll back and retry with feedback, up to 3 attempts. Acceptance is refused while required verification is failing.
5. **Finish** — final verification, outcome logging, lesson storage, and a short report.

## The TUI

`bicameral` opens a dashboard with seven tabs:

- **Overview** — runs, success rate with learning on vs off, editor spend, a sparkline of recent outcomes, backend health, the routing table, recent runs.
- **Setup** — sign in to accounts, save API keys, install/uninstall the Claude Code integration.
- **Models** — the catalog with availability and prices, and your default Editor.
- **Run** — run a task standalone with any two models (for example Architect `claude:opus`, Editor `codex:gpt-5-codex`, both on your accounts).
- **Evals** — run the bundled suite with learning on or as a baseline, and read the comparison.
- **Runs** — history with per-step detail: who executed it, attempts, verification, feedback.
- **Lessons** — what the system has learned, with scores, and a prune button.

## Measure it

```bash
bicameral eval --baseline --runs 3 --architect claude:opus --editor codex:gpt-5-codex
bicameral eval --runs 3            --architect claude:opus --editor codex:gpt-5-codex
bicameral stats
```

`stats` prints success rate and cost for learning on vs off, the routing table (accepted/total per step kind and model), the eval trend per batch, and the lessons with the best track record. Add your own tasks by dropping a directory with `task.json` and a `fixture/` tree into any folder and passing `--suite DIR`:

```json
{
  "id": "median-even-length",
  "kind": "bugfix",
  "task": "median() returns the wrong value for even-length input. Fix it so the tests pass.",
  "verify_command": "{python} -m pytest -q",
  "expect_initial_failure": true
}
```

## Model ids

| Prefix | Backend | Examples |
|---|---|---|
| `claude:` | headless Claude Code, your Anthropic account | `claude:opus`, `claude:sonnet`, `claude:haiku` |
| `codex:` | Codex CLI, your ChatGPT account | `codex:gpt-5-codex`, `codex:gpt-5` |
| none | Anthropic or OpenAI API | `claude-opus-5`, `claude-sonnet-5`, `gpt-5-codex`, `o3` |

Inside Claude Code the Architect is always the host session (`claude-code` in the stats); the prefixes matter for the Editor and for standalone runs.

## Layout

```
src/bicameral/
  mcp_server.py     the tools Claude Code calls: status, recall, begin, execute, check, review, finish, stats
  skill/SKILL.md    the /bicameral skill: the Architect protocol
  engine.py         plan / route / edit / verify / review / rollback / record / reflect, shared by both loops
  orchestrator.py   standalone loop (CLI, evals, TUI Run tab)
  router.py         Thompson-sampling bandit over (step kind, model)
  memory.py         lessons and examples, BM25 retrieval, scoring and pruning
  providers/        anthropic (API/OAuth profile), openai (API), claude_cli, codex_cli
  auth.py           who is signed in to what, and how to sign in
  install.py        writes the skill, registers the MCP server via `claude mcp add`
  tui/              the Textual app
  evals/            harness and bundled fixture tasks
tests/              offline tests with scripted fake backends
```

## Notes and limits

- The Codex CLI is driven through `codex exec` (`--full-auto` inside the repo for edits, `--output-schema` for JSON). It is exercised in tests through a fake subprocess; if a flag drifts in a Codex release, `providers/codex_cli.py` is a one-line fix.
- Headless Claude Code is driven with `claude -p --output-format json`, with `--json-schema` for structured replies and `--permission-mode acceptEdits` for in-place edits. Nesting env vars are stripped so it runs from inside a Claude Code session.
- Account-backed backends are billed to your subscription; the TUI's spend tile only counts API tokens.
- Retrieval is BM25 over local SQLite, not embeddings. Dependency-free and offline; the obvious upgrade point.
- The bandit explores. On a fresh install it will occasionally send a step to the non-suggested side to gather evidence. That is the point.

## License

MIT
