"""043 D1 — ``/inbox`` and ``/book`` in the terminal, at eighty columns.

Both verbs are thin: the Inbox is composed by ``core.surfaces.inbox`` and the
book by ``tools.defi.book.read_book``; both are rendered by
``core.surfaces.inbox_render``, the same renderer Telegram and
``polyrob wallet book`` use. What is tested here is the part a terminal can get
wrong — the width, the three states, and the two sentences that may never be
printed: a confident "nothing needs you" over a list that refused, and a clean
book over a chain that did not answer.
"""
import time

import pytest

from core.surfaces.inbox import Item, compose
from core.surfaces.inbox_render import (
    CHAT_REMEDIES,
    REPL_REMEDIES,
    age,
    render_book,
    render_inbox,
)


class Ctx:
    """The slice of ``CommandContext`` these two handlers touch."""

    def __init__(self, args=None, user_id="u1"):
        self.args = list(args or [])
        self.user_id = user_id
        self.container = None
        self.out = []

    def emit(self, text, title=""):
        self.out.append((title, text))

    @property
    def text(self):
        return "\n".join(t for _title, t in self.out)


NOW = 1_700_000_000.0


def _body(**kw):
    items = kw.pop("items", [])
    sources = kw.pop("sources", {"self_evolution": "ok", "tool_approvals": "ok",
                                 "correspondents": "ok", "asks": "ok",
                                 "apps": "ok"})
    return compose(items, sources)


def _widths(text):
    return [len(line) for line in text.splitlines()]


# --- width ------------------------------------------------------------------ #

def test_every_line_fits_eighty_columns():
    body = _body(items=[
        Item(kind="app", id="price-watch",
             title="Put price-watch on the public internet, at an address a "
                   "stranger could type, which is a very long title indeed",
             body="I built it and its 18 tests pass. " * 6,
             meta="If you refuse, it stays built and stopped. " * 3,
             created_at=NOW - 7200),
        Item(kind="skill", id="4f2a", title="Keep the note about funding rates",
             body="Short.", created_at=NOW - 19 * 3600),
    ])
    out = render_inbox(body, now=NOW)
    assert max(_widths(out)) <= 80, max(_widths(out))


def test_a_narrow_width_is_honoured_for_a_phone():
    body = _body(items=[Item(kind="ask", id="a1", title="Give me a Base key",
                             body="The public node refuses my reads. " * 8,
                             created_at=NOW - 3600)])
    out = render_inbox(body, width=60, remedies=CHAT_REMEDIES, now=NOW)
    assert max(_widths(out)) <= 60


# --- the three states ------------------------------------------------------- #

def test_the_empty_state_says_why_it_can_say_nothing():
    out = render_inbox(_body(), now=NOW)
    assert "Nothing needs you." in out
    assert "every list answered" in out
    assert "Read my own proposals" in out
    assert "All of them answered." in out


def test_the_empty_state_names_every_source():
    from core.surfaces.inbox import SOURCE_LABELS
    out = render_inbox(_body(), now=NOW)
    for label in SOURCE_LABELS.values():
        assert label in out, label


def test_the_item_state_leads_with_the_count_and_lists_each_decision():
    body = _body(items=[
        Item(kind="ask", id="a1", title="Give me a Base key", created_at=NOW - 7200),
        Item(kind="app", id="price-watch", title="Publish price-watch",
             created_at=NOW - 3600),
    ])
    out = render_inbox(body, now=NOW)
    assert "2 things need you" in out
    assert "! Give me a Base key" in out
    assert "! Publish price-watch" in out
    assert "waiting 2h" in out and "waiting 1h" in out


def test_one_decision_is_singular():
    body = _body(items=[Item(kind="ask", id="a1", title="x", created_at=NOW - 60)])
    assert "One thing needs you" in render_inbox(body, now=NOW)


