"""A6 (043 A39): the REPL/CLI finance renderer must honour `available`.

Before this fix, `render_finance` unconditionally computed real numbers from
`treasury`/`runtime` fields even when the ledger had ALREADY marked that leg
unreadable (`available: False`) — a metering-off tenant saw eight confident
`$0.00`s where the webview Finance page renders `—` (finance.html's
`moneyIf`). `render_finance_text` is the pure renderer extracted from
`render_finance` so this can be tested without the async `build_ledger`
round trip; both CLI `polyrob finance` and REPL `/finance` share it via
`render_finance`.

Also covers the "today's cap" headroom line (`ledger["caps"]`, built by
`unified_ledger.build_ledger` from the wallet `PolicyGate`).
"""
from cli.ui.commands.h_finance import render_finance_text

_UNAVAILABLE_REASON = "not readable — metering is off or the store is missing"


def test_repl_finance_renders_dash_when_unavailable():
    ledger = {"treasury": {"available": False, "income_usd": 0.0, "spend_usd": 0.0,
              "pending_usd": 0.0, "net_usd": 0.0, "pending_count": 0},
              "runtime": {"available": True, "spend_window_usd": 1.5, "spend_total_usd": 9.0,
              "calls_window": 3, "calls_total": 30}, "settled_payments": 0}
    out = render_finance_text(ledger, days=7, user_id="u")
    assert "$0.00" not in out.split("Runtime")[0] and "—" in out


def test_repl_finance_treasury_unavailable_shows_reason():
    """The unreadable block must name WHY, not just dash out silently."""
    ledger = {"treasury": {"available": False, "income_usd": 0.0, "spend_usd": 0.0,
              "pending_usd": 0.0, "net_usd": 0.0, "pending_count": 0},
              "runtime": {"available": True, "spend_window_usd": 1.5, "spend_total_usd": 9.0,
              "calls_window": 3, "calls_total": 30}, "settled_payments": 0}
    out = render_finance_text(ledger, days=7, user_id="u")
    assert "not readable — metering is off or the store is missing" in out


def test_repl_finance_runtime_unavailable_shows_dash_and_reason():
    """The two blocks are independent: treasury can be healthy while runtime
    (LLM/API cost metering) is the one that's broken."""
    ledger = {"treasury": {"available": True, "income_usd": 2.0, "spend_usd": 0.25,
              "pending_usd": 0.0, "pending_count": 0, "net_usd": 1.75, "balance_usd": None},
              "runtime": {"available": False, "spend_window_usd": 0.0, "spend_total_usd": 0.0,
              "calls_window": 0, "calls_total": 0, "provider_balance_usd": None},
              "settled_payments": 1, "window_days": 7}
    out = render_finance_text(ledger, days=7, user_id="u")
    runtime_section = out.split("Runtime cost")[1]
    assert "$0.00" not in runtime_section
    assert "not readable — metering is off or the store is missing" in runtime_section
    # treasury is healthy — its real numbers still render.
    assert "$2.00" in out
    assert "$1.75" in out


def test_repl_finance_available_true_renders_real_numbers():
    """Both blocks available: real numbers render, no dash in the money rows."""
    ledger = {"treasury": {"available": True, "income_usd": 2.0, "spend_usd": 0.25,
              "pending_usd": 3.0, "pending_count": 2, "net_usd": 1.75, "balance_usd": None},
              "runtime": {"available": True, "spend_window_usd": 0.5, "spend_total_usd": 0.5,
              "calls_window": 3, "calls_total": 3, "provider_balance_usd": None},
              "settled_payments": 1, "window_days": 7}
    out = render_finance_text(ledger, days=7, user_id="u1")
    assert "$2.00" in out
    assert "$0.25" in out
    assert "$3.00" in out
    assert "$1.75" in out
    assert "$0.50" in out
    assert _UNAVAILABLE_REASON not in out


def test_repl_finance_cap_headroom_line_renders_from_ledger_caps():
    """A39: `today's cap` shows `$used of $cap ($left left)` from `ledger["caps"]`
    — the SAME PolicyGate read `unified_ledger.build_ledger` already computed."""
    ledger = {"treasury": {"available": True, "income_usd": 0.0, "spend_usd": 0.0,
              "pending_usd": 0.0, "pending_count": 0, "net_usd": 0.0, "balance_usd": None},
              "runtime": {"available": True, "spend_window_usd": 0.0, "spend_total_usd": 0.0,
              "calls_window": 0, "calls_total": 0, "provider_balance_usd": None},
              "settled_payments": 0, "window_days": 7,
              "caps": {"daily_cap_usd": 100.0, "daily_used_usd": 20.0,
                       "daily_left_usd": 80.0, "per_tx_cap_usd": 25.0}}
    out = render_finance_text(ledger, days=7, user_id="u1")
    assert "$20.00 of $100.00 ($80.00 left)" in out


def test_repl_finance_cap_headroom_unavailable_when_caps_missing():
    ledger = {"treasury": {"available": True, "income_usd": 0.0, "spend_usd": 0.0,
              "pending_usd": 0.0, "pending_count": 0, "net_usd": 0.0, "balance_usd": None},
              "runtime": {"available": True, "spend_window_usd": 0.0, "spend_total_usd": 0.0,
              "calls_window": 0, "calls_total": 0, "provider_balance_usd": None},
              "settled_payments": 0, "window_days": 7}
    out = render_finance_text(ledger, days=7, user_id="u1")
    assert "cap headroom unavailable (wallet not readable)" in out


def test_repl_finance_treasury_row_alignment_byte_identical_to_before_cap_line():
    """Fix round 1 (Important 1): appending the 'today's cap' row into the
    SAME kv_lines() call as income/spend/pending/net used to widen the
    alignment column for every pre-existing row (kv_lines pads to the
    longest label in one call — "today's cap" at 12 chars vs "pending" at
    7). The cap line must render through its OWN kv_lines call so the old
    rows are byte-identical to the pre-caps rendering. Pin the EXACT
    original spacing for one row."""
    ledger = {"treasury": {"available": True, "income_usd": 2.0, "spend_usd": 0.25,
              "pending_usd": 3.0, "pending_count": 2, "net_usd": 1.75, "balance_usd": None},
              "runtime": {"available": True, "spend_window_usd": 0.5, "spend_total_usd": 0.5,
              "calls_window": 3, "calls_total": 3, "provider_balance_usd": None},
              "settled_payments": 1, "window_days": 7,
              "caps": {"daily_cap_usd": 100.0, "daily_used_usd": 20.0,
                       "daily_left_usd": 80.0, "per_tx_cap_usd": 25.0}}
    out = render_finance_text(ledger, days=7, user_id="u1")
    assert "  income   $2.00   (1 settled)" in out
    assert "  spend    $0.25" in out
    assert "  pending  $3.00   (2 open invoices)" in out
    assert "  net      $1.75" in out
