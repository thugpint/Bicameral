"""The Bicameral GUI: a local web app served by the standard library.

`bicameral` (no arguments) starts this server on 127.0.0.1 and opens the page in
the default browser. Everything the TUI and CLI can do is behind big buttons:
sign in, connect to Claude Code, run a task, read the history and the lessons.

The API is JSON over HTTP on localhost. Every /api call must carry the token
that is embedded into the page at load time, so other sites in the browser
cannot drive it.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import __version__, auth, config, credentials, install, models, paths
from ..evals import harness
from ..memory import Memory
from ..orchestrator import Orchestrator, OrchestratorError, RunConfig
from ..providers import LLM, ProviderError, build_provider, build_providers
from ..providers.claude_cli import clean_env
from ..providers.claude_cli import find_executable as find_claude
from ..providers.codex_cli import find_executable as find_codex
from ..store import Store
from ..workspace import Workspace

INDEX = Path(__file__).parent / "index.html"
STATUS_TTL_S = 20.0


class Job:
    """A background run or eval with a line log that the page polls."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.running = False
        self.result: dict[str, Any] | None = None
        self.lock = threading.Lock()

    def log(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)

    def reset(self) -> None:
        with self.lock:
            self.lines = []
            self.result = None
            self.running = True

    def snapshot(self, since: int) -> dict[str, Any]:
        with self.lock:
            return {"lines": self.lines[since:], "total": len(self.lines), "running": self.running, "result": self.result}


