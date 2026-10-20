"""080 WS-1 item 6 — one data dir: ``config.data_dir`` IS the data home
(``core.runtime_paths.effective_data_home``), never ``<POLYROB_DATA_DIR>/data``
or ``<code root>/data``; console pages and console chat verbs read one store."""
from pathlib import Path


def _cfg():
    from core.config import BotConfig
    return BotConfig()


def test_data_dir_is_the_data_home_on_a_server(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)
    cfg = _cfg()
    assert Path(cfg.data_dir) == (tmp_path / "home").resolve()
    assert Path(cfg.db_path) == (tmp_path / "home").resolve() / "database" / "bot.db"
    assert not (tmp_path / "home" / "data").exists()


def test_data_dir_never_falls_back_into_the_code_tree(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "ph"))
    monkeypatch.chdir(tmp_path)  # the suite's local data home is cwd/.polyrob (conftest)
    cfg = _cfg()
    from core.runtime_paths import _CODE_ROOT, effective_data_home
    assert Path(cfg.data_dir) == effective_data_home()
    assert _CODE_ROOT not in Path(cfg.data_dir).parents


def test_console_pages_and_console_chat_verbs_read_one_store(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("DATA_DIR", raising=False)
    from types import SimpleNamespace

    from surfaces.telegram.harness import _admin_data_dir
    from webview import webgate
    agent = SimpleNamespace(container=SimpleNamespace(config=_cfg()))
    assert Path(_admin_data_dir(agent)) == Path(webgate.data_dir()).resolve()
