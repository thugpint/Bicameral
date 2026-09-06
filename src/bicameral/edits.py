"""Search/replace edit application and diff rendering."""

from __future__ import annotations

import difflib
from pathlib import Path

from .schemas import EditBlock, NewFile


class EditError(ValueError):
    """An edit could not be applied; the message is written to be fed back to the editor model."""


def safe_path(root: Path, rel: str) -> Path:
    rel = rel.strip().replace("\\", "/")
    if not rel or rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise EditError(f"path must be relative to the workspace: {rel!r}")
    target = (root / rel).resolve()
    if root.resolve() not in target.parents and target != root.resolve():
        raise EditError(f"path escapes the workspace: {rel!r}")
    return target


def _normalize_lines(s: str) -> list[str]:
    return [line.rstrip() for line in s.replace("\r\n", "\n").split("\n")]


def _locate(content: str, search: str) -> tuple[int, int]:
    """Return (start, end) of the unique match for `search` in `content`.

    Tries an exact match first, then a match that ignores trailing whitespace
    on each line. Raises EditError on zero or multiple matches.
    """
    count = content.count(search)
    if count == 1:
        i = content.index(search)
        return i, i + len(search)
    if count > 1:
        raise EditError(f"search text matches {count} places; include more surrounding lines to make it unique")

    # Whitespace-tolerant fallback over line boundaries.
    c_lines = content.split("\n")
    s_lines = _normalize_lines(search)
    while s_lines and s_lines[-1] == "":
        s_lines.pop()
    if not s_lines:
        raise EditError("search text is empty")
    norm = [line.rstrip() for line in c_lines]
    hits = [i for i in range(len(norm) - len(s_lines) + 1) if norm[i : i + len(s_lines)] == s_lines]
    if len(hits) == 1:
        start_line = hits[0]
        start = sum(len(line) + 1 for line in c_lines[:start_line])
        end = start + sum(len(line) + 1 for line in c_lines[start_line : start_line + len(s_lines)]) - 1
        return start, end
    if len(hits) > 1:
        raise EditError(f"search text matches {len(hits)} places; include more surrounding lines to make it unique")
    preview = search.strip().split("\n")[0][:80]
    raise EditError(f"search text not found (first line: {preview!r}); copy it verbatim from the current file")


def apply_edits(root: Path, edits: list[EditBlock], new_files: list[NewFile]) -> list[str]:
    """Apply edits atomically: everything is validated before anything is written.

    Returns the list of touched relative paths.
    """
    root = root.resolve()
    pending: dict[str, str] = {}

    for nf in new_files:
        target = safe_path(root, nf.path)
        rel = target.relative_to(root).as_posix()
        if target.exists():
            raise EditError(f"new_files: {rel} already exists; use an edit instead")
        pending[rel] = nf.content

    for e in edits:
        target = safe_path(root, e.path)
        rel = target.relative_to(root).as_posix()
        if rel in pending:
            content = pending[rel]
        elif target.is_file():
            content = target.read_text("utf-8")
        else:
            raise EditError(f"{rel} does not exist; create it with new_files")
        if not e.search:
            raise EditError(f"{rel}: search text is empty; to create a file use new_files")
        try:
            start, end = _locate(content, e.search)
        except EditError as err:
            raise EditError(f"{rel}: {err}") from None
        pending[rel] = content[:start] + e.replace + content[end:]

    if not pending:
        raise EditError("no edits were provided")

    for rel, content in pending.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, "utf-8", newline="\n")
    return list(pending)


def unified_diff(before: dict[str, str | None], after: dict[str, str | None]) -> str:
    chunks: list[str] = []
    for rel in sorted(set(before) | set(after)):
        old = before.get(rel) or ""
        new = after.get(rel) or ""
        if old == new:
            continue
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{rel}" if before.get(rel) is not None else "/dev/null",
            tofile=f"b/{rel}" if after.get(rel) is not None else "/dev/null",
        )
        chunks.append("".join(diff))
    return "\n".join(chunks)
