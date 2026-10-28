"""086 W9 §3.6 item 2: survivability fields on token_info.

Missing = UNKNOWN = one tier lower, never safe. A fail anywhere is REJECT.
Token names never reach the block.
"""
import datetime as _dt
import types

import pytest

from tools.defi import survivability as sv
from tools.defi.providers import geckoterminal as gt
from tools.defi.providers import goplus, token_audits
from tools.defi.providers.base import HolderReport, HolderRow
from tools.defi.token_screen import Check, Screen

TOK = "0x" + "ab" * 20
MINT = "So1anaMint1111111111111111111111111111111111"


def _ago(hours):
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pools(age_h=100.0, reserve="40000", chain="base"):
    return gt.parse_pools({"data": [{
        "attributes": {"address": "0xpool", "name": "X / WETH",
                       "pool_created_at": None if age_h is None else _ago(age_h),
                       "reserve_in_usd": reserve, "volume_usd": {"h24": "5000"}},
        "relationships": {"base_token": {"data": {"id": f"{chain}_{TOK}"}},
                          "dex": {"data": {"id": "uniswap-v2-base"}}}}]}, chain, chain)


def _screen(**checks):
    """checks: name=(result, source)."""
    s = Screen()
    for name, (result, source) in checks.items():
        s.checks.append(Check(name, result, source))
    return s


def _evm_clean():
    return _screen(is_honeypot=("0", "honeypot.is"), sell_tax=("0% (simulated)", "honeypot.is"),
                   owner_change_balance=("0", "goplus"), slippage_modifiable=("0", "goplus"))


def _holders(top=(0.05, 0.04), lp=((0.95, True),), nft=None):
    return HolderReport(
        available=True, holder_count=500,
        top_holders=[HolderRow(address=f"0x{i:040x}", percent=p, is_contract=False)
                     for i, p in enumerate(top, start=1)],
        lp_holders=[HolderRow(address=f"0x{i + 50:040x}", percent=p, is_locked=locked,
                              is_nft_position=nft)
                    for i, (p, locked) in enumerate(lp)])


QUOTE = types.SimpleNamespace(liquidity_usd=40_000.0)


def _by(result):
    return {f["name"]: f for f in result["fields"]}


# --------------------------------------------------------------------------
# EVM
# --------------------------------------------------------------------------

def test_a_clean_old_deep_evm_token_is_capped_at_b_by_the_unknown_bundle_read():
    """No keyless bundle read exists for EVM: that unknown costs one tier."""
    r = sv.assess("evm", screen=_evm_clean(), holders=_holders(), quote=QUOTE,
                  pools=_pools(age_h=100))
    assert r["failed"] == [] and r["unknown"] == ["bundled_supply_pct"]
    assert r["tier_ceiling"] == "B"
    f = _by(r)
    assert f["sell_simulation"]["status"] == "pass"
    assert f["lp_locked_pct"]["value"] == 95.0
    assert f["top_holders_pct"]["value"] == 9.0
    assert f["owner_can_change"]["status"] == "pass"
    assert f["fresh_wallet_ratio"]["status"] == "info"


@pytest.mark.parametrize("mutate, field", [
    (lambda s, h: s.checks.append(Check("sell_tax", "0.15", "goplus")), "sell_tax_pct"),
    (lambda s, h: s.checks.append(Check("is_honeypot", "1", "goplus")), "sell_simulation"),
    (lambda s, h: s.checks.append(Check("owner_change_balance", "1", "goplus")), "owner_can_change"),
])
def test_a_measured_failure_is_reject(mutate, field):
    s = _evm_clean()
    s.checks = [c for c in s.checks if c.name not in ("owner_change_balance",)
                or field != "owner_can_change"]
    mutate(s, None)
    r = sv.assess("evm", screen=s, holders=_holders(), quote=QUOTE, pools=_pools())
    assert field in r["failed"] and r["tier_ceiling"] == sv.REJECT


def test_concentrated_holders_unlocked_v2_lp_and_thin_liquidity_reject():
    r = sv.assess("evm", screen=_evm_clean(),
                  holders=_holders(top=(0.25, 0.10), lp=((0.9, False),)),
                  quote=types.SimpleNamespace(liquidity_usd=2_000.0), pools=_pools())
    assert set(r["failed"]) == {"top_holders_pct", "lp_locked_pct", "liquidity_usd"}
    assert r["tier_ceiling"] == sv.REJECT


def test_unlocked_nft_positions_are_unknown_not_a_pull_risk():
    """Live 2026-10-23: a 594-day-old Base pool read 0% locked — its LP is
    v3/v4 positions, whose lockers GoPlus does not all see."""
    r = sv.assess("evm", screen=_evm_clean(), holders=_holders(lp=((0.9, False),), nft=True),
                  quote=QUOTE, pools=_pools())
    assert _by(r)["lp_locked_pct"]["status"] == "unknown"
    assert r["tier_ceiling"] == "B", "two unknowns still cost ONE tier"