class GuiState:
    def __init__(self, store: Store | None = None, workspace: str | None = None) -> None:
        self.store = store or Store(paths.db_path())
        self.workspace = workspace or os.getcwd()
        self.token = secrets.token_urlsafe(24)
        self.run_job = Job()
        self.eval_job = Job()
        self._backends: list[auth.Backend] = []
        self._backends_at = 0.0
        self._install: install.InstallState | None = None
        self._install_at = 0.0
        self._lock = threading.Lock()

    # -- status --------------------------------------------------------------------

    def backends(self, fresh: bool = False) -> list[auth.Backend]:
        with self._lock:
            if fresh or time.time() - self._backends_at > STATUS_TTL_S:
                self._backends = auth.backends()
                self._backends_at = time.time()
            return self._backends

    def install_state(self, fresh: bool = False) -> install.InstallState:
        with self._lock:
            if fresh or self._install is None or time.time() - self._install_at > STATUS_TTL_S:
                self._install = install.state()
                self._install_at = time.time()
            return self._install

    def invalidate(self) -> None:
        with self._lock:
            self._backends_at = 0.0
            self._install_at = 0.0

    def state(self, fresh: bool = False) -> dict[str, Any]:
        backends = self.backends(fresh)
        st = self.install_state(fresh)
        available = {b.id for b in backends if b.available}
        cfg = config.load()
        store = self.store
        runs = [r for r in store.runs(limit=500) if r["success"] is not None]
        on = [r for r in runs if r["learning"]]
        off = [r for r in runs if not r["learning"]]
        return {
            "version": __version__,
            "workspace": self.workspace,
            "home": str(paths.home()),
            "os": platform.system(),
            "backends": [b.__dict__ for b in backends],
            "install": {"skill": st.skill_installed, "mcp": st.mcp_installed, "claude_found": st.claude_found},
            "tools": {"claude": find_claude() is not None, "codex": find_codex() is not None, "npm": shutil.which("npm") is not None},
            "models": [
                {
                    "id": m.id, "display": m.display, "provider": m.provider, "available": m.provider in available,
                    "account": m.account_backed, "note": m.note,
                    "price": None if m.input_per_m is None else [m.input_per_m, m.output_per_m],
                }
                for m in models.all_models()
            ],
            "config": {"last_architect": cfg.get("last_architect"), "last_editor": cfg.get("last_editor")},
            "stats": {
                "runs": len(runs),
                "lessons": len(store.lessons()),
                "on": {"ok": sum(r["success"] for r in on), "n": len(on)},
                "off": {"ok": sum(r["success"] for r in off), "n": len(off)},
                "cost": sum(r["cost_usd"] or 0 for r in runs),
                "recent": [int(r["success"]) for r in reversed(runs[:40])],
                "routing": [
                    {"kind": r["kind"], "model": r["model"], "ok": r["successes"], "n": r["successes"] + r["failures"]}
                    for r in store.routing_table()
                ],
            },
            "runs": [
                {
                    "id": r["id"], "when": _when(r["created_at"]), "success": r["success"], "kind": r["task_kind"] or "",
                    "task": r["task"] or "", "architect": r["architect_model"] or "", "editor": r["editor_model"] or "",
                    "steps_ok": r["steps_ok"], "steps_total": r["steps_total"], "cost": r["cost_usd"] or 0,
                    "learning": bool(r["learning"]), "summary": r["summary"] or "",
                }
                for r in store.runs(limit=200)
            ],
            "lessons": [
                {"id": lesson.id, "score": lesson.score, "uses": lesson.uses, "role": lesson.role, "applies_to": lesson.applies_to, "text": lesson.text}
                for lesson in store.lessons()
            ],
            "busy": {"run": self.run_job.running, "eval": self.eval_job.running},
            "eval_report": harness.format_report(store),
        }

    def run_detail(self, run_id: int) -> dict[str, Any]:
        return {
            "steps": [
                {
                    "idx": s["step_idx"], "title": s["title"], "kind": s["kind"], "model": s["model"], "role": s["role"],
                    "attempts": s["attempts"], "accepted": bool(s["accepted"]), "verified": bool(s["verified"]),
                    "feedback": s["feedback"] or "",
                }
                for s in self.store.steps_for(run_id)
            ]
        }

    # -- actions ---------------------------------------------------------------------

    def do_install(self) -> dict[str, Any]:
        msgs = install.install_all()
        self.invalidate()
        return {"ok": True, "messages": msgs}

    def do_uninstall(self) -> dict[str, Any]:
        msgs = install.uninstall_all()
        self.invalidate()
        return {"ok": True, "messages": msgs}

    def do_login(self, which: str) -> dict[str, Any]:
        if which == "claude":
            exe = find_claude()
            if not exe:
                return {"ok": False, "message": "Claude Code is not installed yet."}
            cmd = [exe, "auth", "login"]
            label = "Claude sign-in"
        elif which == "codex":
            exe = find_codex()
            if not exe:
                return {"ok": False, "message": "The Codex CLI is not installed yet."}
            cmd = [exe, "login"]
            label = "ChatGPT sign-in"
        elif which == "codex-install":
            npm = shutil.which("npm")
            if not npm:
                return {"ok": False, "message": "npm was not found. Install Node.js from nodejs.org first, then try again."}
            cmd = [npm, "i", "-g", "@openai/codex"]
            label = "Codex CLI install"
        else:
            return {"ok": False, "message": f"unknown login '{which}'"}
        try:
            open_in_terminal(cmd)
        except OSError as e:
            return {"ok": False, "message": f"Could not open a terminal window ({e}). Run this yourself: {' '.join(cmd)}"}
        self.invalidate()
        return {"ok": True, "message": f"{label} opened in a new window. Finish it there; this page updates by itself."}

    def do_save_key(self, provider: str, key: str) -> dict[str, Any]:
        if provider not in credentials.PROVIDERS:
            return {"ok": False, "message": f"unknown provider '{provider}'"}
        key = key.strip()
        if not key:
            return {"ok": False, "message": "Paste a key first."}
        try:
            ids = build_provider(provider, key).list_models()
        except ProviderError as e:
            return {"ok": False, "message": f"Key rejected: {e}"}
        credentials.set_key(provider, key)
        models.remember(provider, ids)
        self.invalidate()
        return {"ok": True, "message": f"{provider} key saved. {len(ids)} models available."}

    def do_remove_key(self, provider: str) -> dict[str, Any]:
        credentials.clear_key(provider)
        models.remember(provider, [])
        self.invalidate()
        return {"ok": True, "message": f"Removed the stored {provider} key."}

    def do_refresh_models(self) -> dict[str, Any]:
        try:
            counts = models.refresh(build_providers())
        except ProviderError as e:
            return {"ok": False, "message": f"Could not list models: {e}"}
        self.invalidate()
        if not counts:
            return {"ok": True, "message": "No API key saved. Account sign-ins list their models automatically."}
        return {"ok": True, "message": "Model list updated: " + ", ".join(f"{k} {n}" for k, n in counts.items()) + "."}

    def do_set_default(self, editor: str | None, architect: str | None) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if editor:
            updates["last_editor"] = editor
        if architect:
            updates["last_architect"] = architect
        if updates:
            config.update(**updates)
        return {"ok": True, "message": "Saved."}

    def do_prune(self) -> dict[str, Any]:
        n = Memory(self.store).prune()
        return {"ok": True, "message": f"Pruned {n} lesson(s)."}

    def do_run(self, task: str, path: str, architect: str, editor: str, learning: bool) -> dict[str, Any]:
        if self.run_job.running:
            return {"ok": False, "message": "A task is already running."}
        if not task.strip():
            return {"ok": False, "message": "Tell me what should change first."}
        if not architect or not editor:
            return {"ok": False, "message": "Pick a planner and a coder model."}
        if not Path(path).is_dir():
            return {"ok": False, "message": f"That folder does not exist: {path}"}
        config.update(last_architect=architect, last_editor=editor)
        self.run_job.reset()
        threading.Thread(target=self._run_worker, args=(task, path, architect, editor, learning), daemon=True).start()
        return {"ok": True, "message": "Started."}

    def _run_worker(self, task: str, path: str, architect: str, editor: str, learning: bool) -> None:
        job = self.run_job
        user_cfg = config.load()
        cfg = RunConfig(architect, editor, learning=learning, max_attempts=int(user_cfg.get("max_attempts", 3)),
                        architect_effort=str(user_cfg.get("architect_effort", "high")), editor_effort=str(user_cfg.get("editor_effort", "medium")))
        try:
            result = Orchestrator(LLM(build_providers()), self.store, Workspace(path), cfg, log=job.log).run(task)
            job.result = {
                "success": result.success, "cost": result.cost_usd, "duration": result.duration_s,
                "steps": [{"title": s.title, "ok": s.ok, "role": s.role, "model": s.model, "attempts": s.attempts} for s in result.steps],
                "failure_reason": result.failure_reason, "lessons": result.lessons_learned,
            }
        except (ProviderError, OrchestratorError) as e:
            job.log(f"run aborted: {e}")
            job.result = {"success": False, "failure_reason": str(e), "steps": [], "lessons": [], "cost": 0, "duration": 0}
        except Exception as e:  # keep the server alive whatever the engine does
            job.log(f"unexpected error: {e!r}")
            job.result = {"success": False, "failure_reason": repr(e), "steps": [], "lessons": [], "cost": 0, "duration": 0}
        finally:
            job.running = False

    def do_eval(self, architect: str, editor: str, baseline: bool, runs: int) -> dict[str, Any]:
        if self.eval_job.running:
            return {"ok": False, "message": "An eval is already running."}
        if not architect or not editor:
            return {"ok": False, "message": "Pick a planner and a coder model."}
        self.eval_job.reset()
        threading.Thread(target=self._eval_worker, args=(architect, editor, baseline, max(1, runs)), daemon=True).start()
        return {"ok": True, "message": "Started."}

    def _eval_worker(self, architect: str, editor: str, baseline: bool, runs: int) -> None:
        job = self.eval_job
        cfg = RunConfig(architect, editor, learning=not baseline)
        try:
            tasks = harness.load_suite()
            suite = harness.default_suite_dir().name
            results = harness.run_suite(tasks, LLM(build_providers()), self.store, cfg, suite, runs=runs, log=job.log)
            job.result = {"success": all(r.success for r in results), "passed": sum(r.success for r in results), "total": len(results)}
        except (ProviderError, OrchestratorError) as e:
            job.log(f"eval aborted: {e}")
            job.result = {"success": False, "passed": 0, "total": 0}
        except Exception as e:
            job.log(f"unexpected error: {e!r}")
            job.result = {"success": False, "passed": 0, "total": 0}
        finally:
            job.running = False


