"""Git plumbing for durable checkpoints, per-step commits and review diffs.

Everything here talks to the repository's own git with explicit argv. Checkpoints
are ordinary commit objects under refs/bicameral/, made through a temporary index
so the user's staging area is never touched and nothing in .git is renamed or
shadowed. They survive a server restart and `git gc` keeps them.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

__all__ = ["GitError", "checkpoint", "commit_paths", "diff", "hunk_ranges", "is_repo", "restore", "revert"]

_IDENTITY = {
    "GIT_AUTHOR_NAME": "bicameral",
    "GIT_AUTHOR_EMAIL": "bicameral@localhost",
    "GIT_COMMITTER_NAME": "bicameral",
    "GIT_COMMITTER_EMAIL": "bicameral@localhost",
}


class GitError(RuntimeError):
    pass


def _git(root: Path, args: list[str], env: dict[str, str] | None = None, timeout: int = 120) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env={**os.environ, **(env or {})},
        )
    except FileNotFoundError as e:
        raise GitError("git is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {args[0]} timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout).strip() or f"git {args[0]} failed (exit {proc.returncode})")
    return proc.stdout.strip()


def is_repo(root: Path) -> bool:
    try:
        return _git(root, ["rev-parse", "--is-inside-work-tree"]) == "true"
    except GitError:
        return False


def _head(root: Path) -> str | None:
    try:
        return _git(root, ["rev-parse", "--verify", "-q", "HEAD"])
    except GitError:
        return None  # unborn branch


def checkpoint(root: Path, ref: str, message: str) -> str:
    """Snapshot the working tree (tracked and untracked, honouring .gitignore) into a commit at `ref`."""
    with tempfile.TemporaryDirectory() as td:
        env = {"GIT_INDEX_FILE": str(Path(td) / "index")}
        _git(root, ["add", "-A", "--", "."], env=env)
        tree = _git(root, ["write-tree"], env=env)
    head = _head(root)
    parent = ["-p", head] if head else []
    sha = _git(root, ["commit-tree", tree, *parent, "-m", message], env=_IDENTITY)
    _git(root, ["update-ref", ref, sha])
    return sha


def restore(root: Path, sha: str) -> None:
    """Make the working tree match a checkpoint: files added since are removed, changed ones rewritten.

    Ignored files and the user's real index are left alone.
    """
    with tempfile.TemporaryDirectory() as td:
        env = {"GIT_INDEX_FILE": str(Path(td) / "index")}
        _git(root, ["add", "-A", "--", "."], env=env)
        _git(root, ["read-tree", "--reset", "-u", sha], env=env)


def commit_paths(root: Path, paths: list[str], message: str) -> str:
    """Commit exactly these paths with the user's own identity; other staged work is left staged."""
    if not paths:
        raise GitError("nothing to commit")
    _git(root, ["add", "-A", "--", *paths])
    _git(root, ["commit", "-q", "--no-verify", "-m", message, "--", *paths])
    return _git(root, ["rev-parse", "HEAD"])


def revert(root: Path, shas: list[str]) -> list[str]:
    """Revert commits newest-first. Returns the new revert commit shas."""
    out = []
    for sha in shas:
        _git(root, ["revert", "--no-edit", sha])
        out.append(_git(root, ["rev-parse", "HEAD"]))
    return out


def diff(root: Path, base: str = "HEAD") -> str:
    """Working tree (including staged changes) against `base`, plus new untracked files as additions."""
    parts = [_git(root, ["diff", "--no-color", base, "--"])]
    untracked = _git(root, ["ls-files", "--others", "--exclude-standard"]).splitlines()
    for rel in untracked:
        p = root / rel
        try:
            text = p.read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lines = text.splitlines()
        body = "\n".join("+" + line for line in lines)
        parts.append(f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1,{len(lines)} @@\n{body}")
    return "\n".join(p for p in parts if p).strip()


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def hunk_ranges(unified: str) -> dict[str, list[tuple[int, int]]]:
    """For each file in a unified diff, the (start, end) line ranges of the new version its hunks cover."""
    out: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in unified.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            current = None if name == "/dev/null" else (name[2:] if name.startswith("b/") else name)
            out.setdefault(current, []) if current else None
            continue
        m = _HUNK.match(line)
        if m and current:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            out[current].append((start, max(start, start + count - 1)))
    return out
