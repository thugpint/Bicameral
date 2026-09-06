"""Command-line entry point.

`bicameral` with no arguments opens the GUI in your browser. `bicameral tui` is
the terminal version. Subcommands cover everything the GUI does for scripting,
plus `mcp` (the server Claude Code talks to).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from . import __version__, auth, config, credentials, gitops, install, models, paths
from .evals import harness
from .memory import Memory
from .orchestrator import Orchestrator, OrchestratorError, RunConfig
from .providers import LLM, ProviderError, build_provider, build_providers
from .store import Store
from .ui import list_models, print_result, resolve_models
from .workspace import Workspace


def _store() -> Store:
    return Store(paths.db_path())


def _llm() -> LLM:
    return LLM(build_providers())


# -- subcommands ---------------------------------------------------------------


def cmd_install() -> int:
    for m in install.install_all():
        print(m)
    return 0


def cmd_uninstall() -> int:
    for m in install.uninstall_all():
        print(m)
    return 0


def cmd_login(provider: str, verify: bool = True) -> int:
    if provider == "claude":
        return _interactive_login("claude")
    if provider == "codex":
        return _interactive_login("codex")
    if provider not in credentials.PROVIDERS:
        print(f"unknown provider '{provider}'")
        return 2
    print(f"Paste your {provider} API key (input hidden). It is stored only in {paths.credentials_path()}.")
    key = getpass.getpass("API key: ").strip()
    if not key:
        print("no key entered")
        return 1
    if verify:
        try:
            ids = build_provider(provider, key).list_models()
        except ProviderError as e:
            print(f"key rejected: {e}")
            return 1
        models.remember(provider, ids)
        print(f"key accepted ({len(ids)} models visible)")
    credentials.set_key(provider, key)
    print(f"logged in to {provider}")
    return 0


def _interactive_login(which: str) -> int:
    import subprocess

    from .providers.claude_cli import clean_env
    from .providers.claude_cli import find_executable as find_claude
    from .providers.codex_cli import find_executable as find_codex

    if which == "claude":
        exe = find_claude()
        if not exe:
            print("claude is not installed; install Claude Code first")
            return 1
        return subprocess.call([exe, "auth", "login"], env=clean_env())
    exe = find_codex()
    if not exe:
        print("codex is not installed; run: npm i -g @openai/codex")
        return 1
    return subprocess.call([exe, "login"], env=clean_env())


def cmd_logout(provider: str) -> int:
    credentials.clear_key(provider)
    print(f"removed stored key for {provider}")
    return 0


def cmd_status() -> int:
    print("backends:")
    for b in auth.backends():
        mark = "ready      " if b.available else "unavailable"
        print(f"  {mark} {b.label}: {b.detail}" + (f"  -> {b.login_hint}" if b.login_hint else ""))
    st = install.state()
    print(f"\nClaude Code: skill {'installed' if st.skill_installed else 'NOT installed'}, MCP server "
          f"{'registered' if st.mcp_installed else 'NOT registered'}" + ("" if st.claude_found else " (claude CLI not found)"))
    cfg = config.load()
    print(f"defaults: architect={cfg.get('last_architect') or '-'} editor={cfg.get('last_editor') or '-'}")
    print(f"data dir: {paths.home()}")
    return 0


def cmd_models(refresh: bool = False) -> int:
    available = auth.available_ids()
    if refresh:
        try:
            for name, n in models.refresh(build_providers()).items():
                print(f"  {name}: {n} models")
        except ProviderError as e:
            print(f"  {e}")
    if not available:
        print("no backends available; showing the full catalog")
        available = sorted({m.provider for m in models.all_models()})
    list_models(available)
    return 0


def _run_config(args: argparse.Namespace, architect: str, editor: str, learning: bool) -> RunConfig:
    cfg = config.load()
    verify: str | None = "" if getattr(args, "no_verify", False) else getattr(args, "verify", None)
    return RunConfig(
        architect_model=architect, editor_model=editor, learning=learning, verify_command=verify,
        max_attempts=getattr(args, "attempts", None) or int(cfg.get("max_attempts", 3)),
        architect_effort=str(cfg.get("architect_effort", "high")), editor_effort=str(cfg.get("editor_effort", "medium")),
        gates=list(getattr(args, "gate", []) or []), commit=bool(getattr(args, "commit", False)),
    )


def cmd_run(args: argparse.Namespace) -> int:
    llm = _llm()
    architect, editor = resolve_models(args.architect, args.editor, llm.available())
    store = _store()
    try:
        ws = Workspace(args.path)
    except NotADirectoryError as e:
        print(f"not a directory: {e}")
        return 2
    cfg = _run_config(args, architect, editor, learning=not args.no_learning)
    print(f"workspace: {ws.root}")
    orch = Orchestrator(llm, store, ws, cfg, log=print)
    try:
        result = orch.run(args.task)
    except (ProviderError, OrchestratorError) as e:
        print(f"run aborted: {e}")
        return 1
    finally:
        store.close()
    print_result(result)
    return 0 if result.success else 1


def cmd_eval(args: argparse.Namespace) -> int:
    llm = _llm()
    architect, editor = resolve_models(args.architect, args.editor, llm.available())
    suite_dir = Path(args.suite) if args.suite else harness.default_suite_dir()
    tasks = harness.load_suite(suite_dir)
    if args.only:
        tasks = [t for t in tasks if t.id in args.only]
    if not tasks:
        print("no tasks selected")
        return 2
    store = _store()
    cfg = _run_config(args, architect, editor, learning=not args.baseline)
    suite_name = args.name or suite_dir.name
    try:
        results = harness.run_suite(tasks, llm, store, cfg, suite_name, runs=args.runs, log=print)
        print()
        print(harness.format_report(store, suite_name))
    finally:
        store.close()
    return 0 if all(r.success for r in results) else 1


def cmd_stats() -> int:
    from .mcp_server import Bicameral

    store = _store()
    try:
        print(Bicameral(store=store).stats())
    finally:
        store.close()
    return 0


def cmd_lessons(prune: bool = False) -> int:
    store = _store()
    try:
        if prune:
            print(f"pruned {Memory(store).prune()} lesson(s)")
        lessons = store.lessons()
        if not lessons:
            print("no lessons yet; they are written after each run with learning on")
        for l in lessons:
            print(f"  #{l.id} [{l.score:+.1f}, used {l.uses}x, conf {l.confidence:.2f}] ({l.role}; {', '.join(l.applies_to) or 'any'}) {l.text}")
    finally:
        store.close()
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    from .mcp_server import Bicameral

    llm = _llm()
    available = llm.available()
    model = args.model or config.load().get("last_editor")
    if not model or model not in available:
        _, model = resolve_models(None, args.model, available)
    store = _store()
    try:
        print(Bicameral(store=store, llm_factory=lambda: llm).review_diff(args.path, model, args.base, args.context or ""))
    finally:
        store.close()
    return 0


def cmd_restore(run_id: int, path: str | None, step: int | None) -> int:
    store = _store()
    try:
        run = store.run(run_id)
        if run is None:
            print(f"no run {run_id}")
            return 2
        points = store.checkpoints_for(run_id)
        if step is not None:
            points = [c for c in points if c["step_id"] == step]
        if not points:
            print(f"run {run_id} has no git checkpoints" + (f" for step {step}" if step else "") + " (not a git repository at the time, or nothing was executed)")
            return 2
        target = points[0]
        root = Path(path or run["workspace"])
        try:
            gitops.restore(root, target["sha"])
        except gitops.GitError as e:
            print(f"restore failed: {e}")
            return 1
        print(f"restored {root} to the checkpoint before run {run_id} step {target['step_id']} ({target['sha'][:10]}, {target['ref']})")
    finally:
        store.close()
    return 0


def cmd_undo(run_id: int, path: str | None) -> int:
    store = _store()
    try:
        run = store.run(run_id)
        if run is None:
            print(f"no run {run_id}")
            return 2
        shas = [s["commit_sha"] for s in store.steps_for(run_id) if s["commit_sha"]]
        if not shas:
            print(f"run {run_id} made no commits (it ran without commit=true); use `bicameral restore {run_id}` for the checkpoint instead")
            return 2
        root = Path(path or run["workspace"])
        try:
            made = gitops.revert(root, list(reversed(shas)))
        except gitops.GitError as e:
            print(f"undo failed: {e}")
            return 1
        print(f"reverted {len(shas)} commit(s) from run {run_id}: " + ", ".join(s[:10] for s in made))
    finally:
        store.close()
    return 0


def cmd_gui(path: str | None, port: int, no_browser: bool) -> int:
    from .gui import serve

    serve(path, port=port, open_browser=not no_browser)
    return 0


def cmd_tui(path: str | None) -> int:
    from .tui import run

    run(path)
    return 0


def cmd_mcp() -> int:
    from .mcp_server import main as serve

    serve()
    return 0


# -- argparse ------------------------------------------------------------------


def _add_model_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--architect", help="model id for planning and review (e.g. claude:opus, claude-opus-5)")
    p.add_argument("--editor", help="model id for writing diffs (e.g. codex:gpt-5-codex, claude:sonnet, gpt-5-codex)")
    p.add_argument("--attempts", type=int, help="max attempts per step (default 3)")
    p.add_argument("--gate", action="append", default=[], metavar="CMD",
                   help="deterministic check run on every edit before review (repeatable), e.g. --gate 'ruff check .'")
    p.add_argument("--commit", action="store_true", help="commit each accepted, verified step with provenance trailers")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bicameral", description="Self-improving architect/editor coding orchestrator. No arguments opens the GUI.")
    p.add_argument("--version", action="version", version=f"bicameral {__version__}")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("gui", help="open the GUI in your browser (the default)")
    s.add_argument("--path", default=None, help="repository to preselect in Run a task")
    s.add_argument("--port", type=int, default=0, help="port to listen on (default: a free port)")
    s.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")

    s = sub.add_parser("tui", help="open the terminal UI instead of the GUI")
    s.add_argument("--path", default=None, help="repository to preselect in the Run tab")

    sub.add_parser("install", help="install the /bicameral skill and MCP server into Claude Code")
    sub.add_parser("uninstall", help="remove the skill and MCP server from Claude Code")
    sub.add_parser("mcp", help="run the MCP server on stdio (Claude Code launches this for you)")

    s = sub.add_parser("login", help="sign in: claude (Anthropic account), codex (ChatGPT account), anthropic/openai (API key)")
    s.add_argument("provider", choices=("claude", "codex", "anthropic", "openai"))
    s.add_argument("--no-verify", action="store_true", help="skip the API check before saving a key")

    s = sub.add_parser("logout", help="remove a stored API key")
    s.add_argument("provider", choices=credentials.PROVIDERS)

    sub.add_parser("status", help="backends, sign-in state and Claude Code integration")

    s = sub.add_parser("models", help="list the models your sign-ins and keys can use")
    s.add_argument("--refresh", action="store_true", help="ask the Anthropic / OpenAI API what your key can see")

    s = sub.add_parser("run", help="run a task standalone (outside Claude Code)")
    s.add_argument("task")
    s.add_argument("--path", default=".", help="repository directory (default: cwd)")
    s.add_argument("--no-learning", action="store_true", help="disable routing/memory/examples (baseline mode)")
    s.add_argument("--verify", help="verification command (default: auto-detect; {python} expands to the interpreter)")
    s.add_argument("--no-verify", action="store_true", help="skip verification entirely")
    _add_model_flags(s)

    s = sub.add_parser("eval", help="run the evaluation suite")
    s.add_argument("--suite", help="directory of eval tasks (default: bundled suite)")
    s.add_argument("--name", help="label for this suite in the results table")
    s.add_argument("--baseline", action="store_true", help="learning off: no routing, memory or examples")
    s.add_argument("--runs", type=int, default=1, help="repeat the whole suite N times")
    s.add_argument("--only", nargs="*", help="task ids to include")
    _add_model_flags(s)

    s = sub.add_parser("review", help="review-only: a second model reviews the working tree's diff, findings grounded to the diff")
    s.add_argument("--path", default=".", help="repository directory (default: cwd)")
    s.add_argument("--base", default="HEAD", help="git ref to diff against (default HEAD)")
    s.add_argument("--model", help="reviewer model id (default: last editor)")
    s.add_argument("--context", help="one line of context for the reviewer, e.g. the task")

    s = sub.add_parser("restore", help="put the working tree back to a run's git checkpoint (undoes an interrupted run)")
    s.add_argument("run_id", type=int)
    s.add_argument("--path", help="repository directory (default: the run's workspace)")
    s.add_argument("--step", type=int, help="restore to the checkpoint taken before this step instead of the first")

    s = sub.add_parser("undo", help="git revert the commits a run made with --commit / commit=true")
    s.add_argument("run_id", type=int)
    s.add_argument("--path", help="repository directory (default: the run's workspace)")

    sub.add_parser("stats", help="success rates, routing table and eval report")

    s = sub.add_parser("lessons", help="show learned lessons")
    s.add_argument("--prune", action="store_true", help="delete lessons with a losing track record")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command
    if cmd is None:
        return cmd_gui(None, 0, False)
    if cmd == "gui":
        return cmd_gui(args.path, args.port, args.no_browser)
    if cmd == "tui":
        return cmd_tui(args.path)
    if cmd == "install":
        return cmd_install()
    if cmd == "uninstall":
        return cmd_uninstall()
    if cmd == "mcp":
        return cmd_mcp()
    if cmd == "login":
        return cmd_login(args.provider, verify=not args.no_verify)
    if cmd == "logout":
        return cmd_logout(args.provider)
    if cmd == "status":
        return cmd_status()
    if cmd == "models":
        return cmd_models(refresh=args.refresh)
    if cmd == "run":
        return cmd_run(args)
    if cmd == "eval":
        return cmd_eval(args)
    if cmd == "review":
        return cmd_review(args)
    if cmd == "restore":
        return cmd_restore(args.run_id, args.path, args.step)
    if cmd == "undo":
        return cmd_undo(args.run_id, args.path)
    if cmd == "stats":
        return cmd_stats()
    if cmd == "lessons":
        return cmd_lessons(prune=args.prune)
    return 2


if __name__ == "__main__":
    sys.exit(main())
