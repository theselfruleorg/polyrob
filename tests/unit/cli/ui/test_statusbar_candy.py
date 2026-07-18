"""Status-bar micro-candy: ctx% thresholds, cooking verb rotation."""
from cli.ui import statusbar
from cli.ui.state import SessionState


class _Lifecycle:
    def __init__(self, active=False, elapsed=0.0):
        self._active, self._elapsed = active, elapsed
    def is_active(self):
        return self._active
    def active_elapsed(self):
        return self._elapsed
    def autonomy_busy(self):
        return False


def _state(**kw):
    s = SessionState()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_ctx_class_thresholds():
    assert statusbar._ctx_class(30.0) == "class:toolbar.ctx"
    assert statusbar._ctx_class(79.9) == "class:toolbar.ctx"
    assert statusbar._ctx_class(80.0) == "class:toolbar.ctx.warn"
    assert statusbar._ctx_class(90.0) == "class:toolbar.ctx.high"


def test_status_formatted_uses_ctx_class():
    s = _state(ctx_percent=85.0)
    frags = statusbar.status_formatted(s)
    classes = {cls for cls, _ in frags}
    assert "class:toolbar.ctx.warn" in classes


def test_cooking_verb_stable_first_20s():
    s = _state(lifecycle=_Lifecycle(active=True, elapsed=5.0))
    assert "cooking…" in statusbar.cooking_text(s)


def test_cooking_verb_rotates_after_20s():
    s = _state(lifecycle=_Lifecycle(active=True, elapsed=25.0))
    text = statusbar.cooking_text(s)
    assert "cooking…" not in text
    assert any(v + "…" in text for v in statusbar._COOKING_VERBS)


def test_cooking_glyph_from_theme():
    from cli.ui.theme import ICONS
    s = _state(lifecycle=_Lifecycle(active=True, elapsed=1.0))
    assert statusbar.cooking_text(s).startswith(f"{ICONS.cooking} ")


def test_autonomy_line_without_model_half():
    s = _state(model="glm", provider="openrouter")
    s.autonomy_snapshot = {"goals": 2, "cron": 1, "review": True}
    line = statusbar.autonomy_line(s, include_model=False)
    assert line == "autonomy: goals 2 · cron 1 · review on"
    assert "glm" not in line


def test_autonomy_line_without_model_hides_when_nothing_to_say():
    s = _state(model="glm")
    s.autonomy_snapshot = {"goals": 0, "cron": 0, "review": False}
    assert statusbar.autonomy_line(s, include_model=False) == ""


def test_autonomy_line_default_keeps_model_half():
    s = _state(model="glm", provider="openrouter")
    s.autonomy_snapshot = {"goals": 1}
    assert statusbar.autonomy_line(s).startswith("glm · openrouter")
