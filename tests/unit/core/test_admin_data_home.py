"""031: an owner-control verb must never act on — or report success for — a data
home the running daemon does not read.

`resolve_data_home()` deliberately never applies the server default
`/var/lib/polyrob`; with `POLYROB_DATA_DIR` absent from the invoking shell it
resolves to `cwd/.polyrob`. An owner who SSHes to the production box and runs
`polyrob autonomy pause` from `~` therefore wrote the pause record into
`/root/.polyrob/`, read it back from the same wrong place, and got a confident
"Paused everything" the daemon never saw.
"""
import pytest

from core.admin_data_home import (
    AmbiguousDataHome,
    admin_data_home,
    resolve_admin_data_home,
)


@pytest.fixture
def nothing_deployed(tmp_path, monkeypatch):
    """A pure-local developer box: no deployment env file, no units."""
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.setattr("core.admin_data_home.DEPLOYED_ENV_FILE",
                        str(tmp_path / "absent" / "polyrob.env"))
    monkeypatch.setattr("core.admin_data_home.UNIT_DIRS",
                        (str(tmp_path / "no-units"),))
    local = tmp_path / "local_home"
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: local)
    return local


def _deploy(tmp_path, monkeypatch, *, body, with_unit=True):
    envf = tmp_path / "etc" / "polyrob.env"
    envf.parent.mkdir(parents=True, exist_ok=True)
    envf.write_text(body, encoding="utf-8")
    monkeypatch.setattr("core.admin_data_home.DEPLOYED_ENV_FILE", str(envf))
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    if with_unit:
        (units / "polyrob.service").write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr("core.admin_data_home.UNIT_DIRS", (str(units),))
    return envf


# --- the pure-local developer experience must not change ---------------------

def test_local_box_is_unchanged_and_silent(nothing_deployed):
    res = resolve_admin_data_home()
    assert res.path == str(nothing_deployed)
    assert res.source == "local"
    assert res.ambiguous is False
    assert res.message == ""
    assert admin_data_home() == str(nothing_deployed)


# --- an explicit POLYROB_DATA_DIR always wins, deployment or not -------------

def test_explicit_env_wins(tmp_path, monkeypatch, nothing_deployed):
    _deploy(tmp_path, monkeypatch, body="POLYROB_DATA_DIR=/var/lib/polyrob\n")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "pinned"))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home",
                        lambda: tmp_path / "pinned")
    res = resolve_admin_data_home()
    assert res.source == "env"
    assert res.path == str(tmp_path / "pinned")
    assert res.ambiguous is False


# --- adopt the deployed home when it can be read unambiguously ---------------

def test_deployed_data_home_is_adopted(tmp_path, monkeypatch, nothing_deployed):
    _deploy(tmp_path, monkeypatch,
            body='# comment\nexport POLYROB_DATA_DIR="/var/lib/polyrob"\nTOKEN=secret\n')
    res = resolve_admin_data_home()
    assert res.source == "deployed"
    assert res.path == "/var/lib/polyrob"
    assert res.ambiguous is False
    assert "/var/lib/polyrob" in res.message and str(nothing_deployed) in res.message
    assert "secret" not in res.message, "the deployment env file must never be echoed"


def test_deployed_home_equal_to_local_is_not_flagged(tmp_path, monkeypatch,
                                                     nothing_deployed):
    _deploy(tmp_path, monkeypatch,
            body=f"POLYROB_DATA_DIR={nothing_deployed}\n")
    res = resolve_admin_data_home()
    assert res.ambiguous is False
    assert res.message == ""
    assert res.path == str(nothing_deployed)


# --- refuse when a deployment exists but its home cannot be read -------------

def test_unit_without_a_readable_data_home_refuses(tmp_path, monkeypatch,
                                                   nothing_deployed):
    _deploy(tmp_path, monkeypatch, body="TELEGRAM_BOT_TOKEN=x\n")
    res = resolve_admin_data_home()
    assert res.ambiguous is True
    assert str(nothing_deployed) in res.message
    assert "set -a" in res.message and "POLYROB_DATA_DIR" in res.message
    with pytest.raises(AmbiguousDataHome):
        admin_data_home()


def test_unit_with_no_env_file_at_all_refuses(tmp_path, monkeypatch,
                                              nothing_deployed):
    units = tmp_path / "units2"
    units.mkdir()
    (units / "polyrob-webview.service").write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr("core.admin_data_home.UNIT_DIRS", (str(units),))
    res = resolve_admin_data_home()
    assert res.ambiguous is True
    with pytest.raises(AmbiguousDataHome) as exc:
        admin_data_home()
    assert str(nothing_deployed) in str(exc.value)


# --- the adoption note reaches the owner exactly once ------------------------

def test_adoption_note_is_echoed_once(tmp_path, monkeypatch, nothing_deployed):
    _deploy(tmp_path, monkeypatch, body="POLYROB_DATA_DIR=/var/lib/polyrob\n")
    from core import admin_data_home as mod
    mod.reset_admin_data_home_notes()
    seen = []
    assert admin_data_home(echo=seen.append) == "/var/lib/polyrob"
    assert admin_data_home(echo=seen.append) == "/var/lib/polyrob"
    assert len(seen) == 1, seen
