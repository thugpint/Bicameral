import pytest

from bicameral.edits import EditError, apply_edits, unified_diff
from bicameral.schemas import EditBlock, NewFile


def test_apply_exact_match(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n", "utf-8")
    touched = apply_edits(tmp_path, [EditBlock("a.py", "y = 2", "y = 3")], [])
    assert touched == ["a.py"]
    assert (tmp_path / "a.py").read_text("utf-8") == "x = 1\ny = 3\n"


def test_apply_tolerates_trailing_whitespace(tmp_path):
    (tmp_path / "a.py").write_text("def f():   \n    return 1\n", "utf-8")
    apply_edits(tmp_path, [EditBlock("a.py", "def f():\n    return 1", "def f():\n    return 2")], [])
    assert (tmp_path / "a.py").read_text("utf-8") == "def f():\n    return 2\n"


def test_missing_search_is_error_and_nothing_written(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", "utf-8")
    with pytest.raises(EditError, match="not found"):
        apply_edits(tmp_path, [EditBlock("a.py", "x = 1", "x = 2"), EditBlock("a.py", "nope", "y")], [])
    assert (tmp_path / "a.py").read_text("utf-8") == "x = 1\n"


def test_ambiguous_search_is_error(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nx = 1\n", "utf-8")
    with pytest.raises(EditError, match="matches 2 places"):
        apply_edits(tmp_path, [EditBlock("a.py", "x = 1", "x = 2")], [])


def test_new_file_then_edit_it(tmp_path):
    apply_edits(tmp_path, [EditBlock("pkg/new.py", "A = 1", "A = 2")], [NewFile("pkg/new.py", "A = 1\n")])
    assert (tmp_path / "pkg" / "new.py").read_text("utf-8") == "A = 2\n"


def test_path_escape_rejected(tmp_path):
    with pytest.raises(EditError, match="escapes|relative"):
        apply_edits(tmp_path, [], [NewFile("../evil.py", "")])
    with pytest.raises(EditError, match="relative"):
        apply_edits(tmp_path, [], [NewFile("/abs.py", "")])


def test_unified_diff_marks_new_and_changed():
    d = unified_diff({"a.py": "x = 1\n", "b.py": None}, {"a.py": "x = 2\n", "b.py": "new\n"})
    assert "-x = 1" in d and "+x = 2" in d
    assert "/dev/null" in d and "+new" in d
