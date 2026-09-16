"""C10 (2026-09-15 prod review): preferences are written and read on ONE axis.

`BotConfig.data_dir` defaults to the RELATIVE string "data", and
`_ensure_directories` anchors a relative default to `POLYROB_DATA_DIR`. On prod
that makes `config.data_dir == /var/lib/polyrob/data` while
`resolve_data_home() == /var/lib/polyrob` — a shadow one level down, carrying
its own empty conversations.db / correspondents.db / outbox.db / surfaces.db.

Every preference WRITER (the console `/config`, `polyrob config set`, the REPL,
the agent's `prefs` action) resolves the data home. The delivery rail's two
readers derived their home from `config.data_dir` instead, so
`delivery.daily_cap`, `delivery.rate_per_hour` and quiet hours were written to
one tree and read from another — the owner's remedy for "too many messages
suppressed" changed nothing at all.
"""
import pytest


class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)

    def get_service(self, name):
        return None


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    return tmp_path


def test_delivery_rail_reads_prefs_from_the_data_home(tmp_path):
    """Exactly prod's shape: the container reports the `data/` shadow."""
    from core.surfaces.user_delivery import _home_dir_for_container
    shadow = _Container(str(tmp_path / "data"))
    assert _home_dir_for_container(shadow) == str(tmp_path)


def test_message_send_reads_prefs_from_the_data_home(tmp_path):
    from tools.controller.message_send import _pref_home_dir
    shadow = _Container(str(tmp_path / "data"))
    assert _pref_home_dir(shadow) == str(tmp_path)


def test_an_owner_cap_written_by_a_seat_is_read_by_the_rail(tmp_path):
    """End to end: the owner raises the cap, the rail honours it."""
    from core.prefs import write_preference
    from core.surfaces.user_delivery import effective_daily_cap, _home_dir_for_container
    ok, err = write_preference(str(tmp_path), "12345", "delivery.daily_cap", 60)
    assert ok, err
    home = _home_dir_for_container(_Container(str(tmp_path / "data")))
    assert effective_daily_cap("12345", home) == 60


def test_no_container_still_resolves_the_data_home(tmp_path):
    from core.surfaces.user_delivery import _home_dir_for_container
    assert _home_dir_for_container(None) == str(tmp_path)


# --- C12: the owner must be able to RAISE their own message budget ----------
#
# `delivery.daily_cap`/`delivery.rate_per_hour` merge `min(pref, env)`, and the
# accessors passed `env_value=_daily_cap()`, which returns the FRAMEWORK DEFAULT
# (30) even when USER_DELIVERY_DAILY_CAP is unset — as it is on prod. So the
# min-merge always clamped against 30 and `/config set delivery.daily_cap 60`
# did nothing. The status health item for a capped message literally advises
# "raise the cap with `/config set delivery.daily_cap N`".
#
# The min-merge is right when the OPERATOR set a ceiling: a pref must not widen
# past what the deployment allowed. With no operator value there is no ceiling
# to widen past — the same reasoning `narrow_list` already states for an empty
# operator set.

def test_owner_can_raise_the_daily_cap_when_the_operator_set_none(tmp_path, monkeypatch):
    monkeypatch.delenv("USER_DELIVERY_DAILY_CAP", raising=False)
    from core.prefs import write_preference
    from core.surfaces.user_delivery import effective_daily_cap
    assert write_preference(str(tmp_path), "12345", "delivery.daily_cap", 60)[0]
    assert effective_daily_cap("12345", str(tmp_path)) == 60


def test_owner_can_lower_the_daily_cap(tmp_path, monkeypatch):
    monkeypatch.delenv("USER_DELIVERY_DAILY_CAP", raising=False)
    from core.prefs import write_preference
    from core.surfaces.user_delivery import effective_daily_cap
    assert write_preference(str(tmp_path), "12345", "delivery.daily_cap", 5)[0]
    assert effective_daily_cap("12345", str(tmp_path)) == 5


def test_an_operator_ceiling_still_wins(tmp_path, monkeypatch):
    """An explicit deployment value IS a ceiling; a pref may only tighten it."""
    monkeypatch.setenv("USER_DELIVERY_DAILY_CAP", "20")
    from core.prefs import write_preference
    from core.surfaces.user_delivery import effective_daily_cap
    assert write_preference(str(tmp_path), "12345", "delivery.daily_cap", 60)[0]
    assert effective_daily_cap("12345", str(tmp_path)) == 20


def test_the_same_holds_for_the_hourly_rate(tmp_path, monkeypatch):
    monkeypatch.delenv("USER_DELIVERY_RATE_PER_HOUR", raising=False)
    from core.prefs import write_preference
    from core.surfaces.user_delivery import effective_rate_per_hour
    assert write_preference(str(tmp_path), "12345", "delivery.rate_per_hour", 25)[0]
    assert effective_rate_per_hour("12345", str(tmp_path)) == 25
    monkeypatch.setenv("USER_DELIVERY_RATE_PER_HOUR", "4")
    assert effective_rate_per_hour("12345", str(tmp_path)) == 4


def test_no_pref_is_byte_identical_to_the_env_default(tmp_path, monkeypatch):
    monkeypatch.delenv("USER_DELIVERY_DAILY_CAP", raising=False)
    from core.surfaces.user_delivery import _daily_cap, effective_daily_cap
    assert effective_daily_cap("12345", str(tmp_path)) == _daily_cap()