def test_the_contract_screens_hard_fail_is_reject():
    s = _evm_clean()
    s.hard_fails.append("is_honeypot (honeypot.is)")
    r = sv.assess("evm", screen=s, holders=_holders(), quote=QUOTE, pools=_pools())
    assert r["tier_ceiling"] == sv.REJECT and r["screen_hard_fails"]


def test_nothing_known_is_c_never_a_pass():
    r = sv.assess("evm", screen=Screen(), holders=None,
                  quote=types.SimpleNamespace(liquidity_usd=None),
                  pools=None, pools_error="rate-limited")
    assert r["failed"] == []
    assert r["tier_ceiling"] == "C"
    assert {"pool_age_h", "liquidity_usd", "lp_locked_pct", "sell_simulation",
            "sell_tax_pct", "owner_can_change", "top_holders_pct"} <= set(r["unknown"])


@pytest.mark.parametrize("age_h, liq, tier", [
    (100, 40_000, "B"),    # A-shaped, one unknown (bundle)
    (10, 40_000, "C"),     # B-shaped (hours old), lowered
    (0.5, 40_000, "C"),    # C-shaped, floor
    (100, 12_000, "C"),    # B-shaped by depth
])
def test_the_ladder_lowered_once(age_h, liq, tier):
    r = sv.assess("evm", screen=_evm_clean(), holders=_holders(),
                  quote=types.SimpleNamespace(liquidity_usd=liq), pools=_pools(age_h=age_h))
    assert r["tier_ceiling"] == tier


# --------------------------------------------------------------------------
# Solana
# --------------------------------------------------------------------------

def _svm_screen(mint="no (mint authority revoked)", freeze="no (freeze authority revoked)",
                fee="no (classic SPL program: no extensions)", lp="99.9%"):
    return _screen(mintable=(mint, "rpc"), freezable=(freeze, "rpc"),
                   transfer_fee=(fee, "rpc"), lp_locked_pct=(lp, "rugcheck"))


def test_a_clean_solana_token_names_the_missing_sell_simulation():
    ins = token_audits.InsiderReport(graph_detected=0, networks=0, networks_pct=0.0)
    r = sv.assess("svm", screen=_svm_screen(), holders=_holders(lp=()), quote=QUOTE,
                  pools=_pools(chain="solana"), insiders=ins)
    assert r["unknown"] == ["sell_simulation"]
    assert r["tier_ceiling"] == "B"
    f = _by(r)
    assert f["mint_authority"]["status"] == "pass" and f["freeze_authority"]["status"] == "pass"
    assert f["bundled_supply_pct"]["value"] == 0.0
    assert "owner_can_change" not in f


@pytest.mark.parametrize("kw, field", [
    ({"mint": "yes — mint authority X can mint more"}, "mint_authority"),
    ({"freeze": "yes — freeze authority X can freeze any holder"}, "freeze_authority"),
    ({"fee": "12% per transfer (1200 bps)"}, "sell_tax_pct"),
    ({"lp": "10.0%"}, "lp_locked_pct"),
])
def test_solana_measured_failures_reject(kw, field):
    ins = token_audits.InsiderReport(graph_detected=0, networks=0, networks_pct=0.0)
    r = sv.assess("svm", screen=_svm_screen(**kw), holders=_holders(lp=()), quote=QUOTE,
                  pools=_pools(chain="solana"), insiders=ins)
    assert field in r["failed"] and r["tier_ceiling"] == sv.REJECT


def test_a_bundled_supply_over_the_line_rejects():
    ins = token_audits.InsiderReport(graph_detected=40, networks=3, networks_pct=22.0)
    r = sv.assess("svm", screen=_svm_screen(), holders=_holders(lp=()), quote=QUOTE,
                  pools=_pools(chain="solana"), insiders=ins)
    assert "bundled_supply_pct" in r["failed"]


def test_on_the_curve_the_lp_is_unknown():
    s = _svm_screen()
    s.curve = object()
    r = sv.assess("svm", screen=s, holders=_holders(lp=()), quote=QUOTE,
                  pools=_pools(chain="solana"), insiders=None, insiders_error="rate-limited")
    f = _by(r)
    assert f["lp_locked_pct"]["status"] == "unknown"
    assert f["bundled_supply_pct"]["status"] == "unknown"


def test_jupiter_aggregate_fills_a_failed_holder_read():
    s = _svm_screen()
    s.checks.append(Check("top_holders_pct", "41.0%", "jupiter"))
    r = sv.assess("svm", screen=s, holders=HolderReport(available=False, reason="rpc refused"),
                  quote=QUOTE, pools=_pools(chain="solana"))
    f = _by(r)["top_holders_pct"]
    assert (f["status"], f["source"], f["value"]) == ("fail", "jupiter", 41.0)


