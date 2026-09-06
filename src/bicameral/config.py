from __future__ import annotations

import json
from typing import Any

from .paths import config_path

DEFAULTS: dict[str, Any] = {
    "last_architect": None,
    "last_editor": None,
    "architect_effort": "high",
    "editor_effort": "medium",
    "max_attempts": 3,
    "extra_models": {},  # provider -> [model ids discovered via `models --refresh`]
}


def load() -> dict[str, Any]:
    p = config_path()
    data: dict[str, Any] = {}
    if p.exists():
        try:
            data = json.loads(p.read_text("utf-8"))
        except json.JSONDecodeError:
            data = {}
    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def save(data: dict[str, Any]) -> None:
    config_path().write_text(json.dumps(data, indent=2), "utf-8")


def update(**kwargs: Any) -> dict[str, Any]:
    data = load()
    data.update(kwargs)
    save(data)
    return data
