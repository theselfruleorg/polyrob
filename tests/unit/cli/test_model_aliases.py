"""B6 — user-defined `model_aliases` (Hermes parity).

`/model <alias>` and `-m <alias>` expand a name from the CLI config's
`model_aliases` map to a (provider, model) pair, flowing through the same
persist+swap logic as an explicit `/model <provider> <model>`.
"""
import json

import cli.config_store as cs


# --- resolve_model_alias ------------------------------------------------------

def test_resolve_alias_slug(monkeypatch):
    monkeypatch.setattr(
        cs, "load_cli_config",
        lambda: {"model_aliases": {"fav": "anthropic/claude-sonnet-4-5"}},
    )
    assert cs.resolve_model_alias("fav") == ("anthropic", "claude-sonnet-4-5")


def test_resolve_alias_dict(monkeypatch):
    monkeypatch.setattr(
        cs, "load_cli_config",
        lambda: {"model_aliases": {"cheap": {"provider": "anthropic", "model": "claude-haiku-4-5"}}},
    )
    assert cs.resolve_model_alias("cheap") == ("anthropic", "claude-haiku-4-5")


def test_resolve_alias_bare_model(monkeypatch):
    monkeypatch.setattr(
        cs, "load_cli_config",
        lambda: {"model_aliases": {"mini": "gpt-5-mini"}},
    )
    assert cs.resolve_model_alias("mini") == (None, "gpt-5-mini")


def test_unknown_alias_returns_none(monkeypatch):
    monkeypatch.setattr(cs, "load_cli_config", lambda: {})
    assert cs.resolve_model_alias("nope") is None


def test_no_model_aliases_key_returns_none(monkeypatch):
    monkeypatch.setattr(cs, "load_cli_config", lambda: {"default_provider": "openai"})
    assert cs.resolve_model_alias("fav") is None


def test_empty_name_returns_none(monkeypatch):
    monkeypatch.setattr(
        cs, "load_cli_config",
        lambda: {"model_aliases": {"fav": "anthropic/claude-sonnet-4-5"}},
    )
    assert cs.resolve_model_alias("") is None
    assert cs.resolve_model_alias(None) is None


# --- resolve_provider_model expansion ------------------------------------------

def test_resolve_provider_model_expands_alias_slug(monkeypatch):
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    monkeypatch.setattr(
        cs, "load_cli_config",
        lambda: {"model_aliases": {"fav": "anthropic/claude-sonnet-4-5"}},
    )
    provider, model = cs.resolve_provider_model(None, "fav", available_keys=set())
    assert provider == "anthropic"
    assert model == "claude-sonnet-4-5"


def test_resolve_provider_model_alias_infers_provider(monkeypatch):
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    monkeypatch.setattr(
        cs, "load_cli_config",
        lambda: {"model_aliases": {"mini": "gpt-5"}},
    )
    provider, model = cs.resolve_provider_model(None, "mini", available_keys=set())
    assert provider == "openai"  # inferred via _provider_for_model
    assert model == "gpt-5"


def test_resolve_provider_model_explicit_provider_wins_over_alias(monkeypatch):
    # DECISION: an explicit --provider beats the alias's own provider — the user
    # typed -p on purpose, e.g. routing an alias's model through a different
    # provider/proxy.
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    monkeypatch.setattr(
        cs, "load_cli_config",
        lambda: {"model_aliases": {"fav": "anthropic/claude-sonnet-4-5"}},
    )
    provider, model = cs.resolve_provider_model("openrouter", "fav", available_keys=set())
    assert provider == "openrouter"
    assert model == "claude-sonnet-4-5"


def test_resolve_provider_model_non_alias_model_unaffected(monkeypatch):
    monkeypatch.setattr(cs, "get_default_model", lambda: (None, None))
    monkeypatch.setattr(cs, "load_cli_config", lambda: {})
    provider, model = cs.resolve_provider_model(
        None, "gpt-5", available_keys={"OPENROUTER_API_KEY"}
    )
    assert provider == "openai"
    assert model == "gpt-5"


# --- migrate_to_dotenv preserves model_aliases ---------------------------------

def test_migrate_preserves_aliases_rewrites_json(tmp_path, monkeypatch):
    cfg_path = tmp_path / "cli.json"
    cfg_path.write_text(json.dumps({
        "default_provider": "anthropic",
        "default_model": "claude-sonnet-4-5",
        "model_aliases": {"fav": "anthropic/claude-sonnet-4-5"},
    }))
    monkeypatch.setenv("POLYROB_CLI_CONFIG", str(cfg_path))

    cs.migrate_to_dotenv()

    env_path = tmp_path / ".env"
    assert env_path.exists()
    env_text = env_path.read_text()
    assert "DEFAULT_PROVIDER=anthropic" in env_text
    assert "DEFAULT_MODEL=claude-sonnet-4-5" in env_text
    # cli.json survives (NOT deleted) but is trimmed to only model_aliases.
    assert cfg_path.exists()
    remaining = json.loads(cfg_path.read_text())
    assert remaining == {"model_aliases": {"fav": "anthropic/claude-sonnet-4-5"}}


def test_migrate_deletes_json_when_no_aliases(tmp_path, monkeypatch):
    cfg_path = tmp_path / "cli.json"
    cfg_path.write_text(json.dumps({
        "default_provider": "anthropic",
        "default_model": "claude-sonnet-4-5",
    }))
    monkeypatch.setenv("POLYROB_CLI_CONFIG", str(cfg_path))

    cs.migrate_to_dotenv()

    assert not cfg_path.exists()  # legacy behavior unchanged when there's nothing to keep
