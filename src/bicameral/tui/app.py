"""The Bicameral TUI.

Tabs: Overview (live stats), Setup (sign in, install into Claude Code), Models,
Run (standalone task), Evals, Runs (history), Lessons.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.theme import Theme
from textual.widgets import (
    Button,
    DataTable,
    Digits,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    Sparkline,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)

from .. import __version__, auth, config, credentials, install, models, paths
from ..evals import harness
from ..memory import Memory
from ..orchestrator import Orchestrator, OrchestratorError, RunConfig
from ..providers import LLM, ProviderError, build_provider, build_providers
from ..providers.claude_cli import clean_env
from ..providers.claude_cli import find_executable as find_claude
from ..providers.codex_cli import find_executable as find_codex
from ..store import Store

BICAMERAL_THEME = Theme(
    name="bicameral",
    primary="#a78bfa",
    secondary="#22d3ee",
    accent="#f472b6",
    warning="#fbbf24",
    error="#fb7185",
    success="#34d399",
    foreground="#ece9fb",
    background="#0a0912",
    surface="#151329",
    panel="#211d3d",
    dark=True,
)

HERO = (
    "[b #a78bfa]◐[/] [b #ece9fb]BICAMERAL[/] [#22d3ee]v{v}[/]   [#8f89b3]two minds · one diff[/]\n"
    "[#8f89b3]Architect plans and reviews · Editor writes the diffs · every step routed, verified, and learned from[/]"
)


class Tile(Vertical):
    def __init__(self, caption: str, value: str = "–", sub: str = "", *, id: str, classes: str = "") -> None:
        super().__init__(id=id, classes=f"tile {classes}".strip())
        self._caption, self._value, self._sub = caption, value, sub

    def compose(self) -> ComposeResult:
        yield Label(self._caption, classes="caption")
        yield Digits(self._value)
        yield Label(self._sub, classes="sub")

    def set(self, value: str, sub: str | None = None, tone: str | None = None) -> None:
        self.query_one(Digits).update(value)
        if sub is not None:
            self.query_one(".sub", Label).update(sub)
        if tone is not None:
            self.remove_class("good", "warn", "accent")
            if tone:
                self.add_class(tone)


class BicameralApp(App):
    TITLE = "Bicameral"
    SUB_TITLE = "architect / editor orchestrator"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("1", "tab('overview')", "Overview"),
        Binding("2", "tab('setup')", "Setup"),
        Binding("3", "tab('models')", "Models"),
        Binding("4", "tab('run')", "Run"),
        Binding("5", "tab('evals')", "Evals"),
        Binding("6", "tab('runs')", "Runs"),
        Binding("7", "tab('lessons')", "Lessons"),
    ]

    def __init__(self, store: Store | None = None, workspace: str | None = None) -> None:
        super().__init__()
        self.store = store or Store(paths.db_path())
        self.workspace = workspace or os.getcwd()
        self._backends: list[auth.Backend] = []

    # -- layout ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                with VerticalScroll():
                    yield Static(HERO.format(v=__version__), id="hero")
                    with Horizontal(classes="tiles"):
                        yield Tile("RUNS", id="t-runs")
                        yield Tile("SUCCESS · learning on", id="t-on", classes="good")
                        yield Tile("SUCCESS · learning off", id="t-off", classes="warn")
                        yield Tile("EDITOR SPEND", id="t-cost", classes="accent")
                    yield Label("recent outcomes", classes="section-title")
                    yield Sparkline([0.0], summary_function=max, id="spark")
                    yield Label("backends", classes="section-title")
                    yield DataTable(id="backends")
                    yield Label("routing track record", classes="section-title")
                    yield DataTable(id="routing")
                    yield Label("recent runs", classes="section-title")
                    yield DataTable(id="recent")
            with TabPane("Setup", id="setup"):
                with VerticalScroll():
                    yield Label("Claude Code integration", classes="section-title")
                    yield Static("", id="install-state", classes="status-line")
                    with Horizontal(classes="row"):
                        yield Button("Install /bicameral into Claude Code", id="btn-install", variant="primary")
                        yield Button("Uninstall", id="btn-uninstall")
                    yield Label("Accounts (no API key needed)", classes="section-title")
                    yield Static("", id="acct-state", classes="status-line")
                    with Horizontal(classes="row"):
                        yield Button("Sign in to Claude", id="btn-claude-login", variant="success")
                        yield Button("Sign in to ChatGPT (Codex)", id="btn-codex-login", variant="success")
                        yield Button("Install Codex CLI", id="btn-codex-install")
                    yield Label("API keys (optional)", classes="section-title")
                    with Horizontal(classes="row"):
                        yield Select([("Anthropic", "anthropic"), ("OpenAI", "openai")], value="openai", allow_blank=False, id="key-provider")
                        yield Input(placeholder="paste API key", password=True, id="key-input")
                        yield Button("Save key", id="btn-save-key", variant="primary")
                        yield Button("Remove key", id="btn-remove-key")
                    yield RichLog(id="setup-log", markup=True, wrap=True)
            with TabPane("Models", id="models"):
                with VerticalScroll():
                    yield Label("Default editor model (asked again on every /bicameral run, this is the recommended one)", classes="section-title")
                    with Horizontal(classes="row"):
                        yield Select([], prompt="choose an editor model", id="editor-select")
                        yield Button("Save", id="btn-save-editor", variant="primary")
                    yield Label("catalog", classes="section-title")
                    yield DataTable(id="models-table")
            with TabPane("Run", id="run"):
                with VerticalScroll():
                    yield Label("Run a task standalone (outside Claude Code): pick two models, we do the rest", classes="section-title")
                    with Horizontal(classes="row"):
                        yield Input(placeholder="what should change?", id="run-task")
                    with Horizontal(classes="row"):
                        yield Input(value=self.workspace, placeholder="repository path", id="run-path")
                    with Horizontal(classes="row"):
                        yield Select([], prompt="architect model", id="run-arch")
                        yield Select([], prompt="editor model", id="run-editor")
                        yield Label("learning")
                        yield Switch(value=True, id="run-learning")
                        yield Button("Run", id="btn-run", variant="primary")
                    yield RichLog(id="run-log", markup=True, wrap=True)
            with TabPane("Evals", id="evals"):
                with VerticalScroll():
                    yield Label("Measure it: run the fixture suite with learning on, then --baseline, and compare", classes="section-title")
                    with Horizontal(classes="row"):
                        yield Select([], prompt="architect model", id="eval-arch")
                        yield Select([], prompt="editor model", id="eval-editor")
                        yield Label("baseline")
                        yield Switch(value=False, id="eval-baseline")
                        yield Input(value="1", placeholder="runs", id="eval-runs")
                        yield Button("Run suite", id="btn-eval", variant="primary")
                    yield Static("", id="eval-report", classes="panel")
                    yield RichLog(id="eval-log", markup=True, wrap=True)
            with TabPane("Runs", id="runs"):
                with VerticalScroll():
                    yield DataTable(id="runs-table")
                    yield RichLog(id="run-detail", markup=True, wrap=True)
            with TabPane("Lessons", id="lessons"):
                with VerticalScroll():
                    with Horizontal(classes="row"):
                        yield Button("Prune losing lessons", id="btn-prune", variant="warning")
                    yield DataTable(id="lessons-table")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(BICAMERAL_THEME)
        self.theme = "bicameral"
        for tid, cols in {
            "backends": ("backend", "status", "detail", "how to sign in"),
            "routing": ("step kind", "model", "accepted", "total", "rate"),
            "recent": ("#", "when", "outcome", "kind", "task", "editor", "cost"),
            "models-table": ("model", "backend", "available", "price / 1M", "note"),
            "runs-table": ("#", "when", "outcome", "kind", "task", "architect", "editor", "steps", "cost"),
            "lessons-table": ("#", "score", "uses", "role", "applies to", "lesson"),
        }.items():
            table = self.query_one(f"#{tid}", DataTable)
            table.add_columns(*cols)
            table.cursor_type = "row"
            table.zebra_stripes = True
        self.action_refresh()

    # -- actions -----------------------------------------------------------------

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_refresh(self) -> None:
        self._refresh_backends()

    @work(thread=True, exclusive=True, group="status")
    def _refresh_backends(self) -> None:
        backends = auth.backends()
        state = install.state()
        self.call_from_thread(self._apply_status, backends, state)

    def _apply_status(self, backends: list[auth.Backend], state: install.InstallState) -> None:
        self._backends = backends
        available = {b.id for b in backends if b.available}

        table = self.query_one("#backends", DataTable)
        table.clear()
        for b in backends:
            mark = "[#34d399]● ready[/]" if b.available else "[#fb7185]○ unavailable[/]"
            table.add_row(b.label, mark, b.detail, b.login_hint or "")

        skill = "[#34d399]installed[/]" if state.skill_installed else "[#fb7185]not installed[/]"
        mcp = "[#34d399]registered[/]" if state.mcp_installed else "[#fb7185]not registered[/]"
        claude = "" if state.claude_found else "  [#fbbf24]claude CLI not found on PATH[/]"
        self.query_one("#install-state", Static).update(f"skill /bicameral: {skill}   MCP server: {mcp}{claude}")

        acct = []
        for b in backends:
            if b.kind == "account":
                acct.append(f"{b.label}: " + ("[#34d399]" + b.detail + "[/]" if b.available else "[#fb7185]" + b.detail + "[/]"))
        self.query_one("#acct-state", Static).update("\n".join(acct))
        self.query_one("#btn-codex-login", Button).disabled = find_codex() is None
        self.query_one("#btn-claude-login", Button).disabled = find_claude() is None

        opts = [(f"{m.id}  ·  {m.display}", m.id) for m in models.all_models() if m.provider in available]
        cfg = config.load()
        for sid in ("editor-select", "run-editor", "eval-editor"):
            sel = self.query_one(f"#{sid}", Select)
            sel.set_options(opts)
            last = cfg.get("last_editor")
            if last in {v for _, v in opts}:
                sel.value = last
        arch_opts = [(f"{m.id}  ·  {m.display}", m.id) for m in models.all_models() if m.provider in available]
        for sid in ("run-arch", "eval-arch"):
            sel = self.query_one(f"#{sid}", Select)
            sel.set_options(arch_opts)
            last = cfg.get("last_architect")
            if last in {v for _, v in arch_opts}:
                sel.value = last

        mt = self.query_one("#models-table", DataTable)
        mt.clear()
        for m in models.all_models():
            price = "your account" if m.account_backed else (f"${m.input_per_m:.2f} / ${m.output_per_m:.2f}" if m.input_per_m is not None else "n/a")
            ok = "[#34d399]yes[/]" if m.provider in available else "[#8f89b3]no[/]"
            mt.add_row(m.id, m.provider, ok, price, m.note)
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        store = self.store
        runs = [r for r in store.runs(limit=500) if r["success"] is not None]
        on = [r for r in runs if r["learning"]]
        off = [r for r in runs if not r["learning"]]
        self.query_one("#t-runs", Tile).set(str(len(runs)), f"{len(store.lessons())} lessons stored")
        self.query_one("#t-on", Tile).set(_rate(on), f"{sum(r['success'] for r in on)}/{len(on)} runs")
        self.query_one("#t-off", Tile).set(_rate(off), f"{sum(r['success'] for r in off)}/{len(off)} runs")
        cost = sum(r["cost_usd"] or 0 for r in runs)
        self.query_one("#t-cost", Tile).set(f"${cost:.2f}", "API cost only; account backends are free here")
        recent = list(reversed(runs[:40]))
        self.query_one("#spark", Sparkline).data = [float(r["success"]) for r in recent] or [0.0]

        rt = self.query_one("#routing", DataTable)
        rt.clear()
        for r in store.routing_table():
            total = r["successes"] + r["failures"]
            rt.add_row(r["kind"], r["model"], str(r["successes"]), str(total), f"{r['successes'] / total:.0%}" if total else "-")

        for tid, limit, wide in (("recent", 8, False), ("runs-table", 200, True)):
            t = self.query_one(f"#{tid}", DataTable)
            t.clear()
            for r in store.runs(limit=limit):
                outcome = "[#34d399]ok[/]" if r["success"] else ("[#fb7185]fail[/]" if r["success"] == 0 else ("[#8f89b3]interrupted[/]" if r["summary"] == "interrupted" else "[#8f89b3]…[/]"))
                when = _when(r["created_at"])
                cost = f"${(r['cost_usd'] or 0):.3f}"
                if wide:
                    t.add_row(str(r["id"]), when, outcome, r["task_kind"] or "", (r["task"] or "")[:48], r["architect_model"] or "",
                              r["editor_model"] or "", f"{r['steps_ok']}/{r['steps_total']}", cost, key=str(r["id"]))
                else:
                    t.add_row(str(r["id"]), when, outcome, r["task_kind"] or "", (r["task"] or "")[:48], r["editor_model"] or "", cost)

        lt = self.query_one("#lessons-table", DataTable)
        lt.clear()
        for l in store.lessons():
            lt.add_row(str(l.id), f"{l.score:+.1f}", str(l.uses), l.role, ", ".join(l.applies_to) or "any", l.text)

    # -- setup tab ---------------------------------------------------------------

    def _setup_log(self, text: str) -> None:
        self.query_one("#setup-log", RichLog).write(text)

    @on(Button.Pressed, "#btn-install")
    def _install(self) -> None:
        for m in install.install_all():
            self._setup_log(f"[#34d399]•[/] {m}")
        self.notify("Installed. Restart Claude Code and run /bicameral <task>.", title="Bicameral")
        self.action_refresh()

    @on(Button.Pressed, "#btn-uninstall")
    def _uninstall(self) -> None:
        for m in install.uninstall_all():
            self._setup_log(f"[#fbbf24]•[/] {m}")
        self.action_refresh()

    @on(Button.Pressed, "#btn-claude-login")
    def _claude_login(self) -> None:
        exe = find_claude()
        if exe:
            self._interactive([exe, "auth", "login"], "Claude sign-in")

    @on(Button.Pressed, "#btn-codex-login")
    def _codex_login(self) -> None:
        exe = find_codex()
        if exe:
            self._interactive([exe, "login"], "ChatGPT sign-in")

    @on(Button.Pressed, "#btn-codex-install")
    def _codex_install(self) -> None:
        self._setup_log("Install the Codex CLI with:  [b]npm i -g @openai/codex[/]   then press the ChatGPT sign-in button.")

    def _interactive(self, cmd: list[str], label: str) -> None:
        """Hand the terminal to an interactive sign-in, then come back and refresh."""
        try:
            with self.suspend():
                subprocess.call(cmd, env=clean_env())
            self._setup_log(f"[#34d399]•[/] {label} finished")
        except Exception:  # driver cannot suspend (some Windows terminals): open a new console
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(cmd, env=clean_env(), creationflags=flags)
            self._setup_log(f"[#fbbf24]•[/] {label} opened in a new console window. Finish there, then press r to refresh.")
        self.action_refresh()

    @on(Button.Pressed, "#btn-save-key")
    def _save_key(self) -> None:
        provider = self.query_one("#key-provider", Select).value
        key = self.query_one("#key-input", Input).value.strip()
        if not key or provider is Select.BLANK:
            self._setup_log("[#fb7185]•[/] paste a key first")
            return
        self.query_one("#key-input", Input).value = ""
        self._verify_and_save_key(str(provider), key)

    @work(thread=True)
    def _verify_and_save_key(self, provider: str, key: str) -> None:
        try:
            n = len(build_provider(provider, key).list_models())
        except ProviderError as e:
            self.call_from_thread(self._setup_log, f"[#fb7185]•[/] key rejected: {e}")
            return
        credentials.set_key(provider, key)
        self.call_from_thread(self._setup_log, f"[#34d399]•[/] {provider} key saved ({n} models visible)")
        self.call_from_thread(self.action_refresh)

    @on(Button.Pressed, "#btn-remove-key")
    def _remove_key(self) -> None:
        provider = self.query_one("#key-provider", Select).value
        if provider is not Select.BLANK:
            credentials.clear_key(str(provider))
            self._setup_log(f"[#fbbf24]•[/] removed stored {provider} key")
            self.action_refresh()

    # -- models tab ----------------------------------------------------------------

    @on(Button.Pressed, "#btn-save-editor")
    def _save_editor(self) -> None:
        value = self.query_one("#editor-select", Select).value
        if value is Select.BLANK:
            self.notify("pick a model first", severity="warning")
            return
        config.update(last_editor=str(value))
        self.notify(f"default editor: {value}", title="saved")

    # -- run tab ---------------------------------------------------------------------

    @on(Button.Pressed, "#btn-run")
    def _run_task(self) -> None:
        task = self.query_one("#run-task", Input).value.strip()
        path = self.query_one("#run-path", Input).value.strip() or self.workspace
        arch = self.query_one("#run-arch", Select).value
        editor = self.query_one("#run-editor", Select).value
        learning = self.query_one("#run-learning", Switch).value
        log = self.query_one("#run-log", RichLog)
        if not task or arch is Select.BLANK or editor is Select.BLANK:
            log.write("[#fb7185]need a task, an architect model and an editor model[/]")
            return
        if not Path(path).is_dir():
            log.write(f"[#fb7185]not a directory: {path}[/]")
            return
        config.update(last_architect=str(arch), last_editor=str(editor))
        log.clear()
        self.query_one("#btn-run", Button).disabled = True
        self._run_worker(task, path, str(arch), str(editor), learning)

    @work(thread=True, exclusive=True, group="run")
    def _run_worker(self, task: str, path: str, arch: str, editor: str, learning: bool) -> None:
        log = self.query_one("#run-log", RichLog)
        emit = lambda line: self.call_from_thread(log.write, line)  # noqa: E731
        user_cfg = config.load()
        cfg = RunConfig(arch, editor, learning=learning, max_attempts=int(user_cfg.get("max_attempts", 3)),
                        architect_effort=str(user_cfg.get("architect_effort", "high")), editor_effort=str(user_cfg.get("editor_effort", "medium")))
        try:
            from ..workspace import Workspace

            result = Orchestrator(LLM(build_providers()), self.store, Workspace(path), cfg, log=emit).run(task)
            emit(f"[b]{'[#34d399]SUCCESS[/]' if result.success else '[#fb7185]FAILED[/]'}[/]  ${result.cost_usd:.4f}  {result.duration_s:.0f}s")
            for s in result.steps:
                emit(f"  {'ok ' if s.ok else 'err'} {s.title} <- {s.role} ({s.model}) x{s.attempts}")
            if result.failure_reason:
                emit(f"  [#fb7185]{result.failure_reason}[/]")
            for text in result.lessons_learned:
                emit(f"  lesson: {text}")
        except (ProviderError, OrchestratorError) as e:
            emit(f"[#fb7185]run aborted: {e}[/]")
        finally:
            self.call_from_thread(lambda: setattr(self.query_one("#btn-run", Button), "disabled", False))
            self.call_from_thread(self._refresh_stats)

    # -- evals tab -----------------------------------------------------------------------

    @on(Button.Pressed, "#btn-eval")
    def _run_eval(self) -> None:
        arch = self.query_one("#eval-arch", Select).value
        editor = self.query_one("#eval-editor", Select).value
        baseline = self.query_one("#eval-baseline", Switch).value
        try:
            runs = max(1, int(self.query_one("#eval-runs", Input).value or "1"))
        except ValueError:
            runs = 1
        log = self.query_one("#eval-log", RichLog)
        if arch is Select.BLANK or editor is Select.BLANK:
            log.write("[#fb7185]pick an architect and an editor model[/]")
            return
        log.clear()
        self.query_one("#btn-eval", Button).disabled = True
        self._eval_worker(str(arch), str(editor), baseline, runs)

    @work(thread=True, exclusive=True, group="eval")
    def _eval_worker(self, arch: str, editor: str, baseline: bool, runs: int) -> None:
        log = self.query_one("#eval-log", RichLog)
        emit = lambda line: self.call_from_thread(log.write, line)  # noqa: E731
        cfg = RunConfig(arch, editor, learning=not baseline)
        try:
            tasks = harness.load_suite()
            harness.run_suite(tasks, LLM(build_providers()), self.store, cfg, harness.default_suite_dir().name, runs=runs, log=emit)
            report = harness.format_report(self.store, harness.default_suite_dir().name)
            self.call_from_thread(self.query_one("#eval-report", Static).update, report)
        except (ProviderError, OrchestratorError) as e:
            emit(f"[#fb7185]eval aborted: {e}[/]")
        finally:
            self.call_from_thread(lambda: setattr(self.query_one("#btn-eval", Button), "disabled", False))
            self.call_from_thread(self._refresh_stats)

    # -- runs tab ----------------------------------------------------------------------------

    @on(DataTable.RowSelected, "#runs-table")
    def _show_run(self, event: DataTable.RowSelected) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        run_id = int(event.row_key.value)
        detail = self.query_one("#run-detail", RichLog)
        detail.clear()
        run = next((r for r in self.store.runs(limit=1000) if r["id"] == run_id), None)
        if run is None:
            return
        detail.write(f"[b]run {run_id}[/]  {run['task']}")
        detail.write(f"[#8f89b3]{run['summary'] or ''}[/]")
        for s in self.store.steps_for(run_id):
            mark = "[#34d399]ok[/]" if s["accepted"] else "[#fb7185]failed[/]"
            detail.write(f"  {mark} step {s['step_idx'] + 1}: {s['title']}  <- {s['role']} ({s['model']}) x{s['attempts']}" + ("  verified" if s["verified"] else ""))
            if s["feedback"]:
                detail.write(f"      [#8f89b3]{s['feedback'][:300]}[/]")

    # -- lessons tab ---------------------------------------------------------------------------

    @on(Button.Pressed, "#btn-prune")
    def _prune_lessons(self) -> None:
        n = Memory(self.store).prune()
        self.notify(f"pruned {n} lesson(s)")
        self._refresh_stats()


def _rate(rows) -> str:
    if not rows:
        return "–"
    return f"{sum(r['success'] for r in rows) / len(rows):.0%}"


def _when(ts: float | None) -> str:
    if not ts:
        return ""
    import datetime as dt

    return dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def run(workspace: str | None = None) -> None:
    BicameralApp(workspace=workspace).run()
