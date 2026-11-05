"""103: a reply cannot send the owner to a command that does not exist."""
from types import SimpleNamespace

import pytest

from core.surfaces.verb_check import unknown_verbs, verb_note
from tools.controller.send_message_route import send_text_refusal


@pytest.mark.parametrize("text", [
	"Run /adopt abc123 to fix it.", "Fix: `/adopt`", "(/adopt)", "/adopt"])
def test_a_retired_verb_is_caught_however_it_is_written(text):
	assert unknown_verbs(text) == ["/adopt"]
	assert "needs no adoption" in verb_note("/adopt")


@pytest.mark.parametrize("text", [
	"Tap /cards or /pending.", "/card_12ab34cd_ok", "/approve_p_371ed9", "/reject_p_1",
	"see https://example.com/adopt now", "the file /var/lib/data/adopt.md", "and/or",
	"data/promo/calendar.md", "no slash here"])
def test_a_real_verb_a_tap_token_a_url_and_a_path_pass(text):
	assert unknown_verbs(text) == []


def _ctl():
	return SimpleNamespace(orchestrator=None, session_id="s-chat")


def test_the_send_is_refused_once_with_the_reason_then_goes_out():
	ctl = _ctl()
	first = send_text_refusal(ctl, "Run /adopt abc.")
	assert "NOT SENT" in first.extracted_content and "needs no adoption" in first.extracted_content
	assert send_text_refusal(ctl, "Run /adopt abc.") is None


def test_a_clean_reply_is_not_touched():
	assert send_text_refusal(_ctl(), "Done. /cards shows the rest.") is None


@pytest.mark.parametrize("text", [
	"Send /buy to order.", "/offers", "/order abc", "/more_3f9a1c2b", "/skill_my_skill",
	"/fulfill_abc123_a"])
def test_a_buyer_verb_and_a_read_token_pass(text):
	assert unknown_verbs(text) == []


def test_the_storefront_verbs_match_the_seat_that_answers_them():
	from pathlib import Path
	from core.surfaces.verb_check import STOREFRONT_VERBS
	seat = Path(__file__).resolve().parents[4] / "surfaces" / "telegram" / "storefront_ops.py"
	text = seat.read_text(encoding="utf-8")
	assert all(verb in text for verb in STOREFRONT_VERBS)


@pytest.mark.asyncio
async def test_the_message_tool_refuses_once_too(monkeypatch):
	from tools.controller import message_send as M
	sent = []

	async def fake_send(**kw):
		sent.append(kw["text"])
		return {"success": True}

	monkeypatch.setattr(M, "perform_message_send", fake_send)
	ctl = _ctl()
	first = await M.perform_message_action(text="Run /adopt abc.", controller=ctl,
	                                       surface="telegram", target="owner")
	assert first["success"] is False and "NOT SENT" in first["error"] and not sent
	second = await M.perform_message_action(text="Run /adopt abc.", controller=ctl,
	                                        surface="telegram", target="owner")
	assert second == {"success": True} and sent == ["Run /adopt abc."]


def test_the_terminal_names_a_retired_verb():
	import asyncio
	from cli.ui.commands.registry import CommandRegistry
	lines = []
	ctx = SimpleNamespace(emit=lines.append)
	asyncio.run(CommandRegistry().dispatch("/adopt", ctx))
	assert any("needs no adoption" in line for line in lines)
