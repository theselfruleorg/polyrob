"""071 W2 — one Screen over every source; a source that did not answer never
turns a check into a pass.

Provider payloads are trimmed copies of LIVE answers recorded 2026-10-02.
"""
import pytest

from tools.defi import token_screen as ts
from tools.defi.providers import token_audits as ta
from tools.defi.providers.base import ScreenVerdict


# --------------------------------------------------------------------------
# Provider parsers
# --------------------------------------------------------------------------

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUMP = "Hu6xf6bNiJ5ErRaG7Yhos89jgfAdCG1tu8DCips2pump"

JUP_USDC = [{"id": USDC, "symbol": "USDC", "mintAuthority": "BJE5", "freezeAuthority": "7dGb",
             "holderCount": 5248202, "audit": {"topHoldersPercentage": 26.237, "devMints": 1},
             "organicScore": 100, "organicScoreLabel": "high", "isVerified": True,
             "tags": ["strict", "verified", "stable"]}]
JUP_PUMP = [{"id": PUMP, "symbol": "HATS", "launchpad": "pump.fun",
             "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "devMints": 1},
             "organicScore": 0, "tags": ["unknown", "token-2022"]}]


def test_jupiter_parses_audit_and_authority_absence():
    t = ta.parse_jupiter(USDC, JUP_USDC)
    assert t.is_verified is True and t.organic_score == 100
    # audit omits *Disabled when FALSE; the top-level authority is the cross-check.
    assert t.mint_authority_disabled is False and t.freeze_authority_disabled is False
    assert t.top_holders_pct == pytest.approx(26.237) and t.holder_count == 5248202
    p = ta.parse_jupiter(PUMP, JUP_PUMP)
    assert p.mint_authority_disabled is True and p.is_verified is None
    assert p.launchpad == "pump.fun"


def test_jupiter_not_indexed_is_none_and_match_is_exact():
    assert ta.parse_jupiter(PUMP, JUP_USDC) is None
    assert ta.parse_jupiter(USDC.lower(), JUP_USDC) is None, "base58 is case-SENSITIVE"
    with pytest.raises(ValueError):
        ta.parse_jupiter(USDC, {"error": "x"})


def test_rugcheck_parses_score_and_named_risks():
    r = ta.parse_rugcheck({"tokenProgram": "Tokenz", "risks": [
        {"name": "Single holder ownership", "value": "50.00%", "score": 4999, "level": "danger"}],
        "score": 5000, "score_normalised": 40, "lpLockedPct": 0})
    assert r.score_normalised == 40 and r.danger[0].name == "Single holder ownership"
    with pytest.raises(ValueError):
        ta.parse_rugcheck({"error": "unable to generate report"})


def test_honeypot_parses_simulation_and_refuses_an_incomplete_one():
    h = ta.parse_honeypot({"simulationSuccess": True, "honeypotResult": {"isHoneypot": False},
                           "simulationResult": {"buyTax": 0, "sellTax": 12.5, "transferTax": 0},
                           "pairAddress": "0xP", "pair": {"liquidity": 1629383.4},
                           "contractCode": {"isProxy": False}})
    assert h.simulated and h.is_honeypot is False and h.sell_tax_pct == 12.5
    bad = ta.parse_honeypot({"simulationSuccess": False, "simulationError": "no pair",
                             "honeypotResult": {}, "simulationResult": {"sellTax": 0}})
    assert not bad.simulated and bad.sell_tax_pct is None, "a tax from a failed sim is not a tax"


def test_honeypot_other_chains_are_not_covered():
    with pytest.raises(ta.SourceUnavailable, match="not covered"):
        ta.honeypot_sim("robinhood", "0x" + "1" * 40)


def test_providers_never_reach_the_network_in_unit_tests():
    with pytest.raises(RuntimeError, match="network"):
        ta.jupiter_token(USDC)


# --------------------------------------------------------------------------
# Merge rules
# --------------------------------------------------------------------------

def test_a_failed_source_leaves_its_checks_not_checked():
    s = ts.merge([ts.from_goplus(ScreenVerdict(available=False), "evm"),
                  ts.failed_source("honeypot.is", "chain not covered", ts.HONEYPOT_CHECKS)])
    assert not s.available and s.partial
    names = {n.name for n in s.not_checked}
    assert {"is_honeypot", "sell_tax", "buy_tax"} <= names
    reason = next(n.reason for n in s.not_checked if n.name == "is_honeypot")
    assert "goplus: no answer" in reason and "honeypot.is: chain not covered" in reason


def test_a_gap_closes_only_when_another_source_ran_the_check():
    gp = ScreenVerdict(available=True, checks={"cannot_buy": "0"}, missing=["is_honeypot", "sell_tax"])
    sim = ta.HoneypotSim(simulated=True, is_honeypot=False, buy_tax_pct=0.0, sell_tax_pct=None)
    s = ts.merge([ts.from_goplus(gp, "evm"), ts.from_honeypot(sim)])
    names = {n.name for n in s.not_checked}
    assert "is_honeypot" not in names, "honeypot.is ran it"
    assert "sell_tax" in names, "nobody ran it — still NOT CHECKED"


