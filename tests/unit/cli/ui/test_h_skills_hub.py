"""067 P6: `/skills tap|search|update` mirror `polyrob skill tap|search|update`
through the same library (cli.commands.skill_hub)."""
from __future__ import annotations

import asyncio
import io

from cli.commands import skill_hub
from cli.ui.commands.h_skills import h_skills
from cli.ui.commands.registry import CommandContext
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


def _ctx(args):
    buf = io.StringIO()
    state = SessionState()
    return CommandContext(renderer=PlainRenderer(state=state, stream=buf), state=state,
                          args=args, user_id="local"), buf


def test_tap_list_and_add(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: True)
    ctx, buf = _ctx(["tap", "add", "acme/kit"])
    asyncio.run(h_skills(ctx))
    assert "Added tap acme/kit" in buf.getvalue()
    ctx, buf = _ctx(["tap"])
    asyncio.run(h_skills(ctx))
    out = buf.getvalue()
    assert "anthropics/skills" in out and "acme/kit" in out and "community" in out


def test_tap_add_refused_when_not_local(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: False)
    ctx, buf = _ctx(["tap", "add", "acme/kit"])
    asyncio.run(h_skills(ctx))
    assert "Refused" in buf.getvalue()
    assert [t.id for t in skill_hub.list_taps() if not t.default] == []


def test_search_and_update_use_the_library(monkeypatch):
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: True)
    tap = skill_hub.Tap("anthropics/skills", "trusted", True)
    monkeypatch.setattr(skill_hub, "search", lambda q, refresh=False: [
        skill_hub.TapListing(tap, [skill_hub.TapSkill("docx", "Word files", "skills/docx", tap)])])
    ctx, buf = _ctx(["search", "docx"])
    asyncio.run(h_skills(ctx))
    assert "anthropics/skills/docx — Word files" in buf.getvalue()

    monkeypatch.setattr(skill_hub, "update_skills", lambda name, user_id: [
        skill_hub.UpdateReport(name or "all", "up-to-date")])
    ctx, buf = _ctx(["update", "docx"])
    asyncio.run(h_skills(ctx))
    assert "docx: up-to-date" in buf.getvalue()
