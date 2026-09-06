# Bicameral

A self-improving architect/editor orchestrator for coding tasks.

Every task is split between two frontier models by strength: a **reasoning model** (the Architect) plans the work and reviews every diff, and a **coding model** (the Editor) writes the diffs. The loop is closed with three learning mechanisms that improve delegation and prompting over time without touching any model weights:

- **Learned routing** — a Thompson-sampling bandit over (step kind, model) decides which of the two models executes each step, starting from the Architect's suggestion and overriding it once the evidence says otherwise.
- **Reflective memory** — after every run the Architect writes lessons about what worked; lessons are retrieved by relevance for future tasks and scored by whether the runs they were used in succeeded. Losers get pruned.
- **Retrieved examples** — diffs that were accepted by the Reviewer *and* passed verification are stored and shown to the Editor as few-shot examples on similar steps.

And two things that make the "self-improving" claim measurable instead of vibes:

- **A reviewer gate** — the Architect verifies every Editor diff against the step's acceptance criterion before it is accepted, then the repo's test command runs. Rejected or failing edits are rolled back and retried with the feedback.
- **An evaluation harness** — fixture repositories with failing tests, hard pass/fail logging per task, and a `--baseline` mode with learning switched off so you can compare.

## Install

```bash
pip install -e .
```

Python 3.11+. Dependencies: `anthropic`, `openai`.

## Login

```bash
bicameral login anthropic
bicameral login openai
```

Neither provider offers a public third-party OAuth flow, so login means pasting an API key (input is hidden, the key is validated against the provider's model list, and stored in `~/.bicameral/credentials.json`). `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` environment variables take precedence if set.

## Use

Start the REPL from inside the repository you want to work on:

```bash
bicameral
```

```
bicameral> /bicameral add a --dry-run flag to the export command
Available models:
   1. claude-fable-5-1   anthropic  $10.00/$50.00  per 1M  strongest reasoning
   2. claude-opus-5      anthropic  $ 5.00/$25.00  per 1M  frontier reasoning + coding
   3. claude-sonnet-5    anthropic  $ 2.00/$10.00  per 1M  fast, strong coding
   ...
Architect model (plans + reviews) [claude-opus-5]:
Editor model (writes the diffs) [gpt-5-codex]:
```

It then plans the task, decides per step which of the two models should execute it, applies the edits, has the Architect review the diff, runs your tests, and records everything. Your last model choice becomes the default next time.

Non-interactive:

```bash
bicameral run "fix the flaky retry test" --architect claude-opus-5 --editor gpt-5-codex
```

Other commands: `models [--refresh]`, `stats`, `lessons [--prune]`, `status`, `logout`. Inside the REPL the same commands are available with a leading slash.

## Measure it

```bash
# learning off: static routing (follow the architect), no memory, no examples
bicameral eval --baseline --runs 3

# learning on: routing, memory and examples all active
bicameral eval --runs 3

bicameral stats
```

`stats` prints success rate and cost for learning on vs off, the routing table (accepted/total per step kind and model), the eval trend per batch of runs, and the lessons with the best track record. The bundled suite has three small Python tasks (a bug fix, a feature, a refactor). Add your own by dropping a directory with `task.json` and a `fixture/` tree into any folder and passing `--suite DIR`:

```json
{
  "id": "median-even-length",
  "kind": "bugfix",
  "task": "median() returns the wrong value for even-length input. Fix it so the tests pass.",
  "verify_command": "{python} -m pytest -q",
  "expect_initial_failure": true
}
```

`{python}` expands to the interpreter running Bicameral.

## How a run works

1. **Plan** — the Architect gets the task, the file tree, manifests, and the most relevant lessons from memory. It can ask for file contents before committing to a plan. It returns 1–4 steps, each with files, an acceptance criterion, a suggested role, and a verification command.
2. **Route** — for each step the router picks Architect or Editor model. With learning off it follows the suggestion. With learning on it samples from each arm's Beta posterior, with the suggestion as a prior pseudo-success.
3. **Edit** — the chosen model receives the step, the current file contents, retrieved examples of accepted edits on similar steps, and any feedback from the previous attempt. It returns search/replace blocks (exact, unique excerpts) and new files. Edits are validated as a set before anything is written.
4. **Verify + Review** — the verification command runs, then the Architect reviews the diff together with the verification output. On rejection or failure the edit is rolled back and the feedback goes into the next attempt (3 attempts by default). If the tests were already failing before the run started, intermediate steps are allowed to leave them red; the last step must make them pass.
5. **Record** — every run, step, model choice, attempt count, verdict, token count and cost is written to `~/.bicameral/bicameral.db`. Routing statistics are updated per (step kind, model).
6. **Reflect** — the Architect reads the run log and writes 0–3 transferable lessons. Lessons that were in context for this run gain or lose score depending on the outcome.

## Layout

```
src/bicameral/
  cli.py            REPL and subcommands
  orchestrator.py   the plan → route → edit → review → verify → record → reflect loop
  router.py         Thompson-sampling bandit over (step kind, model)
  memory.py         lessons (reflective memory) and examples (retrieved diffs)
  retrieval.py      dependency-free BM25
  prompts.py        system prompts and prompt builders for each role
  schemas.py        JSON schemas for every model call (strict-mode compatible)
  edits.py          search/replace application with atomic validation and rollback
  workspace.py      file tree, snapshots, command runner, test-command detection
  store.py          SQLite: runs, steps, routing, lessons, examples, eval_runs
  providers/        Anthropic and OpenAI adapters behind one interface
  evals/            harness and bundled fixture tasks
tests/              offline tests with a scripted fake provider
```

## Notes and limits

- Models are called with structured JSON output (`output_config.format` on Anthropic, `text.format` with `strict` on OpenAI) so parsing failures are rare; there is one retry if they happen.
- Reasoning effort is passed where the model supports it (`output_config.effort` on Claude, `reasoning.effort` on OpenAI reasoning models). Defaults: Architect `high`, Editor `medium`. Change them in `~/.bicameral/config.json`.
- Pricing in the catalog is a snapshot; unknown or refreshed models run fine but show cost as `n/a`.
- Retrieval is BM25 over local SQLite, not embeddings. This keeps the system dependency-free and offline; it is the obvious place to upgrade.
- The routing bandit explores: on a fresh install it will occasionally send a step to the non-suggested model to gather evidence. That is intentional, and it costs money.

## License

MIT
