"""W4 (structural review 2026-10-10): one source per KIND of verb.

"Outbound write", "the text that leaves the box", "produced output", "an X API
write" and "a read the cron/data tool may run" are columns of the per-action
policy rows (``core/verb_policy.py``). Every list that used to hold these names
by hand is a derived view; this file pins each view to its rows and pins the
membership the hand lists had (the live names), so a derivation can never drop
a verb that a gate or a seat relied on.
"""
import pytest

from core import verb_policy as vp
from core.verb_policy import VerbPolicy

#: The live X browser action names (tool ``x_browser``, method ``x_post`` ->
#: ``x_browser_x_post``). The hand lists named the bare ``x_post``, which nothing
#: emits, so the X browser rail was missing from the claim gate and the evidence.
X_BROWSER_WRITES = {"x_browser_x_post", "x_browser_x_reply", "x_browser_x_dm"}
TWITTER_TEXT_WRITES = {"twitter_post", "twitter_reply", "twitter_quote",
                       "twitter_thread", "twitter_dm"}
EMAIL_WRITES = {"email_send", "email_reply", "email_forward"}


# -- the row columns ------------------------------------------------------------

def test_a_row_validates_its_outbound_columns():
    VerbPolicy("a", outbound="write", text_fields=["text"], late_send=True)
    with pytest.raises(ValueError, match="outbound"):
        VerbPolicy("a", outbound="loud")
    with pytest.raises(ValueError, match="text_fields"):
        VerbPolicy("a", text_fields=("text",))           # words leave only on an outbound verb
    with pytest.raises(ValueError, match="late_send"):
        VerbPolicy("a", outbound="text", late_send=True)  # a late send is a pure write
    with pytest.raises(ValueError, match="read_job"):
        VerbPolicy("a", effect="money", read_job=True)
    row = VerbPolicy("a", outbound="write", text_fields=["text", "body"])
    assert row.text_fields == ("text", "body")            # a TOML list lands as a tuple
    hash(row)


# -- outbound ---------------------------------------------------------------------

def test_the_claim_gate_fields_are_the_rows():
    from tools.controller.financial_claim_gate import OUTBOUND_TEXT_FIELDS
    assert OUTBOUND_TEXT_FIELDS == {
        n: vp.policy_for(n).text_fields for n in vp.ids_where(text_fields=bool)}
    assert {"message", "send_message", "agent_nft_journal", "agent_nft_bind_identity"} \
        | TWITTER_TEXT_WRITES | EMAIL_WRITES | X_BROWSER_WRITES <= set(OUTBOUND_TEXT_FIELDS)
    assert OUTBOUND_TEXT_FIELDS["twitter_post"] == ("text", "poll_options")
    assert OUTBOUND_TEXT_FIELDS["email_send"] == ("subject", "body", "html")
    assert not {"x_post", "x_reply", "x_dm"} & set(OUTBOUND_TEXT_FIELDS)


def test_the_late_send_writes_are_the_rows():
    from tools.controller.approval_queue import OUTBOUND_WRITE_TOOLS
    assert OUTBOUND_WRITE_TOOLS == vp.ids_where(late_send=True)
    # The live members of the old hand list (its x_* names were never emitted).
    assert OUTBOUND_WRITE_TOOLS == TWITTER_TEXT_WRITES | EMAIL_WRITES


def test_every_outbound_write_counts_as_produced_output():
    from agents.task.runtime.evidence import OUTPUT_ACTION_ALLOWLIST
    assert OUTPUT_ACTION_ALLOWLIST == (vp.ids_where(output=True)
                                       | vp.ids_where(outbound="write"))
    old = {"twitter_post", "twitter_reply", "filesystem_write_file", "knowledge_kb_ingest",
           "x402_invoice_x402_request", "message", "email_send", "email_reply",
           "email_forward", "coding_apply_patch", "coding_str_replace",
           "coding_create_file", "code_execution_run_code"}
    assert old <= OUTPUT_ACTION_ALLOWLIST
    assert TWITTER_TEXT_WRITES | X_BROWSER_WRITES <= OUTPUT_ACTION_ALLOWLIST


