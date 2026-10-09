"""DATA-4 / SUP-2: an unconfigured local data home is the user's own, never cwd/.polyrob."""
import logging
from pathlib import Path

import core.runtime_paths as rp
from core.runtime_paths import _local_default_data_home as REAL_DEFAULT


def _use_real_default(monkeypatch, home):
    monkeypatch.setattr(rp, "_local_default_data_home", REAL_DEFAULT)
    monkeypatch.setenv("POLYROB_HOME", str(home))
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    rp._warned_project_homes.clear()


def test_a_cloned_project_dot_polyrob_is_not_the_data_home(tmp_path, monkeypatch, caplog):
    clone = tmp_path / "clone"
    (clone / ".polyrob").mkdir(parents=True)
    (clone / ".polyrob" / "owner.md").write_text("planted")
    (clone / ".polyrob" / "goals.db").write_text("planted")
    home = tmp_path / "home" / ".polyrob"
    _use_real_default(monkeypatch, home)
    monkeypatch.chdir(clone)
    with caplog.at_level(logging.WARNING, logger="core.runtime_paths"):
        got = rp.resolve_data_home()
    assert got == (home / "data").resolve()
    assert got != (clone / ".polyrob").resolve()
    assert "POLYROB_DATA_DIR=" in caplog.text            # the move is named, not silent
    assert (clone / ".polyrob" / "owner.md").read_text() == "planted"   # data untouched


def test_local_paths_put_config_and_data_in_the_home_and_keep_cwd_as_workspace(tmp_path, monkeypatch):
    home = tmp_path / "h" / ".polyrob"
    _use_real_default(monkeypatch, home)
    monkeypatch.chdir(tmp_path)
    paths = rp.resolve_runtime_paths(local=True)
    assert paths.data_home == (home / "data").resolve()
    assert paths.config_dir == paths.data_home
    assert paths.workspace_root == Path(tmp_path).resolve()


def test_explicit_data_dir_still_wins(tmp_path, monkeypatch):
    _use_real_default(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "srv"))
    assert rp.resolve_data_home() == (tmp_path / "srv").resolve()


def test_no_warning_without_a_legacy_store(tmp_path, monkeypatch, caplog):
    (tmp_path / ".polyrob").mkdir()      # e.g. only the workspace lock dir
    _use_real_default(monkeypatch, tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING, logger="core.runtime_paths"):
        rp.resolve_data_home()
    assert "no longer loads" not in caplog.text


def test_wallet_refuses_rather_than_flipping_scheme_when_meta_stayed_behind(tmp_path, monkeypatch):
    import pytest
    from core.wallet import derivation

    proj = tmp_path / "proj"
    (proj / ".polyrob" / "wallet").mkdir(parents=True)
    (proj / ".polyrob" / "wallet" / "meta.json").write_text('{"derivation": "bip44"}')
    _use_real_default(monkeypatch, tmp_path / "home" / ".polyrob")
    monkeypatch.chdir(proj)
    monkeypatch.setattr(derivation, "wallet_meta_path",
                        lambda data_dir=None: tmp_path / "home" / ".polyrob" / "data" / "wallet" / "meta.json")
    with pytest.raises(ValueError, match="POLYROB_DATA_DIR="):
        derivation.resolve_scheme(env={})
    # an explicit data home is the operator's choice: no refusal
    assert derivation.resolve_scheme(env={"POLYROB_DATA_DIR": str(tmp_path / "x")}) == "legacy"
