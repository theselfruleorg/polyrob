"""043 A11/A19: plain-word pause mapping.

The owner rejected the raw scope tokens ("trading streams planner cron social
oversight pings apps") as vocabulary; every seat now also takes five plain
words. ``parse_pause_args`` stays a 2-tuple (``scopes, duration_minutes``) —
it already raises ``ValueError`` on anything it does not recognize, so a
3-tuple ``unknown`` return is not needed.
"""
import pytest


def test_each_plain_word_maps_to_its_scopes():
    from core.surfaces.owner_intent import PLAIN_WORDS, parse_pause_args
    assert PLAIN_WORDS == {
        "everything": ("all",),
        "trading": ("trading",),
        "background": ("streams", "planner", "cron"),
        "messages": ("pings",),
        "deploying": ("apps",),
    }
    # dict == is order-blind; the refusal message lists PLAIN_WORDS in
    # insertion order, so pin the order explicitly too.
    assert list(PLAIN_WORDS) == [
        "everything", "trading", "background", "messages", "deploying"]
    assert parse_pause_args(["everything"]) == (("all",), None)
    assert parse_pause_args(["trading"]) == (("trading",), None)
    assert parse_pause_args(["background"]) == (("streams", "planner", "cron"), None)
    assert parse_pause_args(["messages"]) == (("pings",), None)
    assert parse_pause_args(["deploying"]) == (("apps",), None)


def test_oversight_has_no_plain_word():
    """A8/A42: oversight's kinds are dormant — no plain word is offered for it."""
    from core.surfaces.owner_intent import PLAIN_WORDS
    assert "oversight" not in PLAIN_WORDS
    assert all("oversight" not in scopes for scopes in PLAIN_WORDS.values())


def test_raw_scope_tokens_still_accepted():
    from core.surfaces.owner_intent import parse_pause_args
    assert parse_pause_args(["trading", "streams"]) == (("trading", "streams"), None)
    assert parse_pause_args(["social"]) == (("social",), None)
    assert parse_pause_args(["ALL"]) == (("all",), None)


def test_unknown_word_refuses_with_valueerror():
    from core.surfaces.owner_intent import parse_pause_args
    with pytest.raises(ValueError) as exc:
        parse_pause_args(["backgrund"])
    msg = str(exc.value)
    # the five plain words come first, then the raw-scope fallback
    assert "everything" in msg and "trading" in msg and "background" in msg
    assert "messages" in msg and "deploying" in msg
    assert "(or a scope:" in msg


def test_plain_word_with_duration_still_parses():
    from core.surfaces.owner_intent import parse_pause_args
    assert parse_pause_args(["background", "for", "6h"]) == (
        ("streams", "planner", "cron"), 360)


def test_pause_rejects_an_unknown_scope_instead_of_dropping_it(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from core import autonomy_control as ac
    with pytest.raises(ValueError):
        ac.pause(str(tmp_path), scopes=("nonsense",))
    # nothing was written — a refusal must not silently widen to "all"
    assert not (tmp_path / ac.PAUSE_FILENAME).exists()


def test_resume_rejects_an_unknown_scope_instead_of_dropping_it(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from core import autonomy_control as ac
    ac.pause(str(tmp_path), scopes=("cron",))
    with pytest.raises(ValueError):
        ac.resume(str(tmp_path), scopes=("nonsense",))
    # the real pause is still there, untouched
    assert ac.read_state(str(tmp_path)).scopes == ("cron",)


def test_pause_empty_scopes_still_defaults_to_all(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from core import autonomy_control as ac
    res = ac.pause(str(tmp_path), scopes=())
    assert res.state.scopes == ("all",)


def test_telegram_pause_background_pauses_streams_planner_and_cron(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    from core.autonomy_control import allows, read_state
    from surfaces.telegram.harness import _pause_reply

    text = _pause_reply(str(tmp_path), "/pause", ["background"])
    assert text.startswith("⏸ Paused")
    st = read_state(str(tmp_path))
    assert set(st.scopes) == {"streams", "planner", "cron"}
    assert allows("seed_stream", str(tmp_path)).allowed is False
    assert allows("plan", str(tmp_path)).allowed is False
    assert allows("cron_run", str(tmp_path)).allowed is False
    # trading (not part of "background") stays live
    assert allows("trade_entry", str(tmp_path)).allowed is True
