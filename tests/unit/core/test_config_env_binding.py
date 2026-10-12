"""ServerConfig reads the env ONLY through an explicit alias.

pydantic-settings binds every field to the env by default — by its alias, or
implicitly by its field name (case-insensitive). The implicit binding made 50+
undocumented env names into live knobs (``CACHE_TTL``, ``DOC_CHUNK_SIZE``,
``BROWSER`` as JSON, ...). ``AgentConfig.settings_customise_sources`` swaps in
env/dotenv sources that skip a field without an ``alias``: such a field keeps
its default (or a constructor / in-code value) whatever the environment holds.
"""
from pathlib import Path

import pytest

from core.config import AgentConfig, ServerConfig


@pytest.fixture
def _isolated(monkeypatch, tmp_path):
    # Anchor the config's relative paths (and its mkdirs) under tmp, and keep
    # the repo's own .env out of the picture.
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for name in ("CACHE_TTL", "DOC_CHUNK_SIZE", "RATE_LIMIT_WINDOW", "DATA_DIR",
                 "MAX_BROWSER_CONTEXTS", "CACHE_SIZE", "ENVIRONMENT", "DB_PATH", "COMPACTION_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_unaliased_field_ignores_os_environ(_isolated, monkeypatch):
    monkeypatch.setenv("CACHE_TTL", "7")
    monkeypatch.setenv("DOC_CHUNK_SIZE", "11")
    monkeypatch.setenv("cache_ttl", "7")
    cfg = ServerConfig(_env_file=None)
    assert ServerConfig.model_fields["cache_ttl"].alias is None
    assert cfg.cache_ttl == 3600
    assert cfg.doc_chunk_size == 4096


def test_unaliased_field_ignores_dotenv_file(_isolated):
    env_file = Path(_isolated) / ".env"
    env_file.write_text("CACHE_TTL=7\nRATE_LIMIT_WINDOW=9\nMAX_BROWSER_CONTEXTS=42\n")
    cfg = ServerConfig(_env_file=str(env_file))
    assert cfg.cache_ttl == 3600
    assert cfg.rate_limit_window == 60
    # ...while an aliased field in the same file still binds.
    assert cfg.max_browser_contexts == 42


def test_default_dotenv_file_is_still_read(_isolated):
    (Path(_isolated) / ".env").write_text("MAX_BROWSER_CONTEXTS=43\nCACHE_TTL=7\n")
    cfg = ServerConfig()
    assert cfg.max_browser_contexts == 43
    assert cfg.cache_ttl == 3600


def test_unaliased_field_still_takes_constructor_value(_isolated):
    cfg = ServerConfig(_env_file=None, cache_ttl=5)
    assert cfg.cache_ttl == 5
    cfg.cache_ttl = 6
    assert cfg.cache_ttl == 6


def test_data_dir_binds_from_env(_isolated, monkeypatch):
    target = Path(_isolated) / "somewhere" / "data"
    monkeypatch.setenv("DATA_DIR", str(target))
    cfg = ServerConfig(_env_file=None)
    assert ServerConfig.model_fields["data_dir"].alias == "DATA_DIR"
    assert cfg.data_dir == str(target)


def test_documented_unaliased_names_still_bind(_isolated, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DB_PATH", str(Path(_isolated) / "x.db"))
    cfg = ServerConfig(_env_file=None)
    assert cfg.environment == "production"
    assert cfg.db_path == str(Path(_isolated) / "x.db")


def test_operator_alias_binds(_isolated, monkeypatch):
    monkeypatch.setenv("MAX_BROWSER_CONTEXTS", "77")
    monkeypatch.setenv("BROWSER_HEADLESS", "off")
    monkeypatch.setenv("COMPACTION_ENABLED", "off")  # internal: no alias, not env-bound
    cfg = ServerConfig(_env_file=None)
    assert cfg.max_browser_contexts == 77
    assert cfg.browser_headless is False
    assert cfg.get("COMPACTION_ENABLED", True) is True


def test_field_name_kwarg_reaches_aliased_field(_isolated):
    # populate_by_name: a constructor kwarg by field name is honoured.
    cfg = ServerConfig(_env_file=None, data_dir=str(Path(_isolated) / "d"))
    assert cfg.data_dir == str(Path(_isolated) / "d")


def test_agent_config_follows_the_same_rule(_isolated, monkeypatch):
    monkeypatch.setenv("BASE_DIR", "/nonexistent")
    monkeypatch.setenv("DATA_DIR", "/tmp/dd")
    cfg = AgentConfig(_env_file=None)
    assert cfg.base_dir != "/nonexistent"
    assert cfg.data_dir == "/tmp/dd"


def test_every_env_bound_field_declares_an_alias(monkeypatch):
    """The only env names ServerConfig can read are its aliases."""
    from pydantic_settings import EnvSettingsSource
    unaliased = [n for n, f in ServerConfig.model_fields.items() if f.alias is None]
    assert unaliased, "expected some internal (alias-less) fields"
    for name in unaliased:
        monkeypatch.setenv(name.upper(), "1")
    src = ServerConfig.settings_customise_sources(
        ServerConfig,
        init_settings=None,
        env_settings=EnvSettingsSource(ServerConfig),
        dotenv_settings=None,
        file_secret_settings=None,
    )
    env_src = [s for s in src if isinstance(s, EnvSettingsSource)][0]
    read = [n for n in unaliased if env_src.get_field_value(ServerConfig.model_fields[n], n)[0] is not None]
    assert not read, f"alias-less fields still read from the env: {read}"
