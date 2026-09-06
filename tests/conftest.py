from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bicameral.store import Store

FIXTURES = Path(__file__).resolve().parents[1] / "src" / "bicameral" / "evals" / "tasks"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real ~/.bicameral during tests."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("BICAMERAL_HOME", str(home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return home


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def median_repo(tmp_path) -> Path:
    dst = tmp_path / "repo"
    shutil.copytree(FIXTURES / "median-even-length" / "fixture", dst)
    return dst
