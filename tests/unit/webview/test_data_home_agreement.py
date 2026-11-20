"""A-16: with ``POLYROB_DATA_DIR`` unset, the console's avatar read and every other
console read resolve ONE data home (the avatar used a second rule)."""


def test_pages_and_webgate_resolve_the_same_home_with_the_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    # A server-shaped process (not local mode): the avatar's own rule read the CLI
    # home here while every other console read took the server default.
    import core.config_policy as cp
    monkeypatch.setattr(cp, "local_mode_enabled", lambda: False)
    import core.runtime_paths as rp
    monkeypatch.setattr(rp, "_server_default_data_home", lambda: tmp_path / "srv")
    (tmp_path / "home" / "data").mkdir(parents=True)
    from webview import pages, webgate
    assert pages._avatar_data_dir() == webgate.data_dir()


def test_pages_and_webgate_resolve_the_same_home_with_the_env_set(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "srv"))
    from webview import pages, webgate
    assert pages._avatar_data_dir() == webgate.data_dir() == str((tmp_path / "srv").resolve())
