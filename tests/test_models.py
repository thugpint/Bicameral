"""The model list follows what the account or key can actually run."""

from __future__ import annotations

import json

from bicameral import config, models
from bicameral.providers import ProviderError
from bicameral.providers import codex_cli


def _write_cache(tmp_path, monkeypatch, entries):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": entries}), "utf-8")


def test_codex_models_come_from_the_cli_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert codex_cli.cached_models() == []
    assert codex_cli.CodexCliProvider(executable="x").list_models() == ["codex:gpt-5-codex", "codex:gpt-5"]

    _write_cache(tmp_path, monkeypatch, [
        {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list"},
        {"slug": "gpt-reserve", "display_name": "GPT-Reserve", "visibility": "hide"},
        {"slug": "gpt-5-codex", "display_name": "GPT-5 Codex", "visibility": "list"},
    ])
    assert codex_cli.cached_models() == [("gpt-6-astra", "GPT-6-Astra"), ("gpt-5-codex", "GPT-5 Codex")]
    assert codex_cli.CodexCliProvider(executable="x").list_models() == ["codex:gpt-6-astra", "codex:gpt-5-codex"]

    ids = [m.id for m in models.all_models() if m.provider == "codex-cli"]
    assert ids == ["codex:gpt-6-astra", "codex:gpt-5-codex"]  # the cache replaces the static defaults
    astra = models.find("codex:gpt-6-astra")
    assert astra.display == "GPT-6-Astra via Codex CLI" and astra.account_backed and astra.supports_effort
    assert models.find("codex:gpt-5").provider == "codex-cli"  # ids outside the list still resolve


def test_api_models_follow_the_key(monkeypatch):
    class Prov:
        def __init__(self, ids):
            self.ids = ids

        def list_models(self):
            if isinstance(self.ids, Exception):
                raise self.ids
            return self.ids

    counts = models.refresh({"openai": Prov(["gpt-5", "gpt-9-preview"]), "codex-cli": Prov(["ignored"])})
    assert counts == {"openai": 2}
    assert config.load()["extra_models"] == {"openai": ["gpt-5", "gpt-9-preview"]}
    openai_ids = [m.id for m in models.all_models() if m.provider == "openai"]
    assert openai_ids == ["gpt-5", "gpt-9-preview"]  # exactly what the key sees, catalog pricing kept where known
    assert models.find("gpt-5").input_per_m == 1.25 and models.find("gpt-9-preview").note == "your key"
    assert [m.id for m in models.all_models() if m.provider == "anthropic"] == [m.id for m in models.CATALOG if m.provider == "anthropic"]

    models.remember("openai", [])
    assert "gpt-5-codex" in [m.id for m in models.all_models()]  # empty list falls back to the catalog

    try:
        models.refresh({"anthropic": Prov(ProviderError("bad key"))})
    except ProviderError as e:
        assert "bad key" in str(e)
    else:
        raise AssertionError("refresh should surface the provider error")


def test_codex_auth_status_prefers_the_chatgpt_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {"access_token": "t"}}), "utf-8")
    assert codex_cli.auth_status()["authMethod"] == "chatgpt"
    (tmp_path / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-x"}), "utf-8")
    assert codex_cli.auth_status()["authMethod"] == "api-key"
