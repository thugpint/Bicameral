"""Reflective memory (lessons) and retrieved examples of past successful edits."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .retrieval import BM25
from .schemas import Lesson
from .store import Store


class Memory:
    """Lessons written by the architect after each run, ranked by relevance and track record."""

    PRUNE_BELOW = -2.0
    RELEVANCE_WEIGHT = 1.0
    TRACK_RECORD_WEIGHT = 0.3

    def __init__(self, store: Store):
        self.store = store

    def retrieve(self, query: str, k: int = 5) -> list[Lesson]:
        lessons = [l for l in self.store.lessons() if l.score > self.PRUNE_BELOW]
        if not lessons:
            return []
        docs = [f"{l.text} {' '.join(l.applies_to)} {l.role}" for l in lessons]
        bm25 = BM25(docs)
        ranked = sorted(
            range(len(lessons)),
            key=lambda i: -(self.RELEVANCE_WEIGHT * bm25.score(query, i) + self.TRACK_RECORD_WEIGHT * lessons[i].score),
        )
        return [lessons[i] for i in ranked[:k]]

    def add(self, lessons: list[Lesson], run_id: int | None) -> list[int]:
        return [self.store.add_lesson(l, run_id) for l in lessons if l.text.strip()]

    def feedback(self, lesson_ids: list[int], success: bool) -> None:
        """Lessons that were in context for a successful run gain credit; failures cost a little."""
        self.store.bump_lessons(lesson_ids, 1.0 if success else -0.5)

    def prune(self) -> int:
        doomed = [l.id for l in self.store.lessons() if l.id is not None and l.score <= self.PRUNE_BELOW]
        self.store.delete_lessons(doomed)
        return len(doomed)


@dataclass
class Example:
    kind: str
    description: str
    files: list[str]
    diff: str
    model: str


class Examples:
    """Diffs from steps that were accepted by the reviewer and passed verification."""

    MAX_DIFF_CHARS = 3000
    SAME_KIND_BONUS = 1.0

    def __init__(self, store: Store):
        self.store = store

    def retrieve(self, description: str, kind: str, k: int = 2) -> list[Example]:
        rows: list[sqlite3.Row] = self.store.examples()
        if not rows:
            return []
        bm25 = BM25([f"{r['kind']} {r['description']}" for r in rows])
        scored = []
        for i, r in enumerate(rows):
            s = bm25.score(description, i) + (self.SAME_KIND_BONUS if r["kind"] == kind else 0.0)
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        out = []
        for _, i in scored[:k]:
            r = rows[i]
            out.append(Example(r["kind"], r["description"], json.loads(r["files"]), r["diff"], r["model"] or ""))
        return out

    def add(self, kind: str, description: str, files: list[str], diff: str, model: str, run_id: int | None) -> None:
        if not diff.strip():
            return
        self.store.add_example(kind, description, files, diff[: self.MAX_DIFF_CHARS], model, run_id)


class RepoLessons:
    """Lessons learned in one repository, mirrored into `.bicameral/lessons.md` so they can be committed.

    The file is regenerated from the store after every run in a stable order, capped, so its diff in a
    pull request is readable. Lines written by teammates (present in the file, absent from the local
    store) are still read back at recall time.
    """

    REL_PATH = ".bicameral/lessons.md"
    CAP = 40
    HEADER = (
        "# Bicameral lessons for this repository\n\n"
        "Written by Bicameral after each run; commit it so teammates' runs benefit. One lesson per line. "
        "Delete lines you disagree with; the next run will not resurrect them unless it re-learns them.\n\n"
    )

    def __init__(self, root):
        from pathlib import Path

        self.path = Path(root) / self.REL_PATH

    def read(self) -> list[str]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text("utf-8").splitlines():
            if line.startswith("- "):
                text = line[2:]
                if ") " in text and text.startswith("("):
                    text = text.split(") ", 1)[1]
                out.append(text.strip())
        return out

    def write(self, lessons: list[Lesson]) -> int:
        keep = [l for l in lessons if l.text.strip() and l.score > Memory.PRUNE_BELOW][-self.CAP:]
        existing = set(self.read())
        for l in keep:
            existing.discard(l.text.strip())
        if not keep and not self.path.exists():
            return 0
        lines = [f"- ({', '.join(l.applies_to) or 'any'}; {l.role}; {l.score:+.1f}) {l.text.strip()}" for l in keep]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.HEADER + "\n".join(lines) + ("\n" if lines else ""), "utf-8", newline="\n")
        return len(keep)
