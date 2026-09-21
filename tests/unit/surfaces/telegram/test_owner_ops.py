"""Chat write verbs for cron / goals / wallet / invoices (G13).

Each is thin plumbing over the same primitive the `polyrob` CLI calls, so these
tests assert the PLUMBING and the honesty rules — that a short id resolves, that
an ambiguous one is refused rather than guessed, that a tenant can only touch
its own rows, and that a stored-but-inert job says so.
"""
import pytest

from surfaces.telegram import owner_ops


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    return str(tmp_path)


# ---------------------------------------------------------------------------
# short-id resolution — shared by every verb that takes an id
# ---------------------------------------------------------------------------

def test_resolve_prefix_exact_and_unique():
    assert owner_ops.resolve_prefix("abc123", ["abc123", "zzz"]) == ("abc123", None)
    assert owner_ops.resolve_prefix("abc", ["abc123", "zzz"]) == ("abc123", None)


def test_resolve_prefix_refuses_to_guess():
    """These verbs cancel goals and settle invoices — an ambiguous prefix must
    never resolve to whichever row happens to sort first."""
    got, err = owner_ops.resolve_prefix("ab", ["abc1", "abd2"])
    assert got is None and "matches 2 ids" in err
    got, err = owner_ops.resolve_prefix("zz", ["abc1"])
    assert got is None and "no match" in err


# ---------------------------------------------------------------------------
# /cron
# ---------------------------------------------------------------------------

def test_cron_empty_state_teaches_the_syntax(env):
    out = owner_ops.cron_reply("u1", env, [])
    assert "No cron jobs" in out
    assert "/cron add" in out and "|" in out


def test_cron_add_list_show_cancel_round_trip(env, monkeypatch):
    monkeypatch.setenv("CRON_ENABLED", "true")
    out = owner_ops.cron_reply("u1", env, ["add", "every", "monday", "09:00",
                                           "|", "summarise", "the", "week"])
    assert "Scheduled" in out and "next run" in out

    listing = owner_ops.cron_reply("u1", env, [])
    assert "summarise the week" in listing
    assert "every monday 09:00" in listing

    from cron.jobs import CronJobStore
    from core.runtime_paths import cron_db_path
    job = CronJobStore(cron_db_path(env)).list(user_id="u1")[0]

    shown = owner_ops.cron_reply("u1", env, ["show", job.id[:8]])
    assert "summarise the week" in shown

    out = owner_ops.cron_reply("u1", env, ["cancel", job.id[:8]])
    assert "Cancelled" in out
    assert CronJobStore(cron_db_path(env)).list(user_id="u1")[0].status == "cancelled"


def test_cron_add_says_when_no_ticker_will_run_it(env, monkeypatch):
    """A stored job with the ticker off is not a scheduled job — say so."""
    monkeypatch.setenv("CRON_ENABLED", "false")
    out = owner_ops.cron_reply("u1", env, ["add", "30m", "|", "check the treasury"])
    assert "Scheduled" in out
    # D41/D72: the note names what the owner can DO. A flag name is not
    # something he can act on from a phone.
    assert "scheduler is switched off" in out
    assert "polyrob autonomy on" in out


def test_cron_add_rejects_a_bad_schedule(env):
    out = owner_ops.cron_reply("u1", env, ["add", "not-a-schedule", "|", "do it"])
    assert "Invalid schedule" in out


def test_cron_add_without_the_separator_explains(env):
    out = owner_ops.cron_reply("u1", env, ["add", "every", "monday", "do", "it"])
    assert "Usage:" in out and "|" in out


def test_cron_is_tenant_scoped(env, monkeypatch):
    monkeypatch.setenv("CRON_ENABLED", "true")
    owner_ops.cron_reply("u1", env, ["add", "30m", "|", "mine"])
    assert "No cron jobs" in owner_ops.cron_reply("other", env, [])


# ---------------------------------------------------------------------------
# /goal
# ---------------------------------------------------------------------------

def _board(env):
    from agents.task.goals.board import GoalBoard
    from core.runtime_paths import goals_db_path
    return GoalBoard(goals_db_path(env))


def _seed_goal(env, *, user_id="u1", status="ready", title="do the thing"):
    return _board(env).create(user_id=user_id, title=title, status=status).id


def test_goal_pause_and_resume(env):
    goal_id = _seed_goal(env)
    out = owner_ops.goal_reply("u1", env, ["pause", goal_id[:8]])
    assert "→ blocked" in out
    assert _board(env).get(goal_id).status == "blocked"

    out = owner_ops.goal_reply("u1", env, ["resume", goal_id[:8]])
    assert "→ ready" in out
    assert _board(env).get(goal_id).status == "ready"


