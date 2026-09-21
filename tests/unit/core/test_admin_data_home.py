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


# --- 2026-09-20 07:32Z: an UNREADABLE deployed env file is not "nothing declared" -------

def _unreadable_deploy(tmp_path, monkeypatch):
    """The prod shape: /etc/polyrob/polyrob.env exists (secrets, root 0600) and
    the verb runs as the service user under sudo, which cannot read it."""
    import core.admin_data_home as adh
    envf = _deploy(tmp_path, monkeypatch,
                   body="POLYROB_DATA_DIR=/var/lib/x\nPOLYROB_INSTANCE_ID=rob\nPOLYROB_OWNER_USER_ID=rob\n")
    real_open = open

    def _denied(path, *a, **kw):
        if str(path) == str(envf):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", _denied)
    monkeypatch.delenv("POLYROB_INSTANCE_ID", raising=False)
    monkeypatch.delenv("BOT_INSTANCE_ID", raising=False)
    monkeypatch.delenv("POLYROB_PROFILE", raising=False)
    monkeypatch.delenv("POLYROB_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("BOT_OWNER_USER_ID", raising=False)
    return adh


def test_unreadable_env_file_refuses_instead_of_guessing_the_instance(tmp_path, monkeypatch):
    """`sudo -u polyrob-agent … polyrob owner promote owner_doc rob` resolved
    instance 'polyrob' (the default) because EACCES read as 'not declared', and
    wrote into identity/polyrob/user_rob/ — a shadow tree nothing reads — while
    reporting 'no pending owner-facts doc'. Unreadable is 'cannot tell': refuse
    and name the remedy."""
    adh = _unreadable_deploy(tmp_path, monkeypatch)
    with pytest.raises(adh.DeployedEnvUnreadable) as ei:
        adh.admin_instance_id()
    msg = str(ei.value)
    assert "POLYROB_INSTANCE_ID" in msg and "sudo -u polyrob-agent" in msg
    with pytest.raises(adh.DeployedEnvUnreadable) as ei2:
        adh.admin_owner_principal()
    assert "POLYROB_OWNER_USER_ID" in str(ei2.value)
    # an explicit shell value still wins, exactly as before
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    assert adh.admin_instance_id() == "rob" and adh.admin_owner_principal() == "rob"


def test_absent_env_file_is_still_a_local_box(nothing_deployed):
    """No deployment at all: the fallbacks stay — a dev checkout is untouched."""
    import core.admin_data_home as adh
    assert adh.deployed_env_value("POLYROB_INSTANCE_ID") is None
    assert isinstance(adh.admin_instance_id(), str)
    assert isinstance(adh.admin_owner_principal(), str)
