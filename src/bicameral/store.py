"""SQLite persistence for runs, routing statistics, lessons, examples and eval results."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .schemas import Lesson

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    task TEXT NOT NULL,
    task_kind TEXT,
    workspace TEXT,
    architect_model TEXT,
    editor_model TEXT,
    learning INTEGER NOT NULL,
    success INTEGER,
    steps_total INTEGER DEFAULT 0,
    steps_ok INTEGER DEFAULT 0,
    cost_usd REAL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    duration_s REAL,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    step_idx INTEGER NOT NULL,
    title TEXT,
    kind TEXT,
    model TEXT,
    role TEXT,
    attempts INTEGER,
    accepted INTEGER,
    verified INTEGER,
    feedback TEXT,
    cost_usd REAL
);
CREATE TABLE IF NOT EXISTS routing (
    kind TEXT NOT NULL,
    model TEXT NOT NULL,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, model)
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    text TEXT NOT NULL,
    applies_to TEXT NOT NULL,
    role TEXT NOT NULL,
    confidence REAL NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0,
    source_run INTEGER
);
CREATE TABLE IF NOT EXISTS examples (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    files TEXT NOT NULL,
    diff TEXT NOT NULL,
    model TEXT,
    run_id INTEGER
);
CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    suite TEXT NOT NULL,
    task_id TEXT NOT NULL,
    learning INTEGER NOT NULL,
    success INTEGER NOT NULL,
    cost_usd REAL,
    duration_s REAL,
    attempts INTEGER,
    run_id INTEGER
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- runs --------------------------------------------------------------

    def create_run(self, **fields: Any) -> int:
        fields.setdefault("created_at", time.time())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.conn.execute(f"INSERT INTO runs ({cols}) VALUES ({marks})", list(fields.values()))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", [*fields.values(), run_id])
        self.conn.commit()

    def add_step(self, run_id: int, **fields: Any) -> None:
        fields["run_id"] = run_id
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        self.conn.execute(f"INSERT INTO steps ({cols}) VALUES ({marks})", list(fields.values()))
        self.conn.commit()

    def mark_interrupted(self, active_ids: list[int]) -> int:
        """Runs still open that no live session owns were cut off (Claude Code stopped, server restarted)."""
        marks = ",".join("?" for _ in active_ids) or "NULL"
        cur = self.conn.execute(
            f"UPDATE runs SET summary = 'interrupted' WHERE success IS NULL AND summary IS NULL AND id NOT IN ({marks})",
            list(active_ids),
        )
        self.conn.commit()
        return cur.rowcount

    def runs(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)))

    def steps_for(self, run_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM steps WHERE run_id = ? ORDER BY step_idx", (run_id,)))

    # -- routing -----------------------------------------------------------

    def routing_stats(self, kind: str, model: str) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT successes, failures FROM routing WHERE kind = ? AND model = ?", (kind, model)
        ).fetchone()
        return (row["successes"], row["failures"]) if row else (0, 0)

    def record_routing(self, kind: str, model: str, success: bool) -> None:
        self.conn.execute(
            "INSERT INTO routing (kind, model, successes, failures) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(kind, model) DO UPDATE SET successes = successes + excluded.successes, "
            "failures = failures + excluded.failures",
            (kind, model, int(success), int(not success)),
        )
        self.conn.commit()

    def routing_table(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM routing ORDER BY kind, model"))

    # -- lessons -----------------------------------------------------------

    def add_lesson(self, lesson: Lesson, source_run: int | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO lessons (created_at, text, applies_to, role, confidence, source_run) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), lesson.text, json.dumps(lesson.applies_to), lesson.role, lesson.confidence, source_run),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def lessons(self, min_score: float = -1e9) -> list[Lesson]:
        rows = self.conn.execute("SELECT * FROM lessons WHERE score >= ? ORDER BY score DESC, id DESC", (min_score,))
        return [
            Lesson(
                text=r["text"], applies_to=json.loads(r["applies_to"]), role=r["role"], confidence=r["confidence"],
                id=r["id"], score=r["score"], uses=r["uses"],
            )
            for r in rows
        ]

    def bump_lessons(self, ids: list[int], delta: float) -> None:
        if not ids:
            return
        marks = ", ".join("?" for _ in ids)
        self.conn.execute(f"UPDATE lessons SET uses = uses + 1, score = score + ? WHERE id IN ({marks})", [delta, *ids])
        self.conn.commit()

    def delete_lessons(self, ids: list[int]) -> None:
        if not ids:
            return
        marks = ", ".join("?" for _ in ids)
        self.conn.execute(f"DELETE FROM lessons WHERE id IN ({marks})", ids)
        self.conn.commit()

    # -- examples ----------------------------------------------------------

    def add_example(self, kind: str, description: str, files: list[str], diff: str, model: str, run_id: int | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO examples (created_at, kind, description, files, diff, model, run_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), kind, description, json.dumps(files), diff, model, run_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def examples(self, limit: int = 500) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM examples ORDER BY id DESC LIMIT ?", (limit,)))

    # -- evals -------------------------------------------------------------

    def add_eval(self, **fields: Any) -> None:
        fields.setdefault("created_at", time.time())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        self.conn.execute(f"INSERT INTO eval_runs ({cols}) VALUES ({marks})", list(fields.values()))
        self.conn.commit()

    def eval_summary(self, suite: str | None = None) -> list[sqlite3.Row]:
        where = "WHERE suite = ?" if suite else ""
        params = (suite,) if suite else ()
        return list(
            self.conn.execute(
                f"""SELECT suite, learning, COUNT(*) AS n, SUM(success) AS ok,
                           AVG(cost_usd) AS avg_cost, AVG(duration_s) AS avg_s, AVG(attempts) AS avg_attempts
                    FROM eval_runs {where} GROUP BY suite, learning ORDER BY suite, learning""",
                params,
            )
        )

    def eval_by_task(self, suite: str | None = None) -> list[sqlite3.Row]:
        where = "WHERE suite = ?" if suite else ""
        params = (suite,) if suite else ()
        return list(
            self.conn.execute(
                f"""SELECT suite, task_id, learning, COUNT(*) AS n, SUM(success) AS ok
                    FROM eval_runs {where} GROUP BY suite, task_id, learning ORDER BY suite, task_id, learning""",
                params,
            )
        )

    def eval_trend(self, suite: str, learning: int, window: int = 5) -> list[sqlite3.Row]:
        """Success rate per batch of `window` consecutive eval runs, oldest first."""
        return list(
            self.conn.execute(
                """SELECT batch, COUNT(*) AS n, SUM(success) AS ok, AVG(cost_usd) AS avg_cost
                   FROM (SELECT (row_number() OVER (ORDER BY id) - 1) / ? AS batch, success, cost_usd
                         FROM eval_runs WHERE suite = ? AND learning = ?)
                   GROUP BY batch ORDER BY batch""",
                (window, suite, learning),
            )
        )
