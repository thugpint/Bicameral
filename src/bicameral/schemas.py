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
        "pin": {"type": "boolean"},
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
        "concerns": _STR,
        "edits": _arr(_obj({"path": _STR, "search": _STR, "replace": _STR})),
        "new_files": _arr(_obj({"path": _STR, "content": _STR})),
    }
)

CRITIQUE_SCHEMA = _obj(
    {
        "assessment": _STR,
        "concerns": _arr(_obj({"step": {"type": "integer"}, "issue": _STR, "suggestion": _STR})),
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
    pin: bool = False  # the router must honour suggested_role (the user named who does this step)

    @classmethod
    def from_dict(cls, d: dict[str, Any], idx: int) -> Step:
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
            pin=bool(d.get("pin", False)),
        )


@dataclass
class Plan:
    task_kind: str
    summary: str
    verify_command: str
    steps: list[Step] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Plan:
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
    concerns: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EditResult:
        status = d.get("status") if d.get("status") in ("edits", "need_files", "blocked") else "edits"
        return cls(
            status=status,
            files_needed=[str(f) for f in d.get("files_needed", []) if f],
            explanation=str(d.get("explanation", "")),
            concerns=str(d.get("concerns", "")).strip(),
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
    def from_dict(cls, d: dict[str, Any]) -> Review:
        verdict = "accept" if d.get("verdict") == "accept" else "reject"
        return cls(verdict, str(d.get("feedback", "")), [str(i) for i in d.get("issues", [])])

    def as_text(self) -> str:
        out = self.verdict.upper()
        if self.feedback:
            out += f": {self.feedback}"
        extra = [i for i in self.issues if i and i != self.feedback]
        if extra:
            out += "\n  - " + "\n  - ".join(extra)
        return out


@dataclass
class Critique:
    """The Editor's read of the Architect's plan before any file is touched."""

    assessment: str
    concerns: list[tuple[int, str, str]]  # (step id, issue, suggestion)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Critique:
        concerns = []
        for c in d.get("concerns", []):
            try:
                step = int(c.get("step", 0))
            except (TypeError, ValueError):
                step = 0
            issue = str(c.get("issue", "")).strip()
            if issue:
                concerns.append((step, issue, str(c.get("suggestion", "")).strip()))
        return cls(str(d.get("assessment", "")).strip(), concerns)

    def as_text(self) -> str:
        lines = [self.assessment or ("no concerns" if not self.concerns else "")]
        for step, issue, suggestion in self.concerns:
            where = f"step {step}: " if step else ""
            lines.append(f"- {where}{issue}" + (f" -> {suggestion}" if suggestion else ""))
        return "\n".join(l for l in lines if l)


@dataclass
class Lesson:
    text: str
    applies_to: list[str]
    role: str
    confidence: float
    id: int | None = None
    score: float = 0.0
    uses: int = 0
