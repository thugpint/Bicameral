from text_utils import normalize_whitespace, slugify, truncate


def test_normalize_whitespace():
    assert normalize_whitespace("  a   b\n c ") == "a b c"


def test_truncate():
    assert truncate("hello world", 8) == "hello..."


def test_slugify_basic():
    assert slugify("Hello, World!") == "hello-world"


def test_slugify_collapses_runs_and_strips():
    assert slugify("  --Multiple   spaces & symbols--  ") == "multiple-spaces-symbols"


def test_slugify_keeps_digits():
    assert slugify("Release 2.0 (beta)") == "release-2-0-beta"
