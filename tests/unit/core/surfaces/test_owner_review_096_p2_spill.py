"""096 P2: W5 owner-requested spill, W8 partial-send honesty, C1 contacts push."""
import asyncio
import os
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# W5 — one spill, two budgets
# ---------------------------------------------------------------------------

def test_gist_closes_a_fence_it_opened():
    from core.surfaces.spill import build_gist
    body = "Review\n```\n" + "\n".join(f"line {i} " + "x" * 60 for i in range(200)) + "\n```"
    gist = build_gist(body, attachment_name="r.txt", limit=500)
    fences = [ln for ln in gist.splitlines() if ln.strip().startswith("```")]
    assert len(fences) % 2 == 0, gist
    assert gist.splitlines()[-1].startswith("📎")


def test_gist_keeps_the_decision_taps_from_the_tail():
    from core.surfaces.spill import build_gist
    body = "Skill 'x' — update\n" + ("detail line\n" * 800) + \
        "/approve_p_62f952 · /reject_p_62f952"
    gist = build_gist(body, attachment_name="r.txt", limit=400)
    assert "/approve_p_62f952" in gist and "/reject_p_62f952" in gist


def test_owner_reply_spills_only_above_three_messages(tmp_path):
    from core.surfaces.spill import (OWNER_REPLY_GIST_CHARS, OWNER_REPLY_SPILL_CHARS,
                                     maybe_spill_owner_reply)
    short = "Head line\n" + "y" * 80 + "\n"
    under = short * (OWNER_REPLY_SPILL_CHARS // len(short) - 1)
    assert len(under) <= OWNER_REPLY_SPILL_CHARS
    assert maybe_spill_owner_reply(under, home_dir=str(tmp_path),
                                   source="pending_p_ab12") is None
    over = short * (OWNER_REPLY_SPILL_CHARS // len(short) + 5)
    gist, entry = maybe_spill_owner_reply(over, home_dir=str(tmp_path),
                                          source="pending_p_ab12")
    assert len(gist) <= OWNER_REPLY_GIST_CHARS + 200
    assert entry["path"].endswith(".txt")
    assert os.path.basename(entry["path"]).startswith("pending_p_ab12-")
    assert entry["caption"] == "Head line"
    with open(entry["path"], encoding="utf-8") as fh:
        assert fh.read() == over


def test_proactive_spill_is_txt_with_a_caption(tmp_path):
    from core.surfaces.spill import maybe_spill
    body = "**Goal done:** the report\n" + ("filler\n" * 400)
    _gist, entry = maybe_spill(body, home_dir=str(tmp_path), source="goal_done")
    assert entry["path"].endswith(".txt")
    assert entry["caption"] == "Goal done: the report"


def test_caption_cuts_on_a_word_with_a_marker():
    from core.surfaces.spill import caption_for
    cap = caption_for("word " * 100)
    assert cap.endswith(" …") and len(cap) <= 205


# ---------------------------------------------------------------------------
# W8 — /missed keeps the whole held text; marks a cut
# ---------------------------------------------------------------------------

def test_notice_text_is_whole_up_to_the_bound_and_marked_beyond():
    from core.surfaces.user_delivery import NOTICE_TEXT_MAX_CHARS, notice_text
    s = "z" * 5000
    assert notice_text(s) == s
    big = "q" * (NOTICE_TEXT_MAX_CHARS + 77)
    out = notice_text(big)
    assert "[cut: 77 more chars were not kept]" in out


def test_undelivered_reply_row_is_not_cut_at_1900(monkeypatch):
    from surfaces.telegram import harness
    rec = []
    monkeypatch.setattr("core.event_log.emit", lambda kind, **kw: rec.append(kw))
    harness._record_undelivered_reply("u", "/pending", "w" * 6000, RuntimeError("x"))
    assert rec and rec[0]["attrs"]["text"].count("w") == 6000


def test_unsent_text_rule():
    from core.surfaces.outbound_dispatcher import unsent_text
    assert unsent_text("abc", None) == "abc"
    assert unsent_text("abc", "  ") == ""
    assert unsent_text("abc def", "def") == "def"


# ---------------------------------------------------------------------------
# harness: _send_command_reply
# ---------------------------------------------------------------------------

class _Bot:
    def __init__(self, fail_marker=None):
        self.sent, self.docs = [], []
        self._fail = fail_marker

    async def send_message(self, chat_id, body, parse_mode=None, **extra):
        if self._fail and self._fail in body:
            raise RuntimeError("Bad Gateway")
        self.sent.append(body)
        return SimpleNamespace(message_id=len(self.sent))

    async def send_document(self, chat_id, file, caption=None):
        self.docs.append((file, caption))


def _harness(bot):
    from surfaces.telegram.harness import TelegramHarness
    h = TelegramHarness.__new__(TelegramHarness)
    h.bot = bot
    return h


def _result(cmd="/pending", text="/pending_p_ab12"):
    return SimpleNamespace(
        decision=SimpleNamespace(command=cmd, session_key="telegram:1"),
        inbound=SimpleNamespace(text=text, identity=SimpleNamespace(user_id="u1")))


def test_a_long_command_reply_arrives_as_gist_plus_txt(tmp_path, monkeypatch):
    monkeypatch.setattr("core.runtime_paths.prefs_home_dir", lambda: str(tmp_path))
    body = "Skill 'x' — review\n" + ("diff line " * 9 + "\n") * 200 + \
        "/approve_p_ab12cd · /reject_p_ab12cd"
    assert len(body) > 12000
    bot = _Bot()
    asyncio.run(_harness(bot)._send_command_reply({}, _result(), body, "1", None))
    assert len(bot.sent) == 1, "the gist is one message"
    assert "/approve_p_ab12cd" in bot.sent[0]
    assert len(bot.docs) == 1
    file, caption = bot.docs[0]
    path = getattr(file, "path", file)
    assert str(path).endswith(".txt") and caption == "Skill 'x' — review"


def test_a_short_command_reply_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr("core.runtime_paths.prefs_home_dir", lambda: str(tmp_path))
    bot = _Bot()
    asyncio.run(_harness(bot)._send_command_reply({}, _result(), "hello", "1", None))
    assert bot.sent == ["hello"] and bot.docs == []


def test_partial_reply_holds_only_the_rest_and_says_which_part(tmp_path, monkeypatch):
    monkeypatch.setattr("core.runtime_paths.prefs_home_dir", lambda: str(tmp_path))
    rec = []
    monkeypatch.setattr("core.event_log.emit", lambda kind, **kw: rec.append(kw))
    part_a = "\n".join(["AAAA " * 15] * 50)
    part_b = "\n".join(["BBBB " * 15] * 50)
    body = part_a + "\n" + part_b + "\nZZZZ end"
    assert 4096 < len(body) < 12000
    bot = _Bot(fail_marker="ZZZZ")
    asyncio.run(_harness(bot)._send_command_reply({}, _result(), body, "1", None))
    held = rec[0]["attrs"]["text"]
    assert "ZZZZ end" in held
    assert "AAAA" not in held, "a chunk that arrived must not be held again"
    assert "message 2 of 2" in held
    assert any("Message 2 of 2" in m and "/missed" in m for m in bot.sent)


# ---------------------------------------------------------------------------
# C1 — contacts
# ---------------------------------------------------------------------------

def _registry(tmp_path):
    from core.surfaces.correspondents import CorrespondentRegistry
    return CorrespondentRegistry(str(tmp_path / "correspondents.db"))


def test_pending_contact_row_carries_tappable_tokens_that_resolve(tmp_path):
    from core.surfaces.owner_admin import pending_correspondent_items
    from core.surfaces.tappable import parse_tappable
    from tools.controller.approval_queue import resolve_pending_target
    reg = _registry(tmp_path)
    reg.seed(surface="email", address="Bob@Example.com", session_id="s1",
             user_id="t1", require_approval=True)
    items = pending_correspondent_items(reg, "t1")
    preview = items[0]["preview"]
    assert "/approve email:" not in preview
    tok = [w for w in preview.split() if w.startswith("/approve_p_")][0].rstrip(")")
    verb, arg = parse_tappable(tok)
    assert verb == "/approve"
    assert resolve_pending_target(arg, items) is items[0]
    assert "  (approve:" in preview  # the inbox splits the fact off here


def test_new_pending_contact_pushes_a_decision_notice(tmp_path, monkeypatch):
    from core.surfaces import seed
    reg = _registry(tmp_path)
    monkeypatch.setattr("core.surfaces.config.SurfaceConfig.correspondent_access_enabled",
                        staticmethod(lambda: True))
    monkeypatch.setattr("core.surfaces.config.SurfaceConfig.correspondent_require_approval",
                        staticmethod(lambda: True))
    monkeypatch.setattr(seed, "effective_max_new_per_day", lambda *a: 10)
    pushed = []
    monkeypatch.setattr(seed, "_push_contact_notice",
                        lambda c, uid, sid, text: pushed.append((uid, text)))
    container = SimpleNamespace(get_service=lambda name: reg
                                if name == "correspondent_registry" else None)
    msg = "Hello Bob,\n" + "m" * 3500
    state = seed.maybe_seed_correspondent(
        container, surface="email", address="bob@example.com", session_id="s9",
        user_id="t1", first_message=msg)
    assert state == "pending"
    assert len(pushed) == 1
    uid, text = pushed[0]
    assert uid == "t1"
    assert "email bob@example.com" in text
    assert "session s9" in text and "owner-directed" in text
    assert "Hello Bob," in text and "chars: /more_" in text
    from core.surfaces.owner_admin import pending_correspondent_items
    from core.self_evolution import pending_tap_token
    item = pending_correspondent_items(reg, "t1")[0]
    assert pending_tap_token("approve", item) in text
    assert pending_tap_token("reject", item) in text
    # a re-seed of the same address is not new: no second push
    seed.maybe_seed_correspondent(container, surface="email", address="bob@example.com",
                                  session_id="s9", user_id="t1", first_message="again")
    assert len(pushed) == 1


def test_contact_notice_rides_a_critical_lane():
    from core.surfaces.seed import CONTACT_NOTICE_SOURCE
    from core.surfaces.user_delivery import _CRITICAL_SOURCES
    assert CONTACT_NOTICE_SOURCE in _CRITICAL_SOURCES


def test_push_is_fail_open_and_scheduled_on_a_running_loop(monkeypatch):
    from core.surfaces import seed
    calls = []

    async def _fake(container, uid, text, **kw):
        calls.append((uid, kw.get("source")))
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _fake)

    async def _run():
        seed._push_contact_notice(None, "t1", "s1", "hi")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert calls == [("t1", "correspondent_pending")]
