"""096 P2 W5: every cut is a tap — ``/more``, the read verbs, no silent cut.

- ``/more_<token>`` round-trips through the ONE tap grammar and the store
  (per tenant, 24 h TTL);
- each read token (``/ask_<id>``, ``/card_<id>``, ``/skill_<id>``) parses to
  a real verb in ``core.verbs`` and never shadows a decision token;
- the capped lists end in a ``/more`` tap, not "see the console";
- ``/asks``, ``/asks <id>``, ``/cards <id>``, ``/skills <id>``, ``/rules``,
  ``/inbox all`` answer in full;
- D8: "read it" is a button; buttons go to the first three items;
- ratchet: the owner renderers touched here carry no bare ``[:N]`` slice on
  owner text without a marker.
"""
import re
import types
from pathlib import Path

import pytest

from core.surfaces import more_store
from core.surfaces.tappable import (is_tappable_token, parse_tappable,
                                    read_token)

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def _fresh_store():
    more_store._reset_for_tests()
    yield
    more_store._reset_for_tests()


# --- the store and the grammar ---------------------------------------------

def test_more_round_trip_through_the_tap_grammar():
    line = more_store.cut_line("u1", 3, "a\nb\nc", prefix="")
    token = re.search(r"/more_[0-9a-f]+", line).group(0)
    assert line == f"(+3 more: {token})"
    verb, arg = parse_tappable(token)
    assert verb == "/more"
    assert more_store.more_reply("u1", [arg]) == "a\nb\nc"


def test_more_is_per_tenant_and_expires():
    tok = more_store.stash("u1", "secret", now=1000.0)
    assert more_store.fetch("u2", tok, now=1001.0) is None
    assert more_store.fetch("u1", tok, now=1001.0) == "secret"
    assert more_store.fetch("u1", tok, now=1000.0 + more_store.TTL_S + 1) is None
    assert more_store.more_reply("u1", ["deadbeef"]) == more_store.EXPIRED


def test_the_store_is_bounded_per_tenant():
    toks = [more_store.stash("u1", str(i)) for i in range(more_store.MAX_PER_TENANT + 5)]
    assert more_store.fetch("u1", toks[0]) is None
    assert more_store.fetch("u1", toks[-1]) == str(more_store.MAX_PER_TENANT + 4)


def test_clip_always_marks_a_cut():
    assert more_store.clip("abc", 5) == "abc"
    assert more_store.clip("abcdefgh", 5) == "abcd…"
    assert len(more_store.clip("x" * 100, 10)) == 10


def test_cut_text_keeps_the_whole_text_one_tap_away():
    out = more_store.cut_text("u1", "x" * 50, 20)
    head, line = out.split("\n")
    assert head == "x" * 20 and "+30 chars: /more_" in line
    token = re.search(r"/more_([0-9a-f]+)", line).group(1)
    assert more_store.fetch("u1", token) == "x" * 50


@pytest.mark.parametrize("token,want", [
    ("/ask_a1b2c3d4e5f6", ("/asks", "a1b2c3d4e5f6")),
    ("/card_0123456789", ("/cards", "0123456789")),
    ("/skill_promo_6551p", ("/skills", "promo_6551p")),
    ("/more_ab12cd34", ("/more", "ab12cd34")),
    ("/pending_p_62f952", ("/pending", "p-62f952")),
])
def test_read_tokens_parse_to_their_verb(token, want):
    assert parse_tappable(token) == want
    assert is_tappable_token(token)


def test_a_card_press_is_not_a_card_read():
    from core.surfaces.cards import parse_card_token
    assert parse_tappable("/card_0123456789_ok") == (None, None)
    assert parse_card_token("/card_0123456789_ok") == ("0123456789", "ok")


def test_every_read_verb_is_a_core_verb():
    from core.surfaces.dispatcher import command_names
    from core.surfaces.tappable import READ_TOKENS
    from core.verbs import verb_for
    for _prefix, (verb, _shape) in READ_TOKENS.items():
        assert verb_for(verb) is not None and verb in command_names()
    for verb in ("/more", "/rules"):
        assert verb_for(verb) is not None and verb in command_names()


def test_read_token_folds_a_skill_id_and_refuses_a_bad_shape():
    assert read_token("/skill", "promo-6551p") == "/skill_promo_6551p"
    assert read_token("/ask", "not hex!") == ""
    assert read_token("/nope", "x") == ""


