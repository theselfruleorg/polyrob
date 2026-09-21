"""A remedy an owner cannot type on the seat he is reading is not a remedy.

`core/surfaces/room_action_admin.py` is the ONE helper set behind Telegram
`/paid`, `polyrob owner paid …`, the REPL and a console panel — and every
sentence it returned named a SLASH verb with `here`, a grammar that exists only
in a chat. `polyrob owner paid show <chat>` printed "set a price (/paid price
mute 0.50)" and "grant with `/groups set here …`" at a shell prompt.

Same defect and same fix as `core/surfaces/inbox_render.py`'s remedy tables
(C21). These pin that the CHAT form stays the default — Telegram must be
byte-identical — and that the CLI seat actually passes its own.
"""
import inspect

import pytest

from core.surfaces import room_action_admin as adm


class _Policy:
    paid_enabled = False
    paid_asset = ""
    member_verbs = ()
    paid_max_per_payer_day = 1
    paid_max_per_target_day = 1
    paid_offer_ttl = "1h"


@pytest.fixture
def _no_store(monkeypatch):
    monkeypatch.setattr(adm, "_policy", lambda *a, **k: _Policy())
    monkeypatch.setattr(adm, "_store", lambda c: type(
        "S", (), {"open_offers": lambda *a, **k: []})())


# --- the tables themselves --------------------------------------------------

def test_both_tables_answer_every_remedy():
    """A seat with a missing key silently renders the other seat's grammar."""
    assert set(adm.CLI_REMEDIES) == set(adm.CHAT_REMEDIES)
    assert adm.CHAT_REMEDIES, "an empty table proves nothing"


def test_the_chat_table_is_slash_verbs_and_the_cli_table_is_not():
    for key, spec in adm.CHAT_REMEDIES.items():
        assert spec.startswith("/"), f"{key}: {spec!r} is not a chat verb"
    for key, spec in adm.CLI_REMEDIES.items():
        assert spec.startswith("polyrob "), f"{key}: {spec!r} is not a CLI verb"
        assert "here" not in spec.split(), f"{key}: `here` has no meaning at a shell"


def test_a_missing_key_falls_back_to_the_chat_form_and_never_raises():
    assert adm._remedy({}, "price") == adm.CHAT_REMEDIES["price"]
    assert adm._remedy(None, "no-such-remedy") == ""
    # A field the spec needs but the caller did not pass must not raise into a
    # reply — a sentence without its remedy is poor, one that 500s is worse.
    assert adm._remedy(adm.CLI_REMEDIES, "member_verbs") is not None


# --- the default is the chat form ------------------------------------------

def test_status_without_a_table_is_the_chat_form(_no_store):
    out = adm.status(None, "telegram", "-100")
    assert "/paid price mute 0.50" in out
    assert "/paid enable" in out
    assert "polyrob" not in out


def test_the_chat_sentences_are_byte_identical_to_the_pre_split_wording(_no_store):
    """⚠️ Telegram must not move a character. These five strings are the
    pre-2026-09-21 literals, copied verbatim from the commit that had them
    inline — if a refactor changed one, the owner's chat reply changed and the
    only thing that was supposed to change was the CLI's."""
    assert adm.status(None, "telegram", "-100") == (
        "Paid actions are not enabled in telegram:-100. Set a price "
        "(/paid price mute 0.50), an asset (/paid asset rob), then "
        "/paid enable.")
    assert adm._max_dur_hint("mute") == (
        " Longest mute: /groups set here paid_mute_max_duration <e.g. 24h>.")
    assert adm._remedy(None, "member_verbs", verbs="mute,ban") == (
        "/groups set here member_verbs mute,ban")
    assert adm._remedy(None, "disable") == "/paid disable"
    assert adm._remedy(None, "price") == "/paid price mute 0.50"


def test_status_with_the_cli_table_names_commands_a_shell_can_run(_no_store):
    out = adm.status(None, "telegram", "-100", remedies=adm.CLI_REMEDIES)
    assert "polyrob owner paid price -100 mute 0.50" in out
    assert "polyrob owner paid enable -100" in out
    assert "/paid" not in out


def test_the_member_grant_remedy_names_the_room_on_the_cli():
    """`here` cannot name a room at a shell prompt — the chat id must be in it."""
    chat = adm._remedy(adm.CHAT_REMEDIES, "member_verbs", surface="telegram",
                       chat_id="-100", verbs="mute,ban")
    cli = adm._remedy(adm.CLI_REMEDIES, "member_verbs", surface="telegram",
                      chat_id="-100", verbs="mute,ban")
    assert chat == "/groups set here member_verbs mute,ban"
    assert cli == "polyrob owner groups set telegram -100 member_verbs mute,ban"


def test_the_duration_hint_follows_the_seat():
    assert adm._max_dur_hint("mute").strip().startswith("Longest mute: /groups")
    cli = adm._max_dur_hint("mute", "telegram", "-100", adm.CLI_REMEDIES)
    assert "polyrob owner groups set telegram -100 paid_mute_max_duration" in cli
    # A verb that takes no duration still gets no hint at all.
    assert adm._max_dur_hint("nonsense") == ""


# --- every seat-facing helper takes the parameter ---------------------------

@pytest.mark.parametrize("fn", ["status", "set_price", "enable"])
def test_every_helper_that_prints_a_remedy_accepts_a_table(fn):
    assert "remedies" in inspect.signature(getattr(adm, fn)).parameters


def test_the_cli_seat_passes_its_own_table():
    """The table exists and the CLI does not use it = the defect, unchanged."""
    src = inspect.getsource(__import__("cli.commands.owner",
                                       fromlist=["owner"]))
    calls = [line for line in src.splitlines() if "adm.CLI_REMEDIES" in line]
    assert len(calls) >= 3, calls
