"""One shared resolver for the runtime DATA HOME (goals.db/cron.db/memory.db/...).

The 2026-07-12 UI-surface review found four byte-duplicated ``_data_dir()``
helpers (webview pages/activity, cli owner/surface) each re-implementing the
same policy: ``POLYROB_DATA_DIR`` wins, else converge on the CLI/agent home
(``cwd/.polyrob``). ``core.runtime_paths.resolve_data_home()`` is now the
single policy seam; call-site wrappers may only add deploy-topology import
guards, never their own resolution logic.

This is the SECOND path axis, distinct from ``resolve_session_data_root()``
(the PathManager/DATA_ROOT session-artifact tree).
"""
from pathlib import Path


def test_env_set_wins(monkeypatch, tmp_path):
    from core.runtime_paths import resolve_data_home

    home = tmp_path / "home"
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    assert resolve_data_home() == home.resolve()


def test_env_unset_converges_on_cli_home(monkeypatch, tmp_path):
    from core.runtime_paths import resolve_data_home

    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_data_home() == (tmp_path / ".polyrob").resolve()


def test_matches_bootstrap_resolution(monkeypatch, tmp_path):
    """The seam must agree with what build_cli_container actually uses, so the
    CLI admin verbs and the running daemons read the SAME sidecar DBs."""
    from core.bootstrap import _resolve_cli_data_home
    from core.runtime_paths import resolve_data_home

    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    bootstrap_home, _ws, _root = _resolve_cli_data_home()
    assert resolve_data_home() == Path(bootstrap_home).resolve()


def test_three_resolvers_agree_across_env_cases(monkeypatch, tmp_path):
    """resolve_data_home / bootstrap._resolve_cli_data_home / runtime_config.
    get_data_root are ONE rule (bootstrap and get_data_root delegate here).
    POLYROB_PROJECT_DIR moves only the WORKSPACE placement — never the data home."""
    from core.bootstrap import _resolve_cli_data_home
    from core.runtime_config import get_data_root
    from core.runtime_paths import resolve_data_home

    # Headless: DATA_DIR set — data home is DATA_DIR even with PROJECT_DIR present.
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("POLYROB_PROJECT_DIR", str(tmp_path / "proj"))
    expected = (tmp_path / "data").resolve()
    assert resolve_data_home() == expected
    assert Path(_resolve_cli_data_home()[0]) == expected
    assert Path(get_data_root()) == expected

    # PROJECT_DIR only — data home stays cwd/.polyrob (workspace moved, data not).
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    expected = (tmp_path / ".polyrob").resolve()
    assert resolve_data_home() == expected
    assert Path(_resolve_cli_data_home()[0]) == expected
    assert Path(get_data_root()) == expected


def test_cli_owner_and_surface_share_the_seam(monkeypatch, tmp_path):
    """owner.py / surface.py had their own copy-pasted ``_data_dir()``; they
    must now resolve through the shared seam (same value, both env and unset)."""
    from cli.commands.owner import _data_dir as owner_data_dir
    from cli.commands.surface import _data_dir as surface_data_dir
    from core.runtime_paths import resolve_data_home

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    assert owner_data_dir() == surface_data_dir() == str(resolve_data_home())

    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert owner_data_dir() == surface_data_dir() == str(resolve_data_home())
