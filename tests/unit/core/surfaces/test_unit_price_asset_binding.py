"""A unit price carries the asset it was written for, or it is not a price.

⚠️ The trap this closes, found live 2026-09-15 while switching a room from PNL
to USDC. `chat.paid_ban_units = "75000"` was a bare number, and the reader
scaled it by whatever `chat.paid_asset` happened to be. Running

    /paid asset usdc-base

on a room priced at 75,000 PNL (~$5) would have quoted the next payer
**75,000 USDC**. Nothing warned, nothing refused; the number was simply
reinterpreted against a different token.

The fix is structural, not a guard bolted on top: the amount and its asset are
ONE value (``"75000 pnl"``), bound at WRITE time from the room's current asset
so the owner never types it, and verified at READ time. A mismatch REFUSES and
names both assets — it never falls back to the USD price either, because the
owner set a token price deliberately and silently charging a different figure is
the same class of lie.
"""
import pytest

from core.surfaces.room_actions import parse_unit_price


class _Asset:
    def __init__(self, asset_id, decimals=18, symbol="PNL"):
        self.asset_id, self.decimals, self.symbol = asset_id, decimals, symbol


PNL = _Asset("pnl", 18, "PNL")
USDC = _Asset("usdc-base", 6, "USDC")


# --- the binding -----------------------------------------------------------

def test_a_bound_price_resolves_against_its_own_asset():
    # Behaviour, not class identity: another test in this suite reloads the
    # module, so `isinstance` against the imported symbol is not reliable.
    p = parse_unit_price("75000 pnl", PNL)
    assert p.ok
    assert p.amount_raw == 75000 * 10 ** 18


def test_a_price_written_for_another_asset_REFUSES():
    """⚠️ The live trap. 75,000 PNL must never become 75,000 USDC."""
    p = parse_unit_price("75000 pnl", USDC)
    assert not p.ok
    assert "pnl" in p.reason.lower() and "usdc-base" in p.reason.lower()
    assert p.amount_raw is None


def test_a_mismatch_never_silently_falls_back_to_the_usd_price():
    """Refusing is the point: the owner set a token price on purpose, and
    quoting the dollar figure instead would charge a number nobody chose."""
    p = parse_unit_price("75000 pnl", USDC)
    assert not p.ok
    assert p.fall_back_to_usd is False


# --- ambiguity is refused, not guessed -------------------------------------

def test_a_BARE_number_is_ambiguous_and_refuses():
    """⚠️ A bare number is exactly what caused this. It can only reach the
    reader by a hand-edit, since every write path binds the asset."""
    p = parse_unit_price("75000", PNL)
    assert not p.ok
    assert "asset" in p.reason.lower()


def test_an_unparseable_value_refuses_and_says_so():
    for bad in ("abc pnl", "pnl", "75000 pnl extra", ""):
        p = parse_unit_price(bad, PNL)
        assert not p.ok, f"{bad!r} was accepted"


@pytest.mark.parametrize("bad", ["0 pnl", "-5 pnl"])
def test_a_non_positive_amount_refuses(bad):
    assert not parse_unit_price(bad, PNL).ok


# --- no price at all is not a refusal --------------------------------------

def test_no_unit_price_falls_back_to_the_usd_price():
    """An unset key means 'this room prices in dollars' — the legacy path."""
    p = parse_unit_price("", PNL)
    assert not p.ok
    assert p.fall_back_to_usd is True


def test_whitespace_only_is_the_same_as_unset():
    assert parse_unit_price("   ", PNL).fall_back_to_usd is True


# --- precision -------------------------------------------------------------

def test_a_fractional_amount_is_exact():
    p = parse_unit_price("0.5 pnl", PNL)
    assert p.ok and p.amount_raw == 5 * 10 ** 17


def test_the_asset_id_match_is_case_and_space_insensitive():
    assert parse_unit_price("  75000   PNL  ", PNL).ok


# --- the WRITE path binds it, so the owner never types an asset id ----------

def test_setting_a_bare_unit_price_binds_the_rooms_current_asset(tmp_path):
    """⚠️ This is what keeps a bare value out of the store. The owner types
    `/groups set here paid_ban_units 75000`; what is WRITTEN is '75000 pnl'."""
    from core.surfaces import chat_policy
    from core.surfaces.group_admin import set_key

    class _C:
        config = type("Cfg", (), {"data_dir": str(tmp_path)})()

        def get_service(self, n):
            return None

    home = str(tmp_path)
    ok, _ = chat_policy.set(home, "rob", "telegram", "-100", "chat.paid_asset", "pnl")
    assert ok
    out = set_key(_C(), "rob", "telegram", "-100", "paid_ban_units", "75000")
    assert "❌" not in out, out
    stored = chat_policy.load(home, "rob", "telegram", "-100").paid_ban_units
    assert stored == "75000 pnl", stored


def test_an_explicit_asset_is_respected_on_write(tmp_path):
    from core.surfaces import chat_policy
    from core.surfaces.group_admin import set_key

    class _C:
        config = type("Cfg", (), {"data_dir": str(tmp_path)})()

        def get_service(self, n):
            return None

    home = str(tmp_path)
    chat_policy.set(home, "rob", "telegram", "-100", "chat.paid_asset", "pnl")
    set_key(_C(), "rob", "telegram", "-100", "paid_ban_units", "75000 usdc-base")
    assert chat_policy.load(home, "rob", "telegram", "-100").paid_ban_units == "75000 usdc-base"
