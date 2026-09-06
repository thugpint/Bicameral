import random

from bicameral.memory import Examples, Memory
from bicameral.retrieval import BM25
from bicameral.router import Router
from bicameral.schemas import Lesson

CANDS = {"architect": "big-model", "editor": "small-model"}


def test_router_follows_suggestion_when_learning_off(store):
    r = Router(store, learning=False)
    assert r.choose("bugfix", CANDS, "architect").model == "big-model"
    assert r.choose("bugfix", CANDS, "editor").model == "small-model"


def test_router_follows_suggestion_with_no_data(store):
    r = Router(store, learning=True, rng=random.Random(0))
    picks = [r.choose("bugfix", CANDS, "editor").role for _ in range(200)]
    assert picks.count("editor") > 120  # prior pseudo-success biases toward the suggestion


def test_router_learns_to_override_a_bad_suggestion(store):
    r = Router(store, learning=True, rng=random.Random(1))
    for _ in range(15):
        r.record("refactor", "small-model", False)
        r.record("refactor", "big-model", True)
    picks = [r.choose("refactor", CANDS, "editor") for _ in range(100)]
    overrides = [p for p in picks if p.role == "architect"]
    assert len(overrides) > 90
    assert "overrides" in overrides[0].reason


def test_router_single_model(store):
    r = Router(store)
    c = r.choose("bugfix", {"architect": "m", "editor": "m"}, "editor")
    assert c.model == "m" and "single" in c.reason


def test_bm25_ranks_relevant_doc_first():
    bm = BM25(["fix the median function for even length lists", "add a slugify helper", "rename config loader"])
    top = bm.top("median even length bug", k=2)
    assert top[0][0] == 0


def test_memory_retrieve_and_feedback(store):
    m = Memory(store)
    ids = m.add(
        [
            Lesson("include the failing test in editor context for bugfix steps", ["bugfix"], "editor", 0.9),
            Lesson("refactors across many files go to the architect", ["refactor"], "router", 0.7),
        ],
        run_id=None,
    )
    got = m.retrieve("fix bug so failing test passes", k=1)
    assert got[0].text.startswith("include the failing test")

    m.feedback([ids[1]], success=False)
    m.feedback([ids[1]], success=False)
    m.feedback([ids[1]], success=False)
    m.feedback([ids[1]], success=False)
    assert m.prune() == 1
    assert [l.text for l in store.lessons()] == ["include the failing test in editor context for bugfix steps"]


def test_examples_prefer_same_kind(store):
    ex = Examples(store)
    ex.add("bugfix", "fix median for even lists", ["stats.py"], "--- a\n+++ b\n-x\n+y", "m", None)
    ex.add("feature", "add median helper", ["stats.py"], "--- a\n+++ b\n-p\n+q", "m", None)
    got = ex.retrieve("median even lists", "bugfix", k=1)
    assert got and got[0].kind == "bugfix"
    assert ex.retrieve("totally unrelated words", "docs") == []
