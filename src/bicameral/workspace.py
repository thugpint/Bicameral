"""Filesystem and command access for the repository being edited."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", ".bicameral", ".idea", ".vscode", ".tox", "target",
}
MANIFESTS = ("README.md", "pyproject.toml", "package.json", "setup.py", "Cargo.toml", "go.mod", "Makefile")
PYTHON = f'"{sys.executable}"'


@dataclass
class CommandResult:
    ok: bool
    code: int
    output: str


@dataclass
class GateResult:
    """A deterministic check (lint, typecheck, build) run before any model judges a diff."""

    command: str
    ran: bool  # False when the executable could not be started: a configuration error, never a pass
    ok: bool
    output: str


class Workspace:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(str(self.root))

    # -- reading -----------------------------------------------------------

    def _walk(self, max_entries: int) -> list[str]:
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS and not d.startswith("."))
            rel_dir = Path(dirpath).relative_to(self.root).as_posix()
            for fn in sorted(filenames):
                if fn.startswith("."):
                    continue
                out.append(fn if rel_dir == "." else f"{rel_dir}/{fn}")
                if len(out) >= max_entries:
                    out.append("... (truncated)")
                    return out
        return out

    def tree(self, max_entries: int = 400) -> str:
        return "\n".join(self._walk(max_entries))

    def read(self, rel: str) -> str | None:
        p = self.root / rel
        if not p.is_file():
            return None
        try:
            return p.read_text("utf-8")
        except UnicodeDecodeError:
            return None

    def read_many(self, paths: list[str], max_chars_per_file: int = 12000) -> dict[str, str]:
        out: dict[str, str] = {}
        for rel in paths:
            rel = rel.strip().replace("\\", "/")
            if not rel:
                continue
            content = self.read(rel)
            if content is None:
                out[rel] = "<<file not found>>"
                continue
            if len(content) > max_chars_per_file:
                content = content[:max_chars_per_file] + f"\n<<truncated: {len(content) - max_chars_per_file} more chars>>"
            out[rel] = content
        return out

    def overview(self, max_manifest_chars: int = 3000) -> str:
        parts = ["## File tree", self.tree()]
        for name in MANIFESTS:
            content = self.read(name)
            if content:
                parts.append(f"## {name}\n{content[:max_manifest_chars]}")
        return "\n\n".join(parts)

    # -- snapshots ---------------------------------------------------------

    def snapshot(self, paths: list[str]) -> dict[str, str | None]:
        return {p: self.read(p) for p in paths}

    def current(self, paths: list[str]) -> dict[str, str | None]:
        return self.snapshot(paths)

    def restore(self, snap: dict[str, str | None]) -> None:
        for rel, content in snap.items():
            p = self.root / rel
            if content is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, "utf-8", newline="\n")

    # -- whole-tree snapshots (for backends that edit files themselves) -------

    MAX_SNAPSHOT_FILE_BYTES = 400_000

    def snapshot_tree(self, max_files: int = 4000) -> dict[str, str]:
        """Contents of every text file in the tree (ignoring build/VCS dirs)."""
        out: dict[str, str] = {}
        for rel in self._walk(max_files):
            if rel.startswith("..."):
                break
            p = self.root / rel
            try:
                if p.stat().st_size > self.MAX_SNAPSHOT_FILE_BYTES:
                    continue
                out[rel] = p.read_text("utf-8")
            except (UnicodeDecodeError, OSError):
                continue
        return out

    def changed_since(self, before: dict[str, str]) -> dict[str, str | None]:
        """Paths whose content differs from `before`: new/changed -> content, deleted -> None."""
        after = self.snapshot_tree()
        changed: dict[str, str | None] = {}
        for rel in sorted(set(before) | set(after)):
            if before.get(rel) != after.get(rel):
                changed[rel] = after.get(rel)
        return changed

    # -- commands ----------------------------------------------------------

    def run(self, command: str, timeout: int = 600, tail_chars: int = 4000) -> CommandResult:
        cmd = command.replace("{python}", PYTHON)
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=self.root, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return CommandResult(False, -1, f"command timed out after {timeout}s: {cmd}")
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        if len(output) > tail_chars:
            output = "...\n" + output[-tail_chars:]
        return CommandResult(proc.returncode == 0, proc.returncode, output.strip())

    def run_gate(self, command: str, timeout: int = 600, tail_chars: int = 3000) -> GateResult:
        """Run a gate with explicit argv (no shell), so a missing executable is reported as such."""
        tokens = shlex.split(command, posix=os.name != "nt")
        argv = [sys.executable if t == "{python}" else t.strip('"') for t in tokens]
        if not argv:
            return GateResult(command, False, False, "empty command")
        try:
            proc = subprocess.run(
                argv, cwd=self.root, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            return GateResult(command, False, False, f"could not start {argv[0]!r}: {e}")
        except subprocess.TimeoutExpired:
            return GateResult(command, True, False, f"timed out after {timeout}s")
        output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        if len(output) > tail_chars:
            output = "...\n" + output[-tail_chars:]
        return GateResult(command, True, proc.returncode == 0, output.strip())

    def detect_test_command(self) -> str | None:
        has_py_tests = (self.root / "tests").is_dir() or any(self.root.glob("test_*.py")) or (
            self.root / "pytest.ini"
        ).exists()
        if has_py_tests:
            return "{python} -m pytest -q -x"
        pkg = self.root / "package.json"
        if pkg.exists() and '"test"' in pkg.read_text("utf-8", errors="replace"):
            return "npm test --silent"
        if (self.root / "Cargo.toml").exists():
            return "cargo test -q"
        if (self.root / "go.mod").exists():
            return "go test ./..."
        return None
