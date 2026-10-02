"""064 phase 2: the `release` pause scope — `/pause release` holds the train."""
import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    return str(tmp_path)


def test_release_is_a_scope_and_its_kinds_are_declared():
    from core.autonomy_control import KIND_SCOPES, SCOPES
    assert "release" in SCOPES
    assert KIND_SCOPES["release_cut"] == ("all", "release")
    assert KIND_SCOPES["release_publish"] == ("all", "release")
    assert KIND_SCOPES["factory_build"] == ("all", "oversight")


@pytest.mark.parametrize("scope", ["release", "all"])
def test_a_release_or_full_pause_holds_cut_and_publish(home, scope):
    from core import autonomy_control as ac
    ac.pause(home, scopes=(scope,))
    assert not ac.allows("release_cut", home).allowed
    assert not ac.allows("release_publish", home).allowed


def test_a_trading_pause_does_not_hold_the_train(home):
    from core import autonomy_control as ac
    ac.pause(home, scopes=("trading",))
    assert ac.allows("release_publish", home).allowed


def test_a_release_pause_does_not_stop_other_work(home):
    from core import autonomy_control as ac
    ac.pause(home, scopes=("release",))
    assert ac.allows("dispatch", home).allowed
    assert ac.allows("cron_run", home).allowed


def test_resume_release_lifts_it(home):
    from core import autonomy_control as ac
    ac.pause(home, scopes=("release",))
    ac.resume(home, scopes=("release",))
    assert ac.allows("release_publish", home).allowed


def test_the_owner_verb_parses_release():
    from core.surfaces.owner_intent import parse_pause_args
    assert parse_pause_args(["release"]) == (("release",), None)


def test_an_oversight_pause_holds_the_factory_build(home):
    from core import autonomy_control as ac
    ac.pause(home, scopes=("oversight",))
    assert not ac.allows("factory_build", home).allowed
