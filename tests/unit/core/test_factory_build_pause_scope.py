"""An oversight pause holds the factory build; a `release` record from before the removal pauses nothing."""
import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    return str(tmp_path)


def test_an_oversight_pause_holds_the_factory_build(home):
    from core import autonomy_control as ac
    assert ac.KIND_SCOPES["factory_build"] == ("all", "oversight")
    ac.pause(home, scopes=("oversight",))
    assert not ac.allows("factory_build", home).allowed


def test_release_is_no_longer_an_owner_scope():
    from core.autonomy_control import SCOPES
    from core.surfaces.owner_intent import parse_pause_args
    assert "release" not in SCOPES
    with pytest.raises(ValueError):
        parse_pause_args(["release"])