def test_the_partial_state_never_says_nothing_needs_you():
    """The sentence this whole surface exists to prevent."""
    body = compose([], {"apps": "ok", "tool_approvals": "unreadable(OSError: disk)"})
    out = render_inbox(body, now=NOW)
    assert "Nothing needs you" not in out
    assert "This list is incomplete" in out
    assert "spend approvals" in out
    assert "Could not read spend approvals." in out


def test_an_unreadable_source_is_an_entry_with_its_own_mark():
    from core.surfaces.inbox import unreadable_item
    body = compose([Item(kind="ask", id="a1", title="real", created_at=NOW - 60),
                    unreadable_item("tool_approvals")],
                   {"tool_approvals": "unreadable(locked)"})
    out = render_inbox(body, now=NOW)
    assert "? spend approvals" in out
    assert "is not the same as none" in out


# --- what counts ------------------------------------------------------------ #

def test_an_informational_row_is_listed_and_not_counted():
    body = _body(items=[Item(kind="ask", id="a1", title="decide me",
                             created_at=NOW - 60)],
                 sources={"asks": "ok"})
    body["not_blocking"] = compose(
        [Item(kind="invoice", id="1180", title="acme.dev is six days late",
              blocking=False, created_at=NOW - 6 * 86400)], {})["not_blocking"]
    out = render_inbox(body, now=NOW)
    assert "One thing needs you" in out
    assert "Not blocking, listed and not counted:" in out
    assert "acme.dev is six days late" in out


def test_an_informational_row_is_not_offered_a_yes_or_a_no():
    body = compose([Item(kind="invoice", id="1180", title="late", blocking=False,
                         created_at=NOW - 86400)], {"apps": "ok"})
    out = render_inbox(body, now=NOW)
    assert "/pending approve" not in out
    assert "/approve" not in out


# --- a cut list says it was cut --------------------------------------------- #

def test_a_truncated_list_says_how_many_it_left_out():
    """A silent cut is the same failure shape as a silently dropped source: the
    reader believes they have seen everything, and the count above them stops
    matching what is on screen."""
    body = _body(items=[
        Item(kind="ask", id=str(i), title=f"ask {i}", created_at=NOW - 60 * i)
        for i in range(1, 6)])
    out = render_inbox(body, limit=2, now=NOW)
    assert out.count("! ask") == 2
    assert "3 more are waiting here and not shown" in out
    assert "5 things need you" in out


def test_one_hidden_row_is_singular():
    body = _body(items=[
        Item(kind="ask", id=str(i), title=f"ask {i}", created_at=NOW - 60 * i)
        for i in range(1, 4)])
    assert "one more is waiting here" in render_inbox(body, limit=2, now=NOW)


def test_the_truncation_sentence_does_not_truncate_itself():
    """⚠️ ``_more_line`` returned only the FIRST wrapped line, so at a phone's
    60 columns the sentence announcing a truncation was itself cut in half."""
    body = _body(items=[
        Item(kind="ask", id=str(i), title=f"ask {i}", created_at=NOW - 60 * i)
        for i in range(1, 8)])
    out = render_inbox(body, limit=1, width=60, remedies=CHAT_REMEDIES, now=NOW)
    # The sentence is COMPLETE, however it wraps — which is the whole point.
    flat = " ".join(out.split())
    assert "6 more are waiting here and not shown. Ask for the whole list." in flat
    assert max(_widths(out)) <= 60


def test_an_untruncated_list_says_nothing_about_more():
    body = _body(items=[Item(kind="ask", id="a", title="only one",
                             created_at=NOW - 60)])
    assert "not shown" not in render_inbox(body, limit=5, now=NOW)


# --- the remedy is the SEAT's own verb -------------------------------------- #

def test_the_repl_remedy_is_the_repls_own_decider():
    body = _body(items=[Item(kind="skill", id="4f2a", title="a note",
                             created_at=NOW - 60)])
    out = render_inbox(body, remedies=REPL_REMEDIES, now=NOW)
    assert "/pending approve skill 4f2a" in out
    assert "/pending reject skill 4f2a" in out


