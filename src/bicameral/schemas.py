"""JSON schemas for every model call, plus typed views over the parsed results.

Schemas are written to satisfy OpenAI strict mode (every property required,
additionalProperties false) and are passed unchanged to Anthropic's
output_config.format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TASK_KINDS: tuple[str, ...] = ("bugfix", "feature", "refactor", "test", "docs", "config", "investigate")
ROLES: tuple[str, ...] = ("architect", "editor")


def _arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_STR = {"type": "string"}

STEP_SCHEMA = _obj(
    {
        "id": {"type": "integer"},
        "title": _STR,
        "description": _STR,
        "kind": {"type": "string", "enum": list(TASK_KINDS)},
        "files": _arr(_STR),
        "acceptance": _STR,
        "suggested_role": {"type": "string", "enum": list(ROLES)},
        "rationale": _STR,
    }
)

PLAN_SCHEMA = _obj(
    {
        "status": {"type": "string", "enum": ["plan", "need_files"]},
        "files_needed": _arr(_STR),
        "task_kind": {"type": "string", "enum": list(TASK_KINDS)},
        "summary": _STR,
        "verify_command": _STR,
        "steps": _arr(STEP_SCHEMA),
    }
)

EDIT_SCHEMA = _obj(
    {
        "status": {"type": "string", "enum": ["edits", "need_files", "blocked"]},
        "files_needed": _arr(_STR),
        "explanation": _STR,
        "edits": _arr(_obj({"path": _STR, "search": _STR, "replace": _STR})),
        "new_files": _arr(_obj({"path": _STR, "content": _STR})),
    }
)

REVIEW_SCHEMA = _obj(
    {
        "verdict": {"type": "string", "enum": ["accept", "reject"]},
        "feedback": _STR,
        "issues": _arr(_STR),
    }
)

REFLECT_SCHEMA = _obj(
    {
        "lessons": _arr(
            _obj(
                {
                    "lesson": _STR,
                    "applies_to": _arr({"type": "string", "enum": list(TASK_KINDS)}),
                    "role": {"type": "string", "enum": ["architect", "editor", "router", "any"]},
                    "confidence": {"type": "number"},
                }
            )
        ),
    }
)


@dataclass
class Step:
    id: int
    title: str
    description: str
    kind: str
    files: list[str]
    acceptance: str
    suggested_role: str
    rationale: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any], idx: int) -> "Step":
        kind = d.get("kind") if d.get("kind") in TASK_KINDS else "feature"
        role = d.get("suggested_role") if d.get("suggested_role") in ROLES else "editor"
        return cls(
            id=int(d.get("id", idx + 1)),
            title=str(d.get("title", f"Step {idx + 1}")),
            description=str(d.get("description", "")),
            kind=kind,
            files=[str(f) for f in d.get("files", []) if f],
            acceptance=str(d.get("acceptance", "")),
            suggested_role=role,
            rationale=str(d.get("rationale", "")),
        )


@dataclass
class Plan:
    task_kind: str
    summary: str
    verify_command: str
    steps: list[Step] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Plan":
        kind = d.get("task_kind") if d.get("task_kind") in TASK_KINDS else "feature"
        return cls(
            task_kind=kind,
            summary=str(d.get("summary", "")),
            verify_command=str(d.get("verify_command", "")).strip(),
            steps=[Step.from_dict(s, i) for i, s in enumerate(d.get("steps", []))],
        )


@dataclass
class EditBlock:
    path: str
    search: str
    replace: str


@dataclass
class NewFile:
    path: str
    content: str


@dataclass
class EditResult:
    status: str
    files_needed: list[str]
    explanation: str
    edits: list[EditBlock]
    new_files: list[NewFile]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EditResult":
        status = d.get("status") if d.get("status") in ("edits", "need_files", "blocked") else "edits"
        return cls(
            status=status,
            files_needed=[str(f) for f in d.get("files_needed", []) if f],
            explanation=str(d.get("explanation", "")),
            edits=[
                EditBlock(str(e.get("path", "")), str(e.get("search", "")), str(e.get("replace", "")))
                for e in d.get("edits", [])
            ],
            new_files=[NewFile(str(n.get("path", "")), str(n.get("content", ""))) for n in d.get("new_files", [])],
        )


@dataclass
class Review:
    verdict: str
    feedback: str
    issues: list[str]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Review":
        verdict = "accept" if d.get("verdict") == "accept" else "reject"
        return cls(verdict, str(d.get("feedback", "")), [str(i) for i in d.get("issues", [])])


@dataclass
class Lesson:
    text: str
    applies_to: list[str]
    role: str
    confidence: float
    id: int | None = None
    score: float = 0.0
    uses: int = 0