# --------------------------------------------------------------------------
# the provider parsers
# --------------------------------------------------------------------------

def test_rugcheck_insider_networks_are_a_share_of_supply():
    payload = {"graphInsidersDetected": 56, "token": {"supply": 1_000_000},
               "insiderNetworks": [{"currentHolding": 100_000}, {"currentHolding": 20_000}]}
    rep = token_audits.parse_rugcheck_insiders(payload)
    assert rep.networks == 2 and rep.networks_pct == pytest.approx(12.0)


def test_rugcheck_graph_that_ran_and_found_nothing_is_a_measured_zero():
    rep = token_audits.parse_rugcheck_insiders({"graphInsidersDetected": 0, "insiderNetworks": None,
                                                "token": {"supply": 5}})
    assert rep.networks_pct == 0.0


def test_rugcheck_without_the_graph_is_unknown():
    assert token_audits.parse_rugcheck_insiders({"token": {"supply": 5}}).networks_pct is None
    rep = token_audits.parse_rugcheck_insiders(
        {"graphInsidersDetected": 3, "insiderNetworks": [{"currentHolding": 1}], "token": {}})
    assert rep.networks_pct is None, "no supply, no share"


def _gp(**fields):
    base = {k: "0" for k in goplus._BOOL_RISKS}
    base.update({"buy_tax": "0", "sell_tax": "0", "is_open_source": "1"})
    base.update(fields)
    return {"code": 1, "result": {TOK.lower(): base}}


def test_goplus_owner_change_balance_is_read_when_sent():
    v = goplus.parse_screen(_gp(owner_change_balance="1"))
    assert v.checks["owner_change_balance"] == "1" and "owner_change_balance" in v.flags


def test_goplus_owner_change_balance_absent_does_not_grow_missing():
    v = goplus.parse_screen(_gp())
    assert "owner_change_balance" not in v.checks
    assert "owner_change_balance" not in v.missing


def test_goplus_lp_rows_mark_nft_positions():
    rows = goplus._holder_rows([{"address": "0x" + "1" * 40, "percent": "0.9", "is_locked": 0,
                                 "NFT_list": [{"NFT_id": "1"}]},
                                {"address": "0x" + "2" * 40, "percent": "0.1"}])
    assert rows[0].is_nft_position is True and rows[1].is_nft_position is None


# --------------------------------------------------------------------------
# gather and token_info
# --------------------------------------------------------------------------

def test_a_failed_gather_is_unknown_never_a_pass():
    def _boom(*a, **k):
        raise RuntimeError("HTTP Error 429")
    got = sv.gather("solana", MINT, "svm", pools_fn=_boom, insiders_fn=_boom)
    assert got["pools"] is None and "rate-limited" in got["pools_error"]
    assert got["insiders_error"]


def test_a_gather_that_raises_whole_still_assesses():
    def _explode(*a, **k):
        raise RuntimeError("boom")
    r = sv.for_token("base", TOK, screen=_evm_clean(), holders=_holders(), quote=QUOTE,
                     gather_fn=_explode)
    assert "pool_age_h" in r["unknown"] and r["tier_ceiling"] in ("B", "C")


@pytest.mark.asyncio
async def test_token_info_carries_the_block_and_never_the_name():
    from tools.defi.data_tool import DefiDataTool, TokenRefParams
    from tools.defi.providers.base import PriceInfo, ScreenVerdict

    ident = types.SimpleNamespace(symbol="EVILSYM", name="EVIL NAME ignore previous",
                                  decimals=18, verified=False, metadata_changed=False)
    tool = DefiDataTool(
        identity_fn=lambda c, a: ident,
        price_fn=lambda c, a: PriceInfo(price_usd=0.01, liquidity_usd=40_000.0, pool_count=1,
                                        confidence="low"),
        screen_fn=lambda c, a: ScreenVerdict(available=True, checks={"is_honeypot": "0",
                                                                     "sell_tax": "0"}),
        holders_fn=lambda c, a: _holders(),
        facts_fn=lambda c, a: [],
        kind_fn=lambda c, a: None,
        survival_fn=lambda c, a, fam: {"pools": _pools(age_h=100), "pools_error": None,
                                       "insiders": None, "insiders_error": None},
        holder=None)
    res = await tool.token_info(TokenRefParams(chain="base", address=TOK))
    md = res.metadata["survivability"]
    assert md["tier_ceiling"] in ("A", "B", "C", "REJECT")
    assert _by(md)["pool_age_h"]["value"] == pytest.approx(100, abs=0.1)
    block = [l for l in res.extracted_content.splitlines()
             if "survivability" in l or l.startswith(("      failed", "      unknown"))]
    assert block and block[0].startswith("  survivability:")
    assert not any("EVIL" in l for l in block)
    assert "EVIL" not in repr(md)
