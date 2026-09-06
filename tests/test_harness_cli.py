from __future__ import annotations

import json

from bicameral import config, credentials, models
from bicameral.evals import harness
from bicameral.orchestrator import RunConfig
from bicameral.providers import parse_json
from bicameral.workspace import Workspace
from fake import ARCHITECT, EDITOR, accept, edits, fake_llm, plan, step
from test_orchestrator import BUGGY, FIXED, VERIFY


def test_bundled_suite_loads_and_fixtures_fail_initially(tmp_path):
    tasks = harness.load_suite()
    assert {t.id for t in tasks} == {"median-even-length", "slugify-feature", "dedupe-parse-kv"}
    for t in tasks:
        import shutil

        dst = tmp_path / t.id
        shutil.copytree(t.fixture, dst)
        assert not Workspace(dst).run(t.verify_command).ok, f"{t.id} passes before any edit"


def test_harness_records_eval_run_and_leaves_fixture_untouched(store):
    task = next(t for t in harness.load_suite() if t.id == "median-even-length")
    before = (task.fixture / "stats.py").read_text("utf-8")
    llm, fp = fake_llm(
        {
            "plan": [plan([step("fix median", ["stats.py"])], verify=VERIFY)],
            "edit": [edits([("stats.py", BUGGY, FIXED)])],
            "review": [accept()],
        }
    )
    cfg = RunConfig(ARCHITECT, EDITOR, learning=False)
    results = harness.run_suite([task], llm, store, cfg, suite="unit", runs=2, log=lambda _: None)
    assert [r.success for r in results] == [True, True]
    assert (task.fixture / "stats.py").read_text("utf-8") == before
    rows = store.eval_summary("unit")
    assert rows[0]["n"] == 2 and rows[0]["ok"] == 2 and rows[0]["learning"] == 0
    report = harness.format_report(store, "unit")
    assert "100%" in report and "median-even-length" in report


def test_harness_records_failure_when_editor_does_nothing(store):
    task = next(t for t in harness.load_suite() if t.id == "slugify-feature")
    llm, _ = fake_llm(
        {
            "plan": [plan([step("add slugify", ["text_utils.py"], kind="feature")], kind="feature", verify=VERIFY)],
            "edit": [{"status": "blocked", "files_needed": [], "explanation": "nope", "edits": [], "new_files": []}],
            "review": [accept()],
        }
    )
    results = harness.run_suite([task], llm, store, RunConfig(ARCHITECT, EDITOR, learning=False), suite="unit", log=lambda _: None)
    assert results[0].success is False and "blocked" in results[0].note
    assert store.eval_summary("unit")[0]["ok"] == 0


def test_credentials_roundtrip_and_env_precedence(monkeypatch, isolated_home):
    assert credentials.logged_in() == []
    credentials.set_key("openai", "sk-test ")
    assert credentials.get_key("openai") == "sk-test"
    assert credentials.key_source("openai") == "file"
    assert json.loads((isolated_home / "credentials.json").read_text())["openai"] == "sk-test"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert credentials.get_key("openai") == "sk-env" and credentials.key_source("openai") == "env"
    credentials.clear_key("openai")
    monkeypatch.delenv("OPENAI_API_KEY")
    assert credentials.get_key("openai") is None


def test_model_lookup_infers_unknown_ids_and_extra_models():
    assert models.find("claude-opus-5").provider == "anthropic"
    assert models.find("gpt-5").supports_effort
    unknown = models.find("gpt-4.1")
    assert unknown.provider == "openai" and not unknown.supports_effort and unknown.input_per_m is None
    assert models.find("claude-haiku-4-5").supports_effort is False
    config.update(extra_models={"openai": ["gpt-9-preview"]})
    assert any(m.id == "gpt-9-preview" and m.note == "your key" for m in models.all_models())
    assert models.estimate_cost(models.find("claude-sonnet-5"), 1_000_000, 100_000) == 3.0


def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": [1, 2]} hope that helps') == {"a": [1, 2]}


def test_cli_parser_and_offline_commands(capsys, monkeypatch):
    from bicameral import auth
    from bicameral.cli import main

    monkeypatch.setattr(auth, "available_ids", lambda: [])  # no sign-ins: `models` shows the full catalog
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "backends:" in out and "OpenAI API" in out and "Claude Code" in out
    assert main(["models"]) == 0
    assert "claude-opus-5" in capsys.readouterr().out
    assert main(["lessons"]) == 0
    assert main(["stats"]) == 0
    assert "no eval runs" in capsys.readouterr().out
