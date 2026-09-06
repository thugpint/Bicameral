"""Command-line entry point.

`bicameral` with no arguments opens the TUI. Subcommands cover everything the
TUI does for scripting, plus `mcp` (the server Claude Code talks to).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from . import __version__, auth, config, credentials, install, models, paths
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
            n = len(build_provider(provider, key).list_models())
        except ProviderError as e:
            print(f"key rejected: {e}")
            return 1
        print(f"key accepted ({n} models visible)")
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
        extra = {}
        for name, provider in build_providers().items():
            if name not in ("anthropic", "openai"):
                continue
            try:
                ids = provider.list_models()
            except ProviderError as e:
                print(f"  {name}: {e}")
                continue
            extra[name] = ids
            print(f"  {name}: {len(ids)} models")
        merged = dict(config.load().get("extra_models") or {})
        merged.update(extra)
        config.update(extra_models=merged)
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bicameral", description="Self-improving architect/editor coding orchestrator. No arguments opens the TUI.")
    p.add_argument("--version", action="version", version=f"bicameral {__version__}")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("tui", help="open the TUI")
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

    s = sub.add_parser("models", help="list models")
    s.add_argument("--refresh", action="store_true", help="pull the live model list from API providers")

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

    sub.add_parser("stats", help="success rates, routing table and eval report")

    s = sub.add_parser("lessons", help="show learned lessons")
    s.add_argument("--prune", action="store_true", help="delete lessons with a losing track record")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command
    if cmd is None:
        return cmd_tui(None)
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
    if cmd == "stats":
        return cmd_stats()
    if cmd == "lessons":
        return cmd_lessons(prune=args.prune)
    return 2


if __name__ == "__main__":
    sys.exit(main())