def test_the_chat_remedy_is_telegrams_own_decider():
    """One tappable token, not a verb plus an id the owner has to copy.

    A chat client auto-links the VERB and not its argument, so `/approve 4f2a`
    made the one tappable thing on the page the half that does nothing.
    """
    from core.self_evolution import pending_tap_token
    from surfaces.telegram.harness import normalize_tappable_command

    item = {"kind": "skill", "id": "4f2a"}
    body = _body(items=[Item(kind="skill", id="4f2a", title="a note",
                             created_at=NOW - 60)])
    out = render_inbox(body, remedies=CHAT_REMEDIES, width=60, now=NOW)

    approve = pending_tap_token("approve", item)
    assert approve in out and pending_tap_token("reject", item) in out
    assert "/approve 4f2a" not in out
    assert "/pending approve" not in out
    # What is rendered is what the router parses back.
    assert normalize_tappable_command(approve)[0] == "/approve"


def test_an_app_and_an_ask_carry_their_own_verbs_on_both_seats():
    body = _body(items=[
        Item(kind="app", id="price-watch", title="app", created_at=NOW - 60),
        Item(kind="ask", id="g7", title="ask", created_at=NOW - 60),
    ])
    for table in (REPL_REMEDIES, CHAT_REMEDIES):
        out = render_inbox(body, remedies=table, now=NOW)
        assert "/apps approve price-watch" in out
        assert "/fulfill g7" in out


# --- age --------------------------------------------------------------------- #

@pytest.mark.parametrize("delta,expected", [
    (0, ""), (None, ""), (61, "1m"), (59 * 60, "59m"),
    (3600, "1h"), (19 * 3600, "19h"), (6 * 86400, "6d"),
])
def test_age_reads_like_a_person_wrote_it(delta, expected):
    if delta in (0, None):
        assert age(delta, NOW) == expected
    else:
        assert age(NOW - delta, NOW) == expected


def test_an_unstamped_record_has_no_invented_age():
    body = _body(items=[Item(kind="skill", id="x", title="no stamp")])
    assert "waiting" not in render_inbox(body, now=NOW)


# --- the REPL handlers ------------------------------------------------------- #

def test_inbox_handler_renders_the_composed_body(monkeypatch):
    import cli.ui.commands.h_inbox as mod
    monkeypatch.setattr(mod, "build", lambda uid, home: _body(
        items=[Item(kind="ask", id="a1", title="Give me a Base key",
                    created_at=NOW - 3600)]))
    ctx = Ctx()
    mod.h_inbox(ctx)
    assert "Give me a Base key" in ctx.text
    assert max(_widths(ctx.text)) <= 80


def test_inbox_handler_takes_a_limit(monkeypatch):
    import cli.ui.commands.h_inbox as mod
    monkeypatch.setattr(mod, "build", lambda uid, home: _body(items=[
        Item(kind="ask", id=str(i), title=f"ask {i}", created_at=NOW - 60 * i)
        for i in range(1, 6)]))
    ctx = Ctx(args=["2"])
    mod.h_inbox(ctx)
    assert ctx.text.count("! ask") == 2


def test_inbox_handler_reports_a_composer_failure_as_unknown(monkeypatch):
    import cli.ui.commands.h_inbox as mod

    def boom(uid, home):
        raise RuntimeError("data home is gone")

    monkeypatch.setattr(mod, "build", boom)
    ctx = Ctx()
    mod.h_inbox(ctx)
    assert "UNKNOWN" in ctx.text
    assert "Nothing needs you" not in ctx.text


# --- the book ---------------------------------------------------------------- #

