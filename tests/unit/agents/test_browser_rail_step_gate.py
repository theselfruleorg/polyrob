"""The step loop observes page state only when the browser rail is usable, and
says so ONCE per agent — not three ERRORs per step (prod 2026-09-17: ~200/h)."""
import logging

from agents.task.agent.core.step import StepMixin
from core.security import browser_rail as br


class _Agent(StepMixin):
    def __init__(self):
        self.logger = logging.getLogger("test.step")


def test_unusable_rail_skips_and_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(br, "browser_rail_status",
                        lambda **k: br.BrowserRailStatus("unset", "local", True))
    a = _Agent()
    with caplog.at_level(logging.WARNING, logger="test.step"):
        assert a._browser_rail_usable() is False
        assert a._browser_rail_usable() is False
        assert a._browser_rail_usable() is False
    warns = [r for r in caplog.records if "Browser rail unavailable" in r.getMessage()]
    assert len(warns) == 1
    assert "polyrob browser install" in warns[0].getMessage()


def test_usable_rail_observes(monkeypatch):
    monkeypatch.setattr(br, "browser_rail_status",
                        lambda **k: br.BrowserRailStatus("ok", "cdp", True))
    assert _Agent()._browser_rail_usable() is True


def test_resolver_failure_fails_open(monkeypatch):
    monkeypatch.setattr(br, "browser_rail_status",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _Agent()._browser_rail_usable() is True