def test_both_sources_are_shown_so_a_disagreement_is_visible():
    gp = ScreenVerdict(available=True, checks={"is_honeypot": "0"}, missing=[])
    sim = ta.HoneypotSim(simulated=True, is_honeypot=True, buy_tax_pct=0.0, sell_tax_pct=99.0)
    s = ts.merge([ts.from_goplus(gp, "evm"), ts.from_honeypot(sim)])
    text = "\n".join(ts.render(s))
    assert "0 [goplus]" in text and "1 [honeypot.is]" in text
    assert "HARD FAIL" in text and "is_honeypot (honeypot.is)" in text


def test_incomplete_simulation_is_not_checked_never_a_pass():
    sim = ta.HoneypotSim(simulated=False, reason="no pair")
    s = ts.merge([ts.from_honeypot(sim)])
    assert not s.available and {"is_honeypot", "sell_tax"} <= {n.name for n in s.not_checked}


# --------------------------------------------------------------------------
# Solana facts → checks
# --------------------------------------------------------------------------

def _facts(**kw):
    from core.wallet.spl_facts import MintFacts
    base = dict(mint="M", program="spl-token-2022", decimals=6, supply_raw=10 ** 15,
                mint_authority=None, freeze_authority=None, extensions={})
    base.update(kw)
    return MintFacts(**base)


def test_permanent_delegate_is_a_hard_fail():
    rep = ts.from_svm_mint(_facts(extensions={"permanentDelegate": {"delegate": "D"}}))
    assert "permanent_delegate" in rep.hard_fails


@pytest.mark.parametrize("ext,hard", [
    ({"nonTransferable": {}}, "non_transferable"),
    ({"defaultAccountState": {"accountState": "frozen"}}, "default_account_state_frozen"),
])
def test_unsellable_extensions_are_hard_fails(ext, hard):
    assert hard in ts.from_svm_mint(_facts(extensions=ext)).hard_fails


def test_transfer_fee_and_hook_are_flagged_with_numbers():
    rep = ts.from_svm_mint(_facts(extensions={
        "transferFeeConfig": {"olderTransferFee": {"transferFeeBasisPoints": 0},
                              "newerTransferFee": {"transferFeeBasisPoints": 250},
                              "transferFeeConfigAuthority": "A", "withdrawWithheldAuthority": "W"},
        "transferHook": {"programId": "HOOK", "authority": None}}))
    assert "transfer_fee_250bps" in rep.flags and "transfer_hook_active" in rep.flags
    assert "transfer_fee_upgradable" in rep.flags
    assert any(c.name == "transfer_fee_withdraw_authority" and c.result == "W" for c in rep.checks)


def test_live_authorities_are_flags_and_revoked_ones_are_clean():
    rep = ts.from_svm_mint(_facts(mint_authority="MA", freeze_authority="FA"))
    assert {"mintable", "freezable"} <= set(rep.flags) and not rep.hard_fails
    clean = ts.from_svm_mint(_facts(program="spl-token"))
    assert not clean.flags and not clean.hard_fails and not clean.not_checked


def test_unassessed_extension_is_not_checked():
    rep = ts.from_svm_mint(_facts(extensions={"brandNewThing": {}}))
    assert [n.name for n in rep.not_checked] == ["extension:brandNewThing"]


def test_pump_curve_states_render():
    from core.wallet.spl_facts import PumpCurve
    on = ts.from_pump_curve(PumpCurve(state="on_curve", real_token_reserves=793_100_000_000_000 // 4,
                                      real_sol_reserves=60 * 10 ** 9))
    assert "ON CURVE" in on.checks[0].result and "75.0%" in on.checks[0].result
    assert "graduated" in ts.from_pump_curve(PumpCurve(state="graduated")).checks[0].result
    assert "not a pump.fun" in ts.from_pump_curve(PumpCurve(state="not_pump")).checks[0].result


def test_svm_holders_rows_become_a_holder_report():
    from core.wallet.spl_facts import HolderLine
    lines = [HolderLine("TA1", "CURVE", 600, 0.6, True, "pump.fun bonding curve"),
             HolderLine("TA2", "WALLET", 300, 0.3, False)]
    rep = ts.from_svm_holders(lines, 1000.0)
    assert rep.holders.available and rep.holders.top_holders[0].tag == "pump.fun bonding curve"
    assert rep.holders.top_percent_wallets == pytest.approx(0.3)
    assert "program-owned" in rep.notes[0]


# --------------------------------------------------------------------------
# Gather — isolation and the budget
# --------------------------------------------------------------------------

def test_gather_on_solana_with_every_source_down_is_all_not_checked():
    reps = ts.gather_facts("solana", PUMP, budget=5)
    s = ts.merge(reps)
    assert not s.available
    names = {n.name for n in s.not_checked}
    assert {"mintable", "permanent_delegate", "pump_curve", "holder_concentration",
            "organic_score", "rugcheck_score"} <= names


