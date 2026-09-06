"""Command-line entry point: `bicameral` (REPL) and subcommands."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from . import __version__, config, credentials, models, paths
from .evals import harness
from .memory import Memory
from .orchestrator import Orchestrator, OrchestratorError, RunConfig
from .providers import LLM, ProviderError, build_provider, build_providers
from .store import Store
from .ui import list_models, print_result, resolve_models
from .workspace import Workspace

HELP = """commands:
  /bicameral <task>   plan, delegate, edit, review and verify a task in the current directory
  /models             list models (add --refresh to pull the live list from each provider)
  /login <provider>   store an API key for anthropic or openai
  /stats              success rates, routing table, eval results
  /lessons            lessons the system has learned (add --prune to drop losers)
  /help, /quit
Plain text without a leading slash is treated as a task."""


def _store() -> Store:
    return Store(paths.db_path())


def _llm() -> LLM:
    return LLM(build_providers())


# -- subcommands ---------------------------------------------------------------


def cmd_login(provider: str, verify: bool = True) -> int:
    if provider not in credentials.PROVIDERS:
        print(f"unknown provider '{provider}'; choose from {', '.join(credentials.PROVIDERS)}")
        return 2
    print(f"Paste your {provider} API key (input hidden). It is stored only in {paths.credentials_path()}.")
    key = getpass.getpass("API key: ").strip()
    if not key:
        print("no key entered")
        return 1
    if verify:
        try:
            n = len(build_provider(provider, key).list_models())
        except ProviderError as e:
            print(f"key rejected: {e}")
            return 1
        print(f"key accepted ({n} models visible)")
    credentials.set_key(provider, key)
    print(f"logged in to {provider}")
    return 0


def cmd_logout(provider: str) -> int:
    credentials.clear_key(provider)
    print(f"removed stored key for {provider}")
    return 0


def cmd_status() -> int:
    for p in credentials.PROVIDERS:
        src = credentials.key_source(p)
        print(f"  {p:<10} {'logged in (' + src + ')' if src else 'not logged in'}")
    cfg = config.load()
    print(f"  last models: architect={cfg.get('last_architect') or '-'} editor={cfg.get('last_editor') or '-'}")
    print(f"  data dir:    {paths.home()}")
    return 0


def cmd_models(refresh: bool = False) -> int:
    available = credentials.logged_in()
    if refresh:
        extra = {}
        for name, provider in build_providers().items():
            try:
                ids = provider.list_models()
            except ProviderError as e:
                print(f"  {name}: {e}")
                continue
            extra[name] = ids
            print(f"  {name}: {len(ids)} models")
        cfg = config.load()
        merged = dict(cfg.get("extra_models") or {})
        merged.update(extra)
        config.update(extra_models=merged)
    if not available:
        print("no providers logged in; showing catalog")
        available = list(credentials.PROVIDERS)
    list_models(available)
    return 0


def _run_config(args: argparse.Namespace, architect: str, editor: str, learning: bool) -> RunConfig:
    cfg = config.load()
    verify: str | None
    if getattr(args, "no_verify", False):
        verify = ""
    else:
        verify = getattr(args, "verify", None)
    return RunConfig(
        architect_model=architect,
        editor_model=editor,
        learning=learning,
        verify_command=verify,
        max_attempts=getattr(args, "attempts", None) or int(cfg.get("max_attempts", 3)),
        architect_effort=str(cfg.get("architect_effort", "high")),
        editor_effort=str(cfg.get("editor_effort", "medium")),
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
    store = _store()
    try:
        runs = store.runs(limit=1000)
        if not runs:
            print("no runs yet")
        else:
            for learning in (1, 0):
                subset = [r for r in runs if r["learning"] == learning and r["success"] is not None]
                if subset:
                    ok = sum(r["success"] for r in subset)
                    cost = sum(r["cost_usd"] or 0 for r in subset)
                    print(f"runs with learning {'on ':<3}: {ok}/{len(subset)} succeeded ({ok / len(subset):.0%}), ${cost:.4f} total"
                          if learning else
                          f"runs with learning off: {ok}/{len(subset)} succeeded ({ok / len(subset):.0%}), ${cost:.4f} total")
        rows = store.routing_table()
        if rows:
            print("\nrouting table (step kind x model -> accepted/total):")
            for r in rows:
                total = r["successes"] + r["failures"]
                print(f"  {r['kind']:<12} {r['model']:<28} {r['successes']}/{total}")
        print()
        print(harness.format_report(store))
        lessons = store.lessons()
        if lessons:
            print("\ntop lessons:")
            for l in lessons[:10]:
                print(f"  [{l.score:+.1f}, used {l.uses}x] ({l.role}) {l.text}")
    finally:
        store.close()
    return 0


def cmd_lessons(prune: bool = False) -> int:
    store = _store()
    try:
        if prune:
            n = Memory(store).prune()
            print(f"pruned {n} lesson(s) with score below {Memory.PRUNE_BELOW}")
        lessons = store.lessons()
        if not lessons:
            print("no lessons yet; they are written after each run with learning on")
        for l in lessons:
            kinds = ", ".join(l.applies_to) or "any"
            print(f"  #{l.id} [{l.score:+.1f}, used {l.uses}x, conf {l.confidence:.2f}] ({l.role}; {kinds}) {l.text}")
    finally:
        store.close()
    return 0


# -- REPL ----------------------------------------------------------------------


def repl() -> int:
    print(f"Bicameral {__version__} - architect/editor orchestrator. Type /help for commands.")
    logged = credentials.logged_in()
    if not logged:
        print("No providers logged in yet. Use /login anthropic and/or /login openai.")
    else:
        print(f"Logged in: {', '.join(logged)}")
    while True:
        try:
            line = input("bicameral> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        rest = rest.strip()
        if cmd in ("/quit", "/exit", "/q"):
            return 0
        if cmd == "/help":
            print(HELP)
        elif cmd == "/login":
            cmd_login(rest or input("provider (anthropic/openai): ").strip())
        elif cmd == "/logout":
            cmd_logout(rest)
        elif cmd == "/status":
            cmd_status()
        elif cmd == "/models":
            cmd_models(refresh="--refresh" in rest)
        elif cmd == "/stats":
            cmd_stats()
        elif cmd == "/lessons":
            cmd_lessons(prune="--prune" in rest)
        elif cmd == "/bicameral" or not cmd.startswith("/"):
            task = rest if cmd == "/bicameral" else line
            if not task:
                task = input("task: ").strip()
            if not task:
                continue
            ns = argparse.Namespace(task=task, architect=None, editor=None, no_learning=False, verify=None,
                                    no_verify=False, path=".", attempts=None)
            try:
                cmd_run(ns)
            except SystemExit as e:
                print(e)
        else:
            print(f"unknown command {cmd}; /help for the list")


# -- argparse ------------------------------------------------------------------


def _add_model_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--architect", help="model id for planning and review (reasoning model)")
    p.add_argument("--editor", help="model id for writing diffs (coding model)")
    p.add_argument("--attempts", type=int, help="max attempts per step (default from config, 3)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bicameral", description="Self-improving architect/editor coding orchestrator.")
    p.add_argument("--version", action="version", version=f"bicameral {__version__}")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("login", help="store an API key for a provider")
    s.add_argument("provider", choices=credentials.PROVIDERS)
    s.add_argument("--no-verify", action="store_true", help="skip the API check before saving")

    s = sub.add_parser("logout", help="remove a stored API key")
    s.add_argument("provider", choices=credentials.PROVIDERS)

    sub.add_parser("status", help="show login state and defaults")

    s = sub.add_parser("models", help="list models")
    s.add_argument("--refresh", action="store_true", help="pull the live model list from each provider")

    s = sub.add_parser("run", help="run a task in a directory")
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

    sub.add_parser("stats", help="success rates, routing table and eval report")

    s = sub.add_parser("lessons", help="show learned lessons")
    s.add_argument("--prune", action="store_true", help="delete lessons with a losing track record")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        return repl()
    if args.command == "login":
        return cmd_login(args.provider, verify=not args.no_verify)
    if args.command == "logout":
        return cmd_logout(args.provider)
    if args.command == "status":
        return cmd_status()
    if args.command == "models":
        return cmd_models(refresh=args.refresh)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "eval":
        return cmd_eval(args)
    if args.command == "stats":
        return cmd_stats()
    if args.command == "lessons":
        return cmd_lessons(prune=args.prune)
    return 2


if __name__ == "__main__":
    sys.exit(main())
