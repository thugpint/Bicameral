"""Run eval tasks against fixture repositories and record hard pass/fail outcomes.

Each task directory contains `task.json` and a `fixture/` tree. The fixture is
copied to a temporary directory, the orchestrator runs against the copy, and the
task's verify command decides success. Results go to the `eval_runs` table so
runs with learning on can be compared against `--baseline` runs.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..orchestrator import Orchestrator, OrchestratorError, RunConfig
from ..providers import LLM, ProviderError
from ..store import Store
from ..workspace import Workspace

Logger = Callable[[str], None]


@dataclass
class EvalTask:
    id: str
    task: str
    kind: str
    verify_command: str
    fixture: Path
    expect_initial_failure: bool = True


@dataclass
class EvalResult:
    task_id: str
    success: bool
    cost_usd: float
    duration_s: float
    attempts: int
    run_id: int | None
    note: str = ""


def default_suite_dir() -> Path:
    return Path(__file__).parent / "tasks"


def load_suite(suite_dir: Path | None = None) -> list[EvalTask]:
    suite_dir = suite_dir or default_suite_dir()
    tasks: list[EvalTask] = []
    for spec in sorted(suite_dir.glob("*/task.json")):
        data = json.loads(spec.read_text("utf-8"))
        fixture = spec.parent / "fixture"
        if not fixture.is_dir():
            raise FileNotFoundError(f"{spec}: missing fixture/ directory")
        tasks.append(
            EvalTask(
                id=data.get("id") or spec.parent.name,
                task=data["task"],
                kind=data.get("kind", "feature"),
                verify_command=data["verify_command"],
                fixture=fixture,
                expect_initial_failure=bool(data.get("expect_initial_failure", True)),
            )
        )
    return tasks


def run_task(task: EvalTask, llm: LLM, store: Store, cfg: RunConfig, suite: str, log: Logger) -> EvalResult:
    tmp = Path(tempfile.mkdtemp(prefix=f"bicameral-eval-{task.id}-"))
    t0 = time.time()
    try:
        shutil.copytree(task.fixture, tmp, dirs_exist_ok=True)
        ws = Workspace(tmp)
        initial = ws.run(task.verify_command)
        if task.expect_initial_failure and initial.ok:
            log(f"  warning: {task.id} verify command already passes before any edit; the task may be vacuous")
        run_cfg = RunConfig(**{**cfg.__dict__, "verify_command": task.verify_command})
        orch = Orchestrator(llm, store, ws, run_cfg, log=lambda m: log("  " + m))
        run_id: int | None = None
        attempts = 0
        note = ""
        try:
            result = orch.run(task.task)
            run_id = result.run_id
            attempts = result.attempts
            note = result.failure_reason
        except (ProviderError, OrchestratorError) as e:
            note = f"error: {e}"
        final = ws.run(task.verify_command)
        success = final.ok
        duration = time.time() - t0
        store.add_eval(
            suite=suite, task_id=task.id, learning=int(cfg.learning), success=int(success),
            cost_usd=orch.cost, duration_s=duration, attempts=attempts, run_id=run_id,
        )
        return EvalResult(task.id, success, orch.cost, duration, attempts, run_id, note)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_suite(
    tasks: list[EvalTask], llm: LLM, store: Store, cfg: RunConfig, suite: str, runs: int = 1, log: Logger = print,
) -> list[EvalResult]:
    results: list[EvalResult] = []
    for r in range(runs):
        for task in tasks:
            log(f"[{suite}] {task.id} (run {r + 1}/{runs}, learning={'on' if cfg.learning else 'off'})")
            res = run_task(task, llm, store, cfg, suite, log)
            status = "PASS" if res.success else "FAIL"
            log(f"  -> {status}  ${res.cost_usd:.4f}  {res.duration_s:.1f}s  {res.attempts} attempt(s)" + (f"  {res.note}" if res.note else ""))
            results.append(res)
    return results


def format_report(store: Store, suite: str | None = None) -> str:
    rows = store.eval_summary(suite)
    if not rows:
        return "no eval runs recorded yet"
    lines = ["suite            learning  runs  pass  rate    avg cost  avg time  avg attempts"]
    for r in rows:
        rate = (r["ok"] or 0) / r["n"] if r["n"] else 0.0
        lines.append(
            f"{r['suite']:<16} {'on ' if r['learning'] else 'off':<9} {r['n']:>4}  {r['ok'] or 0:>4}  {rate:>5.0%}   "
            f"${(r['avg_cost'] or 0):.4f}   {(r['avg_s'] or 0):>6.1f}s  {(r['avg_attempts'] or 0):.2f}"
        )
    per_task = store.eval_by_task(suite)
    if per_task:
        lines.append("")
        lines.append("per task:")
        for r in per_task:
            lines.append(f"  {r['task_id']:<28} learning={'on ' if r['learning'] else 'off'}  {r['ok'] or 0}/{r['n']}")
    suites = sorted({r["suite"] for r in rows})
    for s in suites:
        trend = store.eval_trend(s, 1)
        if len(trend) >= 2:
            lines.append("")
            lines.append(f"trend for {s} with learning on (success rate per batch of 5 runs, oldest first):")
            lines.append("  " + "  ".join(f"{(t['ok'] or 0) / t['n']:.0%}" for t in trend))
    return "\n".join(lines)