# --- the capped lists -------------------------------------------------------

def test_recap_cut_is_a_more_tap_on_a_chat_seat():
    from core.recap import RecapEntry, format_recap_markdown
    entries = [RecapEntry(ts=float(i), kind="event", text=f"e{i}") for i in range(20)]
    out = format_recap_markdown(entries, "24h", limit=15, more_tenant="u1")
    assert "console" not in out
    token = re.search(r"/more_([0-9a-f]+)", out).group(1)
    rest = more_store.fetch("u1", token)
    assert rest.count("\n") == 4 and "e19" in rest
    # A terminal (no tenant) keeps a plain count.
    assert "/more_" not in format_recap_markdown(entries, "24h", limit=15)


def _inbox_body(n):
    from core.surfaces.inbox import SOURCE_LABELS, Item, compose
    items = [Item(kind="ask", id=f"a{i}", title=f"ask {i}", created_at=1.0)
             for i in range(n)]
    return compose(items, {name: "ok" for name in SOURCE_LABELS})


def test_inbox_cut_is_a_more_tap_and_inbox_all_lists_everything(monkeypatch):
    from surfaces.telegram import owner_ops
    import surfaces.inbox_sources as src
    monkeypatch.setattr(src, "build_inbox", lambda uid, data_dir=None: _inbox_body(8))
    out = owner_ops.inbox_reply("u1", "/tmp")
    assert "Ask for the whole list" not in out
    token = re.search(r"/more_([0-9a-f]+)", out).group(1)
    assert "ask 7" in more_store.fetch("u1", token)
    assert "ask 7" not in out
    every = owner_ops.inbox_reply("u1", "/tmp", ["all"])
    assert "ask 7" in every and "/more_" not in every
    assert "ask 2" in owner_ops.inbox_reply("u1", "/tmp", ["3"])
    assert "Usage" in owner_ops.inbox_reply("u1", "/tmp", ["junk"])


def test_goal_show_marks_the_cut_body():
    from surfaces.telegram.owner_ops import _goal_show
    goal = types.SimpleNamespace(id="g" * 12, status="ready", title="t",
                                 body="b" * 2000, priority=1, consecutive_failures=0,
                                 max_retries=3, result="", payload={}, session_id=None)
    import core.goal_owner_verbs as gov
    orig = gov.reason
    gov.reason = lambda board, goal, uid: "why"
    try:
        out = _goal_show("u1", None, goal)
    finally:
        gov.reason = orig
    assert "/more_" in out and "+800 chars" in out


# --- the read verbs ---------------------------------------------------------

def _ask(aid, title, body, options=None):
    return types.SimpleNamespace(id=aid, title=title, body=body, user_id="u1",
                                 payload={"options": options or {}})


class _Board:
    def __init__(self, asks):
        self._asks = asks

    def asks(self, user_id=None, status=None):
        return list(self._asks)


def _call(board, args, text="/asks"):
    return types.SimpleNamespace(board=board, user_id="u1", data_dir="/tmp",
                                 instance_id="agent1", args=args, cmd="/asks",
                                 result=types.SimpleNamespace(
                                     inbound=types.SimpleNamespace(text=text)))


@pytest.mark.asyncio
async def test_asks_list_and_one_ask_in_full(monkeypatch):
    from surfaces.telegram import approval_ops
    monkeypatch.setattr(approval_ops, "_elsewhere", lambda c, v: "")
    long_why = "w" * 900
    board = _Board([_ask("a1b2c3d4e5f6", "Pick a chain", long_why,
                         {"A": "base", "B": "arbitrum"})])
    out = await approval_ops.asks_verb(_call(board, []))
    assert "/ask_a1b2c3d4e5f6" in out
    assert "/fulfill_a1b2c3d4e5f6_a" in out and "/fulfill_a1b2c3d4e5f6_b" in out
    assert "w" * 600 in out and "w" * 601 not in out and "+300 chars" in out
    one = await approval_ops.asks_verb(_call(board, ["a1b2c3d4e5f6"]))
    assert long_why in one
    assert "A) base — /fulfill_a1b2c3d4e5f6_a" in one
    assert "No open ask" in await approval_ops.asks_verb(_call(board, ["ffffff"]))