def test_gather_on_robinhood_names_honeypot_not_covered(monkeypatch):
    reps = ts.gather_facts("robinhood", "0x" + "2" * 40, budget=5)
    s = ts.merge(reps)
    reason = next(n.reason for n in s.not_checked if n.name == "is_honeypot")
    assert "honeypot.is: chain not covered" in reason


def test_a_slow_source_times_out_into_not_checked(monkeypatch):
    import time
    monkeypatch.setattr(ts, "_jobs", lambda fam: [
        ("slow", lambda c, a: (time.sleep(1.0), [])[1], ["x"]),
        ("fast", lambda c, a: [ts.SourceReport("fast", checks=[ts.Check("y", "ok", "fast")])], ["y"])])
    s = ts.merge(ts.gather_facts("base", "0x" + "3" * 40, budget=0.2))
    assert [c.name for c in s.checks] == ["y"]
    assert s.not_checked[0].name == "x" and "timed out" in s.not_checked[0].reason


def test_svm_holders_fallback_names_the_failure_and_the_key_remedy():
    rep = ts.svm_holders(PUMP)
    assert not rep.available and "DEFI_SOLANA_RPC" in rep.reason


# --------------------------------------------------------------------------
# Render conventions
# --------------------------------------------------------------------------

def test_render_unavailable_says_unscreened_and_never_safe():
    text = "\n".join(ts.render(ts.merge([ts.from_goplus(None, "evm")])))
    assert "UNSCREENED" in text and "unavailable" in text and "safe" not in text.lower()


def test_render_complete_clean_screen_is_not_partial():
    s = ts.merge([ts.from_goplus(ScreenVerdict(True, {"is_honeypot": "0"}, [], []), "evm")])
    text = "\n".join(ts.render(s))
    assert "no risk flags raised" in text and "PARTIAL" not in text


# --------------------------------------------------------------------------
# Compact render (owner UX): flags once, passes on one line, gaps by reason
# --------------------------------------------------------------------------

def _aero_like():
    gp = ScreenVerdict(True, {"is_honeypot": "0", "is_mintable": "1", "buy_tax": "0",
                              "sell_tax": "0", "hidden_owner": "0"},
                       ["mintable"], ["cannot_sell_all"])
    hp = ts.SourceReport("honeypot.is", checks=[ts.Check("is_honeypot", "0", "honeypot.is"),
                                                ts.Check("sell_tax", "0% (simulated)", "honeypot.is")])
    rpc = ts.SourceReport("rpc", checks=[ts.Check("proxy", "no standard proxy slot set", "rpc"),
                                         ts.Check("owner", "renounced (owner() = zero address)", "rpc")])
    return ts.merge([ts.from_goplus(gp, "evm"), hp, rpc])


def test_a_minor_gap_covered_by_another_source_is_not_partial():
    """goplus lacked cannot_sell_all, but honeypot.is simulated the sell."""
    s = _aero_like()
    assert s.partial and ts.material_gaps(s) == []
    text = "\n".join(ts.render(s))
    assert "PARTIAL" not in text
    assert "not run: cannot_sell_all" in text and "not a pass" in text
    assert "not a honeypot" in text and "0% buy/sell tax" in text
    assert "is_hidden" not in text and len(text.splitlines()) <= 6


def test_a_material_check_with_no_source_makes_it_partial():
    gp = ScreenVerdict(True, {"is_honeypot": "0"}, [], ["is_mintable"])
    s = ts.merge([ts.from_goplus(gp, "evm")])
    assert ts.material_gaps(s) == ["mint authority"]
    assert "PARTIAL: no source checked mint authority" in "\n".join(ts.render(s))


def test_one_fact_from_two_sources_is_one_flag_line_and_issuer_note():
    gp = ScreenVerdict(True, {"is_proxy": "1"}, ["proxy_upgradeable"], [])
    rpc = ts.SourceReport("rpc", checks=[ts.Check(
        "proxy", "YES (eip1967) — implementation 0xI; proxy admin 0xA: the code can be "
        "replaced after you buy", "rpc")], flags=["upgradeable_proxy"])
    lines = ts.render(ts.merge([ts.from_goplus(gp, "evm"), rpc]), canonical_symbol="USDC")
    flags = [ln for ln in lines if "FLAG" in ln]
    assert len(flags) == 1 and "[goplus, rpc]" in flags[0]
    assert "proxy admin 0xA" in flags[0] and "(normal for USDC; issuer-controlled)" in flags[0]


def test_plain_words_for_numbers_and_failures():
    assert ts.plain_number(8.79944e13) == "87,994,400,000,000"
    assert ts.plain_number(None) == "unknown"
    assert ts.plain_reason("ConnectError: [Errno 54] Connection reset by peer") == "no answer"
    assert ts.plain_reason("rate-limited (429)") == "rate-limited"
    assert ts.plain_errors("tx list (HTTPError); internals (TimeoutError)") == \
        "tx list (no answer); internals (timed out)"
    assert ts.quoted("AE\x00RO\n") == '"AERO "' and ts.quoted(None) is None
