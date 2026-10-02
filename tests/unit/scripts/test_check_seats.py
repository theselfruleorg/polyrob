"""`scripts/check_seats.py` — the exit code IS the answer.

⚠️ The distinction this script exists to make (2026-09-24): "the check ran
fine" and "Rob can think" are different facts. A loop that only looked at
whether the command succeeded would have read a clean run against two empty
accounts as good news — which is exactly the shape of the mistake that put
wrong advice in that morning's owner digest.
"""
import importlib.util
import json
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "check_seats.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_seats", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cs():
    return _load()


def _verdict(provider, state, free=True, detail="d"):
    from modules.llm.seat_probe import SeatVerdict
    return SeatVerdict(provider=provider, state=state, detail=detail,
                       measured_at=1.0, free_check=free)


def _fake_probe(states):
    def _p(name, *, api_key, **kw):
        return _verdict(name, states[name])
    return _p


# --- the exit code ---------------------------------------------------------- #

def test_exit_zero_when_a_seat_can_serve(cs, monkeypatch, capsys):
    monkeypatch.setattr(cs, "check", lambda: [_verdict("openrouter", "ok"),
                                              _verdict("zai-coding", "no_credit")])
    assert cs.main([]) == 0


def test_exit_one_when_every_seat_is_dry(cs, monkeypatch, capsys):
    """The live state at 09:56Z: both accounts empty."""
    monkeypatch.setattr(cs, "check", lambda: [_verdict("openrouter", "no_credit"),
                                              _verdict("zai-coding", "no_credit")])
    assert cs.main([]) == 1
    assert "NONE" in capsys.readouterr().out


def test_unknown_does_not_count_as_usable(cs, monkeypatch, capsys):
    """A seat we could not measure must never make the loop think it is fine."""
    monkeypatch.setattr(cs, "check", lambda: [_verdict("openrouter", "unknown"),
                                              _verdict("zai-coding", "unreachable")])
    assert cs.main([]) == 1


# --- what it reads ---------------------------------------------------------- #

def test_the_key_comes_from_the_environment_per_seat(cs):
    seen = {}

    def _probe(name, *, api_key, **kw):
        seen[name] = api_key
        return _verdict(name, "ok")

    cs.check(env={"OPENROUTER_API_KEY": "a", "ZAI_API_KEY": "b"}, probe=_probe)
    assert seen == {"openrouter": "a", "zai-coding": "b"}


def test_a_missing_key_is_passed_as_empty_not_as_none(cs):
    """`seat_probe` reports `unknown` for an empty key; None would read as a
    programming error instead of a missing seat."""
    seen = {}

    def _probe(name, *, api_key, **kw):
        seen[name] = api_key
        return _verdict(name, "unknown")

    cs.check(env={}, probe=_probe)
    assert set(seen.values()) == {""}


# --- what it prints --------------------------------------------------------- #

def test_each_line_says_whether_the_probe_cost_anything(cs):
    out = cs.render([_verdict("openrouter", "no_credit", free=True),
                     _verdict("zai-coding", "no_credit", free=False)])
    assert "free" in out and "cost a token" in out


def test_the_json_form_is_machine_readable(cs, monkeypatch, capsys):
    monkeypatch.setattr(cs, "check", lambda: [_verdict("openrouter", "ok")])
    cs.main(["--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["provider"] == "openrouter"
    assert rows[0]["state"] == "ok"


def test_the_summary_names_the_usable_seats(cs):
    out = cs.render([_verdict("openrouter", "no_credit"), _verdict("zai-coding", "ok")])
    assert "usable now: zai-coding" in out