def test_the_twitter_write_gate_is_the_rows():
    from polyrob_x.twitter_tool import _twitter_write_actions
    assert _twitter_write_actions() == {
        "twitter_post", "twitter_reply", "twitter_quote", "twitter_thread",
        "twitter_delete_tweet", "twitter_like", "twitter_unlike", "twitter_retweet",
        "twitter_unretweet", "twitter_follow", "twitter_unfollow", "twitter_mute",
        "twitter_unmute", "twitter_block", "twitter_dm"}


def test_the_refusal_taint_outbound_set_keeps_every_old_member():
    from tools.controller import refusal_taint_gate as g
    names = g.outbound_names()
    assert names == vp.ids_where(outbound=bool)
    assert {"publish", "github_issue_create", "github_pr_comment", "message",
            "send_message", "agent_nft_journal"} | TWITTER_TEXT_WRITES | EMAIL_WRITES \
        | X_BROWSER_WRITES <= names


# -- reads ------------------------------------------------------------------------

def test_the_market_data_read_sets_are_the_rows():
    from core.action_names import bare_action_name
    from polyrob_markets.hyperliquid.service import hl_read_actions
    from polyrob_markets.polymarket.service import pm_read_actions
    HL_READ_ACTIONS, PM_READ_ACTIONS = hl_read_actions(), pm_read_actions()
    assert HL_READ_ACTIONS == {bare_action_name("hyperliquid_data", n)
                               for n in vp.ids_where(tool="hyperliquid_data")}
    assert HL_READ_ACTIONS == {
        "get_perpetual_markets", "get_spot_markets", "get_current_price", "get_all_mids",
        "get_orderbook", "get_funding_rate", "get_account_state", "get_spot_balances",
        "get_open_orders", "get_fills", "agent_status"}
    assert PM_READ_ACTIONS == {
        "search_markets", "get_trending_markets", "filter_markets_by_category",
        "get_featured_markets", "get_closing_soon_markets", "get_sports_markets",
        "get_crypto_markets", "get_market_details", "get_current_price", "get_orderbook",
        "get_spread", "get_market_volume", "get_all_positions", "get_portfolio_summary",
        "get_trade_history"}


def test_the_cron_read_verbs_are_the_rows():
    from cron import read_job as R
    assert {v.replace(".", "_", 1) for v in R.READ_VERBS} == vp.ids_where(read_job=True)
    assert R.READ_VERBS == {
        "defi_data.pool_metrics": ("tools.defi.data_tool", "DefiDataTool", "pool_metrics",
                                   "PoolMetricsParams"),
        "defi_data.price": ("tools.defi.data_tool", "DefiDataTool", "price", "TokenRefParams"),
        "defi_data.token_info": ("tools.defi.data_tool", "DefiDataTool", "token_info",
                                 "TokenRefParams"),
        "defi_data.wallet_holdings": ("tools.defi.data_tool", "DefiDataTool",
                                      "wallet_holdings", "WalletHoldingsParams"),
    }


# -- seat verbs ---------------------------------------------------------------------

def test_the_telegram_room_and_background_sets_are_the_verb_rows():
    from core import verbs as V
    from surfaces.telegram import harness as H
    # The membership of the old Telegram literal, now the rows.
    assert H._ROOM_REFUSED_COMMANDS == {
        "/trade", "/wallet", "/deploy", "/lp", "/launch", "/bridge", "/dev", "/claim",
        "/pay", "/nft", "/dapp", "/identity", "/pause", "/halt", "/resume", "/approve",
        "/reject", "/run", "/allow", "/deny", "/config", "/prefs", "/mcp", "/apps",
        "/invoices", "/settle", "/cron", "/goal", "/fulfill", "/rail", "/contacts",
        "/thread", "/why", "/send", "/swap", "/writeoff", "/unquarantine", "/cards",
        "/adopt", "/check", "/restart", "/update"}
    assert {v.name: v.background for v in V.VERB_TABLE if v.background} == {
        "/bridge": "Bridge", "/send": "Send", "/swap": "Swap", "/lp": "Liquidity move",
        "/claim": "Claim", "/nft": "NFT transaction", "/launch": "Launch",
        "/deploy": "Deploy", "/identity": "Identity registration", "/pay": "Payment"}
    assert not hasattr(H, "_BACKGROUND_MONEY_VERBS")
