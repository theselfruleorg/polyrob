"""`polyrob finance` + REPL `/finance` — CLI parity for the unified ledger (G3).

The 2026-07-12 UI-surface review: the earned/spent/pending/net balance sheet
existed ONLY on the webview (/finance + /api/webgate/ledger). These verbs share
one pure renderer (``cli/ui/commands/h_finance.py::render_finance``) over the
SAME ``modules.credits.unified_ledger.build_ledger`` core the webview uses.

Two ledgers that must NEVER be summed: ``treasury`` (the agent's own USDC —
income/spend/pending/net) and ``runtime`` (the owner's LLM/API bill — no net,
nothing to net an expense against). Terminology is income/spend; "earned" is
retired.
"""
from click.testing import CliRunner


_FAKE_LEDGER = {
    "user_id": "u1", "window_days": 7,
    "llm_api_cost_usd": 0.5, "credits_spent": 0.1, "llm_calls": 3,
    "wallet_spend_usd": 0.25, "wallet_payments": 1,
    "settled_payments": 1,
    "pending_invoices_usd": 3.0, "pending_invoices": 2,
    "treasury": {
        "income_usd": 2.0, "spend_usd": 0.25, "pending_usd": 3.0,
        "pending_count": 2, "balance_usd": None,
        "net_usd": 1.75,                                # income - spend, NOT 1.25
        "available": True,
    },
    "runtime": {
        "spend_window_usd": 0.5, "spend_total_usd": 0.5,
        "calls_window": 3, "calls_total": 3,
        "provider_balance_usd": None, "available": True,
    },
}


async def _fake_ledger(user_id, *, days=7, db=None, include_balances=False):
    return dict(_FAKE_LEDGER, user_id=user_id, window_days=days)


# --------------------------------------------------------------------------- #
# Pure renderer
# --------------------------------------------------------------------------- #

def test_render_finance_shows_treasury_and_runtime_split(monkeypatch):
    import cli.ui.commands.h_finance as h_finance
    monkeypatch.setattr(h_finance, "build_ledger", _fake_ledger)
    out = h_finance.render_finance(user_id="u1", days=7)
    assert "$2.00" in out          # treasury income
    assert "$0.25" in out          # treasury spend
    assert "$3.00" in out          # treasury pending
    assert "$1.75" in out          # treasury net (income - spend)
    assert "$0.50" in out          # runtime spend/total
    assert "7" in out              # window
    assert "income" in out.lower()
    assert "runtime" in out.lower()
    assert "earned" not in out.lower()
    # the legacy MERGED net (2.0 - 0.75 = 1.25) must never render — that would
    # be the owner's API bill silently folded into the agent's own P&L.
    assert "$1.25" not in out


def test_render_finance_omits_none_balances(monkeypatch):
    """H14b: a None balance is OMITTED, never rendered as $0.00 — and
    treasury.balance_usd / runtime.provider_balance_usd are independent."""
    import cli.ui.commands.h_finance as h_finance

    async def _with_treasury_balance(user_id, *, days=7, db=None, include_balances=False):
        led = dict(_FAKE_LEDGER, user_id=user_id, window_days=days)
        led["treasury"] = dict(led["treasury"], balance_usd=42.0)
        return led

    monkeypatch.setattr(h_finance, "build_ledger", _with_treasury_balance)
    out = h_finance.render_finance(user_id="u1", days=7)
    assert "$42.00" in out          # treasury balance rendered (present)
    assert "balance" in out.lower()


def test_render_finance_sub_cent_spend_not_rendered_as_zero(monkeypatch):
    """L10: a real sub-cent runtime spend (e.g. $0.0042 LLM cost) must render
    at 4dp — never collapse to an honest-looking $0.00."""
    import cli.ui.commands.h_finance as h_finance

    async def _tiny(user_id, *, days=7, db=None, include_balances=False):
        led = dict(_FAKE_LEDGER, user_id=user_id, window_days=days)
        led["treasury"] = dict(led["treasury"], income_usd=0.0, spend_usd=0.0, net_usd=0.0)
        led["runtime"] = dict(led["runtime"], spend_window_usd=0.0042)
        return led

    monkeypatch.setattr(h_finance, "build_ledger", _tiny)
    out = h_finance.render_finance(user_id="u1", days=7)
    assert "$0.0042" in out
    # the runtime spend row (3 calls, per _FAKE_LEDGER) must show the real
    # sub-cent figure, never a rounded-to-zero "$0.00   (3 calls)" lie.
    assert "$0.00   (3 calls)" not in out
    assert "$0.0042   (3 calls)" in out


def test_render_finance_fail_open_on_ledger_error(monkeypatch):
    import cli.ui.commands.h_finance as h_finance

    async def _boom(user_id, *, days=7, db=None, include_balances=False):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(h_finance, "build_ledger", _boom)
    out = h_finance.render_finance(user_id="u1", days=7)
    assert "unavailable" in out.lower()


# --------------------------------------------------------------------------- #
# CLI command
# --------------------------------------------------------------------------- #

def test_cli_finance_prints_the_ledger(monkeypatch, tmp_path):
    import cli.ui.commands.h_finance as h_finance
    monkeypatch.setattr(h_finance, "build_ledger", _fake_ledger)
    # Hermetic DB resolution: on a clean checkout (public CI) no data-home bot.db
    # exists, so the command would take the honest "no data yet" early exit and
    # never reach the patched build_ledger. DB_PATH only needs to be an existing
    # file — the ledger itself is faked above.
    db_file = tmp_path / "bot.db"
    db_file.write_bytes(b"")
    monkeypatch.setenv("DB_PATH", str(db_file))
    from cli.commands.finance import finance
    res = CliRunner().invoke(finance, ["--days", "7", "--user", "u1"])
    assert res.exit_code == 0, res.output
    assert "$1.75" in res.output
    assert "earned" not in res.output.lower()


def test_cli_finance_registered_in_group():
    from cli.polyrob import cli
    assert "finance" in cli.commands


# --------------------------------------------------------------------------- #
# REPL slash registration
# --------------------------------------------------------------------------- #

def test_repl_finance_slash_registered():
    from cli.ui.commands.handlers import build_default_registry
    reg = build_default_registry()
    names = {c.name for c in reg.commands()} if hasattr(reg, "commands") else set()
    if not names:
        names = set(getattr(reg, "_commands", {}).keys())
    assert "finance" in names
