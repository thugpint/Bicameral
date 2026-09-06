"""Where Bicameral keeps its state on disk. Every path derives from `home()`."""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    """Directory holding credentials, config and the learning database.

    Defaults to ~/.bicameral; override with the BICAMERAL_HOME env var.
    """
    p = Path(os.environ.get("BICAMERAL_HOME") or (Path.home() / ".bicameral"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return home() / "bicameral.db"


def credentials_path() -> Path:
    return home() / "credentials.json"


def config_path() -> Path:
    return home() / "config.json"