def test_goal_retry_clears_failures(env):
    goal_id = _seed_goal(env, status="blocked")
    board = _board(env)
    board.record_failure(goal_id, error="boom")
    out = owner_ops.goal_reply("u1", env, ["retry", goal_id[:8]])
    assert "failures cleared" in out
    refreshed = _board(env).get(goal_id)
    assert refreshed.status == "ready" and refreshed.consecutive_failures == 0


def test_goal_refuses_an_illegal_transition(env):
    goal_id = _seed_goal(env, status="ready")
    out = owner_ops.goal_reply("u1", env, ["retry", goal_id[:8]])
    assert "only blocked goals can be retried" in out
    assert _board(env).get(goal_id).status == "ready"


def test_goal_cancel(env):
    goal_id = _seed_goal(env)
    out = owner_ops.goal_reply("u1", env, ["cancel", goal_id[:8]])
    assert "→ cancelled" in out
    assert _board(env).get(goal_id).status == "cancelled"


def test_goal_cannot_touch_another_tenants_goal(env):
    """`board.get()` is not tenant-scoped, so the handler must scope it itself —
    otherwise any chat id could steer any tenant's goal by id."""
    goal_id = _seed_goal(env, user_id="someone-else")
    out = owner_ops.goal_reply("u1", env, ["cancel", goal_id[:8]])
    assert "no match" in out
    assert _board(env).get(goal_id).status == "ready"


def test_goal_show_is_read_only(env):
    goal_id = _seed_goal(env, title="write the report")
    out = owner_ops.goal_reply("u1", env, ["show", goal_id[:8]])
    assert "write the report" in out
    assert _board(env).get(goal_id).status == "ready"


def test_goal_unknown_verb_lists_the_verbs(env):
    out = owner_ops.goal_reply("u1", env, ["explode", "abc"])
    assert "Usage:" in out and "cancel" in out


# ---------------------------------------------------------------------------
# /wallet — read-only by design
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _bound_wallet_owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u1")

def test_wallet_disabled_says_so(monkeypatch):
    """D72: the refusal names what the OWNER can run, not the flag on its own.
    He is usually on a phone, where a flag name is something he can act on only
    by opening a shell."""
    monkeypatch.delenv("AGENT_WALLET_ENABLED", raising=False)
    out = owner_ops.wallet_reply(["balances"], user_id="u1")
    low = out.lower()
    assert ("i have no wallet" in low or "unavailable" in low
            or "not enabled" in low)
    if "i have no wallet" in low:
        assert "polyrob config set" in low


def test_wallet_renders_the_caps_the_gate_will_apply(monkeypatch):
    """D16: the caps shown are the ones the GATE re-resolves per spend, not the
    ones the wallet singleton was CONSTRUCTED with.

    Since 2026-09-18 an owner-approved per-transaction raise applies live to
    spending — and did not appear here, on the one screen he checks to confirm
    it landed.
    """
    class _Cfg:
        network = "testnet"
        max_per_tx_usd = 2.0
        daily_cap_usd = 100.0
        per_venue_daily_cap_usd = None

    class _Wallet:
        config = _Cfg()
        operational_venue = "treasury"
        address = "0xabc"

    monkeypatch.setattr("core.wallet.factory.get_agent_wallet", lambda: _Wallet())
    monkeypatch.setattr("core.wallet.config.effective_max_per_tx_usd",
                        lambda uid, home, env=None: 7.0)
    monkeypatch.setattr("core.wallet.config.effective_daily_cap_usd",
                        lambda uid, home, env=None: 100.0)
    out = owner_ops.wallet_reply(["balances"], user_id="u1")
    assert "$7.00/tx" in out and "$100.00" in out      # the LIVE per-tx value
    assert "$2.00/tx" not in out                       # never the frozen one
    # D17: the cap note names a chat verb, not an SSH session.
    assert "/config set budget.wallet_per_tx_usd" in out
    assert "/etc/polyrob" not in out


def test_wallet_says_so_when_the_live_caps_cannot_be_read(monkeypatch):
    """A cap we could not re-resolve is LABELLED, never printed as if it were
    the live one."""
    class _Cfg:
        network = "testnet"
        max_per_tx_usd = 2.0
        daily_cap_usd = 100.0
        per_venue_daily_cap_usd = None

    class _Wallet:
        config = _Cfg()
        operational_venue = "treasury"
        address = "0xabc"

    def _boom(*a, **k):
        raise RuntimeError("prefs home unreadable")

    monkeypatch.setattr("core.wallet.factory.get_agent_wallet", lambda: _Wallet())
    monkeypatch.setattr("core.wallet.config.effective_max_per_tx_usd", _boom)
    out = owner_ops.wallet_reply(["balances"], user_id="u1")
    assert "$2.00/tx" in out
    assert "could not re-read the live ones" in out


