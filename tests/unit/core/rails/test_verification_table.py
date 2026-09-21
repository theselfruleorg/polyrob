"""057 WS-E — the ONE per-rail verification table and its three renderings."""
import inspect
import pathlib

import pytest

from core.rails import verification as v

REPO = pathlib.Path(__file__).resolve().parents[4]

REQUIRED = {"x_post", "x_reply", "telegram_channel", "telegram_group",
            "email", "onchain"}


def test_every_rail_has_a_row():
    assert REQUIRED <= set(v.VERIFICATION)


def test_every_row_is_complete_and_keyed_by_its_own_rail():
    for key, row in v.VERIFICATION.items():
        assert row.rail == key
        for field in ("title", "receipt", "proof", "method", "note"):
            assert getattr(row, field).strip(), f"{key}.{field} is empty"


def test_the_channel_row_says_the_receipt_is_the_proof():
    """The 09-19 prod failure: a delivered channel post reported as unconfirmed
    because the agent could not find it. That is what this row prevents."""
    line = v.verification_line("telegram_channel", message_id=123)
    assert line.startswith("proof: telegram receipt message_id=123 — ")
    assert "cannot read its own channel posts" in line


def test_an_onchain_receipt_is_not_a_result():
    assert "read-back" in v.VERIFICATION["onchain"].note
    assert "read" in v.VERIFICATION["onchain"].method


def test_facts_are_optional_and_blanks_are_dropped():
    bare = "proof: smtp 250 + Message-ID — "
    assert v.verification_line("email").startswith(bare)
    assert v.verification_line("email", message_id=None).startswith(bare)
    assert v.verification_line("email", message_id="").startswith(bare)
    assert "message_id=<x@y>" in v.verification_line("email", message_id="<x@y>")


def test_unknown_rail_renders_nothing_rather_than_inventing_a_rule():
    assert v.verification_line("carrier_pigeon") == ""
    assert v.verification_line("") == ""
    assert v.verification_line(None) == ""


@pytest.mark.parametrize("surface,is_room,expected", [
    ("telegram", False, "telegram_channel"),
    ("telegram", True, "telegram_group"),
    ("email", False, "email"),
    ("whatsapp", False, None),
])
def test_rail_for_message(surface, is_room, expected):
    assert v.rail_for_message(surface, is_room=is_room) == expected


def test_ambiguous_telegram_defaults_to_the_cautious_row():
    """Assuming a read-back EXISTS is the failure; assuming it does not is
    merely cautious, so an unclassified telegram send gets the channel row."""
    assert v.rail_for_message("telegram") == "telegram_channel"
    assert v.rail_for_message("telegram", chat_type="supergroup") == "telegram_group"


# --- the generated guide -----------------------------------------------------

GUIDE = REPO / "docs" / "guide" / "rails-verification.md"


def test_guide_matches_the_table():
    """The doc is GENERATED from the table; a hand edit (or a table change with
    no regen) fails here. Regenerate with the command in the doc's own header."""
    assert GUIDE.is_file(), f"{GUIDE} is missing"
    assert GUIDE.read_text(encoding="utf-8") == v.render_guide()


def test_guide_names_every_rail():
    text = GUIDE.read_text(encoding="utf-8")
    for key, row in v.VERIFICATION.items():
        assert f"`{key}`" in text
        assert row.proof in text
        assert row.note in text


def test_guide_has_a_portal_nav_entry():
    """scripts/deploy_portal.sh fails closed on a shipped guide with no
    docs.html sidebar entry — it would be unreachable on the site."""
    nav = REPO / "web" / "portal" / "docs.html"
    # `web/portal/docs.html` is owner-infra and does not ship in the public package, where a
    # bare `pytest` still collects this file. An absent file is 'not this
    # tree's concern', never a failure.
    if not nav.exists():
        pytest.skip("portal sources absent (public package)")
    assert "guide/rails-verification.md" in nav.read_text(encoding="utf-8")


# --- the call sites ----------------------------------------------------------

def test_every_call_site_uses_a_key_that_exists():
    """`verification_line` returns "" for an unknown rail, so a typo would be a
    SILENT drop. Pin the literals each producer passes."""
    from tools.controller import message_send, room_read_action
    from tools.x_browser import tool as x_tool
    from tools import email_tool
    sources = {
        "message_send": inspect.getsource(message_send),
        "room_read_action": inspect.getsource(room_read_action),
        "x_browser": inspect.getsource(x_tool),
        "email_tool": inspect.getsource(email_tool),
    }
    assert "verification_line" in sources["message_send"]
    assert "rail_for_message" in sources["message_send"]
    for literal, where in (("'x_post'", "x_browser"), ("'x_reply'", "x_browser"),
                           ('"telegram_channel"', "room_read_action")):
        assert literal in sources[where], f"{literal} missing from {where}"
        assert literal.strip("'\"") in v.VERIFICATION