def test_card_reopen_shows_the_whole_body(tmp_path):
    from core.surfaces import cards
    st = cards.CardStore(str(tmp_path / "cards.db"))
    card = cards._new("u1", cards.KIND_CHOICE, cards.ORIGIN_AGENT, "Q " * 150,
                      body="x" * 5000, options=["a", "b"], ttl=600, st=st)
    short = cards.render_text(card)
    assert "+2000 chars" in short and f"/card_{card.card_id}" in short
    assert "x" * 3001 not in short
    full = cards.read_reply("u1", card.card_id, st=st)
    assert "x" * 5000 in full and "cut short" not in full
    assert "No card" in cards.read_reply("u2", card.card_id, st=st)


def test_skills_list_carries_read_taps_and_an_id_opens_the_body(monkeypatch):
    from surfaces.telegram import skills_ops
    monkeypatch.setattr(skills_ops, "catalog",
                        lambda uid, q="", desc_chars=80: [("promo-6551p", "posts")]
                        if (not q or q in "promo-6551p") else [])
    monkeypatch.setattr(skills_ops, "skill_body_reply",
                        lambda uid, sid: f"BODY {sid}")
    assert "/skill_promo_6551p" in skills_ops.skills_reply("u1", [])
    assert skills_ops.skills_reply("u1", ["promo_6551p"]) == "BODY promo-6551p"
    assert skills_ops.skills_reply("u1", ["promo-6551p"]) == "BODY promo-6551p"


def test_rules_numbers_owner_rules_and_contract(tmp_path):
    from core.instance import self_tier_root
    from surfaces.telegram.owner_ops import rules_reply
    root = self_tier_root(tmp_path, "u1", "agent1")
    root.mkdir(parents=True)
    (root / "owner.md").write_text("# Posting\n- never post at night\n- ask first\n"
                                   "## Superseded\n- old rule\n")
    (root / "contract.md").write_text("- report daily\n")
    out = rules_reply("u1", str(tmp_path), "agent1")
    assert "1. never post at night" in out and "2. ask first" in out
    assert "old rule" not in out
    assert "1. report daily" in out
    assert "(none written)" in rules_reply("u2", str(tmp_path), "agent1")


# --- D8 buttons -------------------------------------------------------------

def test_read_it_is_a_button_and_only_three_items_get_buttons():
    from core.surfaces.actions import actions_from_text
    text = "\n".join(f"/approve_p_a{i}   /reject_p_a{i}   read it: /pending_p_a{i}"
                     for i in range(5)) + "\nAll: /approve_all"
    got = [a.command for a in actions_from_text(text)]
    assert got[:3] == ["/approve_p_a0", "/reject_p_a0", "/pending_p_a0"]
    assert len(got) == 9 and "/approve_p_a3" not in got
    assert "/approve_all" not in got


# --- ratchet: no silent cut in the owner renderers touched here -------------

#: Owner renderers 096 W5 swept. A bare ``[:N]`` slice on text the owner reads
#: must carry a marker (``…``, a ``/more`` tap, ``clip``/``cut_text``). A slice
#: that is NOT owner text (a log line) or that a count follows says so with a
#: ``# cut: <reason>`` comment on its line.
_SWEPT = ("surfaces/telegram/owner_ops.py", "surfaces/telegram/approval_ops.py",
          "surfaces/telegram/card_ops.py", "surfaces/telegram/skills_ops.py",
          "core/surfaces/inbox_render.py", "core/status_work.py",
          "core/surfaces/more_store.py")
#: Slices that are NOT owner prose: ids, hashes, internal bounds, or a cut
#: whose marker sits on the same line.
_ALLOWED = re.compile(
    r"(id|_id|\bsid|hash|tx|\[\"request_id\"\]|token|addr|\bg\b|key|head|"
    r"limit|keep|LIMIT|_CHARS\]|\.\.\.|…|_code\(|n - len\(marker\)|str\(e\)|"
    r"str\(exc\)|rendered\[)", re.IGNORECASE)
_SLICE = re.compile(r"\[:\s*\d+\s*\]|\[:\s*[A-Za-z_]+\s*\]")


def test_no_silent_slice_in_the_swept_owner_renderers():
    offenders = []
    for rel in _SWEPT:
        for i, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if not _SLICE.search(code) or "# cut: " in line:
                continue
            if _ALLOWED.search(code):
                continue
            offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "a [:N] slice on owner text without a marker:\n" + "\n".join(offenders)