def test_wallet_flags_an_unlimited_daily_cap(monkeypatch):
    class _Cfg:
        network = "mainnet"
        max_per_tx_usd = 2.0
        daily_cap_usd = None
        per_venue_daily_cap_usd = None

    class _Wallet:
        config = _Cfg()
        operational_venue = "treasury"
        address = "0xabc"

    monkeypatch.setattr("core.wallet.factory.get_agent_wallet", lambda: _Wallet())
    monkeypatch.setattr("core.wallet.config.effective_max_per_tx_usd",
                        lambda uid, home, env=None: 2.0)
    monkeypatch.setattr("core.wallet.config.effective_daily_cap_usd",
                        lambda uid, home, env=None: None)
    out = owner_ops.wallet_reply(["balances"], user_id="u1")
    assert "UNLIMITED" in out and "catastrophic-loss ceiling" in out


def test_wallet_skips_network_reads_unless_asked(monkeypatch):
    """A balance probe is a network read — not something a status glance pays."""
    probes = []

    class _Cfg:
        network = "mainnet"
        max_per_tx_usd = 2.0
        daily_cap_usd = 50.0
        per_venue_daily_cap_usd = None

    class _Wallet:
        config = _Cfg()
        operational_venue = "treasury"
        address = "0xabc"

        def signer_for(self, venue):
            probes.append(venue)
            return type("S", (), {"address": "0xdef"})()

    monkeypatch.setattr("core.wallet.factory.get_agent_wallet", lambda: _Wallet())
    monkeypatch.setattr("core.wallet.onchain.balances", lambda a, c, **k: (1.0, 2.0))
    owner_ops.wallet_reply([], user_id="u1")
    assert probes == []
    owner_ops.wallet_reply(["balances"], user_id="u1")
    assert probes == ["treasury", "x402"]


# ---------------------------------------------------------------------------
# /invoices and /settle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invoices_empty_names_the_tenant_it_searched(monkeypatch):
    """Sibling money views default to different tenant scopes — an empty listing
    must show WHICH bucket it read, or a scope mismatch reads as 'no invoices'."""
    async def _none(**kwargs):
        return []

    monkeypatch.setattr("modules.x402.invoicing.list_payment_requests", _none)
    out = await owner_ops.invoices_reply("alice", [])
    assert "No invoices" in out and "alice" in out


@pytest.mark.asyncio
async def test_invoices_lists_and_caps(monkeypatch):
    rows = [{"request_id": f"inv-{i:03d}", "amount_usd": 12.5, "status": "pending",
             "purpose": f"job {i}", "payer_contact": "acme@example.com"}
            for i in range(25)]

    async def _rows(**kwargs):
        return rows

    monkeypatch.setattr("modules.x402.invoicing.list_payment_requests", _rows)
    out = await owner_ops.invoices_reply("alice", [])
    assert "25 invoice(s)" in out
    assert out.count("• ") == owner_ops._LIST_LIMIT
    assert "+15 more" in out
    assert "billed to acme@example.com" in out


@pytest.mark.asyncio
async def test_invoices_rejects_an_unknown_status(monkeypatch):
    out = await owner_ops.invoices_reply("alice", ["paid"])
    assert "Usage:" in out


@pytest.mark.asyncio
async def test_settle_resolves_a_short_id_and_settles(monkeypatch):
    settled = {}

    async def _rows(**kwargs):
        assert kwargs.get("status") == "pending"
        return [{"request_id": "inv-abc123", "amount_usd": 5.0, "status": "pending"}]

    async def _settle(request_id, *, transaction_hash=None, db=None):
        settled["id"] = request_id
        settled["tx"] = transaction_hash
        return True

    monkeypatch.setattr("modules.x402.invoicing.list_payment_requests", _rows)
    monkeypatch.setattr("modules.x402.invoicing.settle_payment_request", _settle)
    out = await owner_ops.settle_reply("alice", ["inv-abc", "0xdeadbeef"])
    assert "Settled" in out
    assert settled == {"id": "inv-abc123", "tx": "0xdeadbeef"}


@pytest.mark.asyncio
async def test_settle_only_sees_this_tenants_pending_rows(monkeypatch):
    async def _rows(**kwargs):
        return []

    monkeypatch.setattr("modules.x402.invoicing.list_payment_requests", _rows)
    out = await owner_ops.settle_reply("alice", ["inv-abc"])
    assert "no match" in out and "PENDING" in out


@pytest.mark.asyncio
async def test_settle_reports_a_refused_transition(monkeypatch):
    async def _rows(**kwargs):
        return [{"request_id": "inv-1", "amount_usd": 5.0, "status": "pending"}]

    async def _settle(request_id, *, transaction_hash=None, db=None):
        return False

    monkeypatch.setattr("modules.x402.invoicing.list_payment_requests", _rows)
    monkeypatch.setattr("modules.x402.invoicing.settle_payment_request", _settle)
    out = await owner_ops.settle_reply("alice", ["inv-1"])
    assert "not settled" in out
