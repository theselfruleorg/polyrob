"""`polyrob owner invite` on an already-bound address, `--rebind`, and `owner revoke`.

The seed is idempotent and keeps the stored row, so a second invite with a new
session used to print the NEW session id while the binding stayed on the old one.
"""
import os

import pytest
from click.testing import CliRunner

from cli.commands.owner import owner
from core.surfaces.correspondents import CorrespondentRegistry


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "false")
    return CorrespondentRegistry(os.path.join(str(tmp_path), "correspondents.db"))


def _invite(*args):
    return CliRunner().invoke(owner, ["invite", "discord", "1234", *args,
                                      "--user", "u_owner"])


def test_reinvite_with_other_session_says_nothing_changed(env):
    assert _invite("sess_old").exit_code == 0
    r = _invite("sess_new")
    assert r.exit_code != 0
    assert "already bound to session sess_old" in r.output
    assert "--rebind" in r.output
    assert "-> session sess_new" not in r.output
    assert env.resolve(surface="discord", address="1234")["session_id"] == "sess_old"


def test_rebind_repoints_binding_and_anchors(env):
    assert _invite("sess_old").exit_code == 0
    env.seed_thread_anchor(surface="discord", address="1234", thread_id="m1",
                           session_id="sess_old", user_id="u_owner")
    r = _invite("sess_new", "--rebind")
    assert r.exit_code == 0, r.output
    assert "from session sess_old -> sess_new" in r.output
    assert {row["session_id"] for row in env.list("u_owner")} == {"sess_new"}
    assert env.resolve(surface="discord", address="1234")["session_id"] == "sess_new"


def test_reinvite_same_session_is_idempotent(env):
    assert _invite("sess_a").exit_code == 0
    r = _invite("sess_a")
    assert r.exit_code == 0, r.output
    assert "active" in r.output


def test_invite_warns_on_unknown_session(env):
    r = _invite("no_such_session")
    assert r.exit_code == 0, r.output
    assert "warning: no session no_such_session" in r.output
    assert env.resolve(surface="discord", address="1234") is not None


def test_revoke_stops_routing_including_thread_anchors(env):
    assert _invite("sess_a").exit_code == 0
    env.seed_thread_anchor(surface="discord", address="1234", thread_id="m1",
                           session_id="sess_a", user_id="u_owner")
    r = CliRunner().invoke(owner, ["revoke", "discord", "1234", "--user", "u_owner"])
    assert r.exit_code == 0, r.output
    assert "2 binding row(s)" in r.output
    assert env.resolve(surface="discord", address="1234") is None
    assert env.resolve(surface="discord", address="1234", thread_id="m1") is None


def test_revoke_unknown_correspondent_fails(env):
    r = CliRunner().invoke(owner, ["revoke", "discord", "999", "--user", "u_owner"])
    assert r.exit_code == 1
    assert "no active correspondent" in r.output


def test_reinvite_after_revoke_is_refused_honestly(env):
    assert _invite("sess_a").exit_code == 0
    assert CliRunner().invoke(owner, ["revoke", "discord", "1234",
                                      "--user", "u_owner"]).exit_code == 0
    r = _invite("sess_b")
    assert r.exit_code != 0
    assert "rejected or revoked" in r.output


def test_invite_known_session_has_no_warning(env, tmp_path):
    (tmp_path / "sessions" / "u_owner" / "sess_known").mkdir(parents=True)
    r = _invite("sess_known")
    assert r.exit_code == 0, r.output
    assert "warning" not in r.output


def test_revoke_pending_points_at_reject(env, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "true")
    assert _invite("sess_a").exit_code == 0
    r = CliRunner().invoke(owner, ["revoke", "discord", "1234", "--user", "u_owner"])
    assert r.exit_code == 1
    assert "owner reject correspondent discord:1234" in r.output
    r2 = CliRunner().invoke(owner, ["reject", "correspondent", "discord:1234",
                                    "--user", "u_owner"])
    assert r2.exit_code == 0, r2.output
