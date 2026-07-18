"""Context-aware hint line: idle vs mid-turn vs slash-usage, rotating tip."""
from types import SimpleNamespace

from cli.ui.hints import TIPS, hint_fragments


class _Lifecycle:
    def __init__(self, active):
        self._active = active
    def is_active(self):
        return self._active


def _state(active=False):
    return SimpleNamespace(lifecycle=_Lifecycle(active))


def _text(frags):
    return "".join(t for _, t in frags)


def test_mid_turn_shows_stop_hint():
    out = _text(hint_fragments(_state(active=True), "", 0.0))
    assert "^C stop" in out
    assert "⏎ send" not in out


def test_idle_shows_send_and_tip():
    out = _text(hint_fragments(_state(), "", 0.0))
    assert "⏎ send" in out
    assert TIPS[0] in out


def test_tip_rotates_with_clock():
    a = _text(hint_fragments(_state(), "", 0.0))
    b = _text(hint_fragments(_state(), "", 12.5))
    assert TIPS[0] in a and TIPS[1] in b


def test_valid_slash_shows_usage(monkeypatch):
    import cli.ui.hints as hints
    monkeypatch.setattr(
        hints, "_usage_for", lambda w: "/model <provider> <model> — swap the model"
    )
    out = _text(hint_fragments(_state(), "/model glm", 0.0))
    assert "swap the model" in out


def test_unknown_slash_falls_back_to_default(monkeypatch):
    import cli.ui.hints as hints
    monkeypatch.setattr(hints, "_usage_for", lambda w: "")
    out = _text(hint_fragments(_state(), "/nope", 0.0))
    assert "⏎ send" in out


def test_fragments_use_hint_classes():
    frags = hint_fragments(_state(), "", 0.0)
    assert all(cls.startswith("class:prompt.hint") for cls, _ in frags)