def test_a_clean_book_says_so_and_names_the_chains():
    body = {"verdict": "clean", "checked_at": NOW - 240,
            "chains": {"base": {"verdict": "clean", "report": {}},
                       "solana": {"verdict": "clean", "report": {}}}}
    out = render_book(body, now=NOW)
    assert "The ledger and the chains agree." in out
    assert "base" in out and "solana" in out
    assert "Checked 4m ago" in out
    assert max(_widths(out)) <= 80


def test_a_disagreement_names_the_rows_and_refuses_to_trade():
    body = {"verdict": "disagreement", "checked_at": NOW,
            "chains": {"base": {"verdict": "disagreement", "report": {
                "unbacked": ["BOTS 850,000 — written down, the chain holds 0"],
                "unexplained": ["BASECAT 2,100,000 — held, not written down"]}}}}
    out = render_book(body, now=NOW)
    assert "DISAGREE" in out
    assert "BOTS" in out and "BASECAT" in out
    assert "will not trade" in out
    assert max(_widths(out)) <= 80


def test_an_unreadable_chain_is_never_clean():
    body = {"verdict": "unverified", "checked_at": NOW,
            "chains": {"base": {"verdict": "unverified", "report": {},
                                "error": "the node did not answer"}}}
    out = render_book(body, now=NOW)
    assert "could not be verified" in out
    assert "the node did not answer" in out
    assert "agree" not in out


def test_a_book_with_no_sight_at_all_says_why():
    body = {"chains": {}, "verdict": None, "checked_at": NOW,
            "error": "on-chain sight is off — the book cannot be read"}
    assert "on-chain sight is off" in render_book(body, now=NOW)


def test_book_handler_reports_a_read_failure_as_unknown(monkeypatch):
    import asyncio

    import cli.ui.commands.h_inbox as mod
    import tools.defi.book as book_mod

    async def boom(user_id, data_dir, **kw):
        raise RuntimeError("no rpc")

    monkeypatch.setattr(book_mod, "read_book", boom)
    ctx = Ctx()
    asyncio.run(mod.h_book(ctx))
    assert "UNKNOWN" in ctx.text


def test_book_handler_is_awaitable_so_the_repl_loop_is_not_blocked():
    """``read_book`` reads every money chain over the network; the registry
    awaits an awaitable handler, so bridging it back to sync would freeze the
    REPL for the duration of those reads."""
    import inspect

    import cli.ui.commands.h_inbox as mod
    assert inspect.iscoroutinefunction(mod.h_book)
    assert not inspect.iscoroutinefunction(mod.h_inbox)


def test_book_handler_renders_a_clean_book(monkeypatch):
    import asyncio

    import cli.ui.commands.h_inbox as mod
    import tools.defi.book as book_mod

    async def clean(user_id, data_dir, **kw):
        return {"verdict": "clean", "checked_at": NOW,
                "chains": {"base": {"verdict": "clean", "report": {}}}}

    monkeypatch.setattr(book_mod, "read_book", clean)
    ctx = Ctx()
    asyncio.run(mod.h_book(ctx))
    assert "The ledger and the chains agree." in ctx.text


# --- registration ------------------------------------------------------------ #

def test_both_verbs_are_registered_in_their_groups():
    from cli.ui.commands.handlers import build_default_registry
    reg = build_default_registry()
    assert reg.lookup("inbox").group == "needs you"
    assert reg.lookup("book").group == "money"


def test_both_verbs_carry_a_long_help_and_an_elsewhere():
    from cli.ui.commands.handlers import build_default_registry
    reg = build_default_registry()
    for name in ("inbox", "book"):
        cmd = reg.lookup(name)
        assert cmd.help_long.strip(), name
        assert cmd.elsewhere.strip(), name


def test_grouped_help_lists_both():
    from cli.ui.commands.h_help import h_help
    from cli.ui.commands.handlers import build_default_registry
    ctx = Ctx()
    ctx.registry = build_default_registry()
    ctx.raw = "help"
    h_help(ctx)
    assert "/inbox" in ctx.text and "/book" in ctx.text