def open_in_terminal(cmd: list[str]) -> None:
    """Run an interactive command in a fresh terminal window the user can see."""
    env = clean_env()
    system = platform.system()
    if system == "Windows":
        subprocess.Popen(cmd, env=env, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return
    quoted = " ".join(_sh_quote(c) for c in cmd)
    if system == "Darwin":
        script = f'tell application "Terminal" to do script "{quoted}"'
        subprocess.Popen(["osascript", "-e", script, "-e", 'tell application "Terminal" to activate'], env=env)
        return
    for term in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"], ["konsole", "-e"], ["xterm", "-e"]):
        if shutil.which(term[0]):
            subprocess.Popen([*term, *cmd], env=env)
            return
    raise OSError("no terminal emulator found")


def _sh_quote(s: str) -> str:
    return s if s.isalnum() or all(c.isalnum() or c in "-_./@:" for c in s) else "'" + s.replace("'", "'\\''") + "'"


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%b %d, %H:%M") if ts else ""


# -- HTTP ---------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    state: GuiState  # set on the class by serve()

    def log_message(self, format: str, *args: Any) -> None:  # quiet
        pass

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Bicameral-Token", ""), self.state.token)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            html = INDEX.read_text("utf-8").replace("__TOKEN__", self.state.token)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if not url.path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return
        if not self._authorized():
            self._json({"error": "unauthorized"}, 401)
            return
        q = parse_qs(url.query)
        if url.path == "/api/state":
            self._json(self.state.state(fresh="fresh" in q))
        elif url.path == "/api/log":
            job = self.state.eval_job if q.get("which", ["run"])[0] == "eval" else self.state.run_job
            self._json(job.snapshot(int(q.get("since", ["0"])[0])))
        elif url.path.startswith("/api/run/"):
            try:
                self._json(self.state.run_detail(int(url.path.rsplit("/", 1)[1])))
            except ValueError:
                self._json({"error": "bad id"}, 400)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return
        if not self._authorized():
            self._json({"error": "unauthorized"}, 401)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"ok": False, "message": "bad json"}, 400)
            return
        st = self.state
        action = url.path[len("/api/"):]
        try:
            if action == "install":
                out = st.do_install()
            elif action == "uninstall":
                out = st.do_uninstall()
            elif action == "login":
                out = st.do_login(str(body.get("which", "")))
            elif action == "key":
                out = st.do_save_key(str(body.get("provider", "")), str(body.get("key", "")))
            elif action == "key/remove":
                out = st.do_remove_key(str(body.get("provider", "")))
            elif action == "models/refresh":
                out = st.do_refresh_models()
            elif action == "defaults":
                out = st.do_set_default(body.get("editor"), body.get("architect"))
            elif action == "prune":
                out = st.do_prune()
            elif action == "run":
                out = st.do_run(str(body.get("task", "")), str(body.get("path") or st.workspace), str(body.get("architect", "")),
                                str(body.get("editor", "")), bool(body.get("learning", True)))
            elif action == "eval":
                out = st.do_eval(str(body.get("architect", "")), str(body.get("editor", "")), bool(body.get("baseline", False)),
                                 int(body.get("runs", 1) or 1))
            else:
                self._json({"error": "not found"}, 404)
                return
        except Exception as e:  # never take the page down
            out = {"ok": False, "message": f"{type(e).__name__}: {e}"}
        self._json(out)


def make_server(state: GuiState, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"state": state})
    return ThreadingHTTPServer((host, port), handler)


def serve(workspace: str | None = None, port: int = 0, open_browser: bool = True) -> None:
    state = GuiState(workspace=workspace)
    server = make_server(state, port=port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Bicameral GUI: {url}   (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state.store.close()


if __name__ == "__main__":
    serve(port=int(sys.argv[1]) if len(sys.argv) > 1 else 0)
