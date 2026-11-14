"""117 W2: the v4 pool seed declaration — its rules, its one-way states and
its card. Every rule has its refusing twin; unreadable is never empty."""
import pytest

from core.surfaces import cards
from core.wallet import liquidity_guard as G, lp_seed as S, univ3_math as M

ZERO = "0x" + "0" * 40
TKN = "0x" + "55" * 20
SQRT = M.sqrt_price_at_tick(156800)
BASE = {"chain": "robinhood", "currency0": ZERO, "currency1": TKN, "fee": 10000,
        "tick_spacing": 200, "hooks": ZERO, "tick_lower": -160000, "tick_upper": 380000,
        "amount0_raw": 2 * 10 ** 18, "amount1_raw": 12_750_000 * 10 ** 18, "dec0": 18,
        "dec1": 18, "sym0": "ETH", "sym1": "TKN", "sqrt_price_x96": SQRT,
        "price_source": "owner", "gap_bps": 100}


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "lp_seeds.db")


def test_a_valid_declaration_is_stored_with_its_pool_id():
    f = S.validate(BASE)
    assert f["pool_id"] == G.v4_pool_id((ZERO, TKN, 10000, 200, ZERO))
    assert f["amount0_raw"] == str(2 * 10 ** 18) and f["sqrt_price_x96"] == str(SQRT)
    assert S.key_of(f) == {"currency0": ZERO, "currency1": TKN, "fee": 10000,
                           "tickSpacing": 200, "hooks": ZERO}


@pytest.mark.parametrize("change,why", [
    ({"currency0": TKN, "currency1": ZERO}, "sorted"),
    ({"currency1": ZERO}, "sorted"),
    ({"fee": G.V4_DYNAMIC_FEE_FLAG}, "dynamic fee"),
    ({"fee": 2_000_000}, "static fee"),
    ({"tick_spacing": 0}, "tick_spacing"),
    ({"tick_lower": -160001}, "multiples of 200"),
    ({"tick_lower": 380000, "tick_upper": -160000}, "ordered"),
    ({"tick_lower": -887400}, "in bounds"),
    ({"hooks": "0x" + "77" * 20}, "no pinned code hash"),
    ({"amount0_raw": 0, "amount1_raw": 0}, "amounts"),
    ({"amount0_raw": 2 ** 128}, "uint128"),
    ({"amount0_raw": 0, "tick_lower": 200000, "tick_upper": 380000}, "no liquidity"),
    ({"sqrt_price_x96": 1}, "price"),
    ({"gap_bps": 501}, "gap_bps"),
    ({"gap_bps": 0}, "gap_bps"),
    ({"price_source": "feed"}, "price_source"),
    ({"pool_id": "0x" + "11" * 32}, "pool_id"),
    ({"chain": "solana"}, "no pinned Uniswap v4"),
    ({"owner": "0x" + "99" * 20}, "unknown declaration field"),
    ({"currency1": "0x1234"}, "not an address"),
    ({"fee": True}, "whole number"),
])
def test_a_declaration_the_guard_would_refuse_is_never_stored(change, why):
    with pytest.raises(S.SeedError, match=why):
        S.validate({**BASE, **change})


def test_the_states_move_one_way_and_a_declaration_is_one_use(db):
    ref, f = S.propose("owner", BASE, origin="agent", path=db)
    assert S.get(ref, user_id="owner", path=db)["state"] == S.PROPOSED
    assert not S.claim(ref, path=db)                         # not confirmed: no use to take
    S.confirm(ref, user_id="owner", path=db)
    with pytest.raises(S.SeedError, match="CONFIRMED, not open"):
        S.confirm(ref, user_id="owner", path=db)
    assert S.claim(ref, path=db) and not S.claim(ref, path=db)   # one use
    assert S.release(ref, note="reverted", path=db)              # nothing moved: whole again
    assert S.claim(ref, path=db)
    assert S.finish(ref, S.SEEDED, token_id=77, tx_hash="0xab", path=db)
    row = S.get(ref, user_id="owner", path=db)
    assert (row["state"], row["token_id"], row["tx_hash"]) == (S.SEEDED, "77", "0xab")
    assert not S.claim(ref, path=db) and not S.release(ref, path=db)
    with pytest.raises(ValueError):
        S.finish(ref, S.CONFIRMED, path=db)


def test_another_user_and_an_unknown_ref_read_as_no_declaration(db):
    ref, _ = S.propose("owner", BASE, origin="agent", path=db)
    for who, which in (("other", ref), ("owner", "s0000000000")):
        with pytest.raises(S.SeedError, match="no seed declaration"):
            S.get(which, user_id=who, path=db)
    with pytest.raises(S.SeedError, match="no seed declaration"):
        S.confirm(ref, user_id="other", path=db)


def test_a_proposal_and_a_confirmation_expire(db):
    ref, _ = S.propose("owner", BASE, origin="agent", path=db, now=1000.0)
    late = 1000.0 + S.PROPOSED_TTL_S + 1
    with pytest.raises(S.SeedError, match="EXPIRED"):
        S.confirm(ref, user_id="owner", path=db, now=late)
    ref, _ = S.propose("owner", BASE, origin="agent", path=db, now=1000.0)
    S.confirm(ref, user_id="owner", path=db, now=2000.0)
    assert S.get(ref, user_id="owner", path=db, now=2000.0 + S.CONFIRMED_TTL_S - 1)["state"] == S.CONFIRMED
    assert S.get(ref, user_id="owner", path=db, now=2000.0 + S.CONFIRMED_TTL_S + 1)["state"] == S.EXPIRED
    assert not S.claim(ref, path=db)


def test_a_missing_store_is_no_declaration_and_an_unreadable_one_says_so(tmp_path):
    missing = str(tmp_path / "none.db")
    with pytest.raises(S.SeedError):
        S.get("s0000000000", user_id="owner", path=missing)
    assert not (tmp_path / "none.db").exists()            # a read never creates the store
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a sqlite file" * 100)
    with pytest.raises(S.SeedUnreadable):
        S.get("s0000000000", user_id="owner", path=str(broken))


def test_the_card_carries_only_the_confirm_line_and_the_validated_numbers(tmp_path, db):
    st = cards.CardStore(str(tmp_path / "cards.db"))
    ref, f = S.propose("owner", {**BASE, "price_source": "agent"}, origin="agent", path=db)
    card = S.seed_card("owner", ref, f, origin="agent", st=st, notes=("the pool does NOT exist yet",))
    assert card.line == f"/lp seed confirm {ref}"
    body = card.body
    assert f["pool_id"] in body and "fee 1%" in body and "no hook" in body
    assert "at most 2 ETH and 12,750,000 TKN" in body
    assert "the AGENT proposed" in body and "does NOT exist yet" in body
    assert "sends nothing" in body and "raises no limit" in body
    assert len(body) <= cards.MAX_BODY_CHARS
    owner = S.card_body(S.validate({**BASE, "price_source": "owner"}))
    assert "a number you typed" in owner


@pytest.mark.parametrize("words", [
    ["/lp", "seed", "run", "s0123456789"], ["/lp", "add", "native", "x"],
    ["/lp", "seed", "confirm"], ["/lp", "seed", "confirm", "s1", "go"],
    ["/lp", "remove", "42", "go"],
])
def test_a_job_card_carries_lp_only_as_seed_confirm(tmp_path, words):
    st = cards.CardStore(str(tmp_path / "cards.db"))
    with pytest.raises(ValueError):
        cards.job_card("owner", words, title="t", st=st)
