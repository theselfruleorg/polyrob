"""Revalidation of the 2026-09-21 interface audit's CLI + REPL commits.

`e4376c96d` (CLI) and `b841b11bb` (REPL) moved every owner verb onto the
deployed-home seam and added the claim/nft/dapp/identity/contacts seats. These
tests pin the five defects the revalidation pass found in the DEPENDENT
components those commits left behind — each one a remedy or a record that named
something other than what the code now does.
"""
import os
import types


# ---------------------------------------------------------------------------
# 1. The gate manager's own usage lines name /gates, not the retired grammar
# ---------------------------------------------------------------------------


def _gate_ctx(tmp_path):
    from cli.ui.commands.h_approve import ApproveCtx
    return ApproveCtx(user_id="local", home_dir=str(tmp_path))


def test_gate_manager_usage_names_gates_not_approve(tmp_path):
    """C5 moved gate management to ``/gates`` and left ``h_approve``'s own
    error and usage strings naming ``/approve list | add | remove`` — the
    exact "the remedy names a command this seat does not run" defect C5
    existed to remove. ``/gates bogus`` answered *unknown /approve
    subcommand*."""
    from cli.ui.commands.h_approve import cmd_approve
    ctx = _gate_ctx(tmp_path)
    for args, must in ((["bogus"], "/gates"), (["add"], "/gates add"),
                       (["remove"], "/gates remove")):
        out = cmd_approve(ctx, args)
        assert must in out, (args, out)
        assert "/approve" not in out, (args, out)


def test_approve_alias_still_runs_the_gate_manager(tmp_path):
    """The deprecated alias must keep WORKING — an owner with the old grammar
    in muscle memory is answered, and told where it moved."""
    import io

    from cli.ui.commands.registry import CommandContext
    from cli.ui.commands import h_gates
    from cli.ui.plain_renderer import PlainRenderer
    from cli.ui.state import SessionState

    buf = io.StringIO()
    state = SessionState()
    ctx = CommandContext(renderer=PlainRenderer(state=state, stream=buf),
                         state=state, user_id="local", args=["list"])
    h_gates._HOME_RESOLVER = lambda _c: str(tmp_path)
    try:
        h_gates.h_approve(ctx)
    finally:
        h_gates._HOME_RESOLVER = None
    out = buf.getvalue()
    assert "gate management moved to `/gates`" in out


# ---------------------------------------------------------------------------
# 2. `cron prune --dry-run` counts exactly what the prune deletes
# ---------------------------------------------------------------------------


def _insert(db, job_id, created, status="cancelled", user="local"):
    import sqlite3
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO cron_jobs (id,task,schedule_spec,user_id,next_run_at,one_shot,"
        "skip_memory,max_duration_seconds,payload,enabled,status,last_run_at,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, "t", "30m", user, "2026-01-01T00:00:00", 0, 0, 600, "{}", 0,
         status, None, created))
    con.commit()
    con.close()


def test_prune_dry_run_count_equals_the_real_prune(tmp_path):
    """C54: the preview and the delete were two WHERE clauses built from two
    clocks. They are one now, so a row on the boundary — and a row whose
    ``created_at`` is blank — cannot be listed and then survive."""
    from datetime import datetime, timedelta

    from cron.jobs import CronJobStore
    db = str(tmp_path / "cron.db")
    store = CronJobStore(db)
    old = (datetime.now() - timedelta(days=40)).isoformat()
    _insert(db, "a", old)
    _insert(db, "b", old)
    _insert(db, "c", datetime.now().isoformat())        # too new
    _insert(db, "d", "")                                # no recorded time
    _insert(db, "e", old, status="scheduled")           # not cancelled
    _insert(db, "f", old, user="someone_else")          # other tenant

    dry = store.prune_cancelled(older_than_days=30, user_id="local", dry_run=True)
    assert dry > 0
    real = store.prune_cancelled(older_than_days=30, user_id="local")
    assert dry == real, "the preview and the prune disagree"

    import sqlite3
    left = sorted(r[0] for r in sqlite3.connect(db).execute("SELECT id FROM cron_jobs"))
    assert left == ["c", "e", "f"]


def test_prune_dry_run_deletes_nothing(tmp_path):
    from datetime import datetime, timedelta

    from cron.jobs import CronJobStore
    db = str(tmp_path / "cron.db")
    store = CronJobStore(db)
    _insert(db, "a", (datetime.now() - timedelta(days=40)).isoformat())
    assert store.prune_cancelled(older_than_days=30, user_id="local", dry_run=True) == 1
    import sqlite3
    assert [r[0] for r in sqlite3.connect(db).execute("SELECT id FROM cron_jobs")] == ["a"]


# ---------------------------------------------------------------------------
# 3. The REPL's /apps records the seat it was run from
# ---------------------------------------------------------------------------


def test_repl_apps_records_the_repl_as_the_seat(monkeypatch, tmp_path):
    """``via`` is the ONE audit field whose job is to say WHERE an owner
    decision was made. E5 gave the REPL the full ``/apps`` verb set but kept
    ``apps_reply``'s ``via="telegram"`` default, so a terminal approve/kill was
    recorded as having come from the phone."""
    import io

    from cli.ui.commands import handlers
    from cli.ui.commands.registry import CommandContext
    from cli.ui.plain_renderer import PlainRenderer
    from cli.ui.state import SessionState

    seen = {}

    def _fake_apps_reply(user_id, data_dir, args, via="telegram"):
        seen["via"] = via
        seen["args"] = list(args)
        return "ok"

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    import surfaces.telegram.apps_ops as apps_ops
    monkeypatch.setattr(apps_ops, "apps_reply", _fake_apps_reply)

    buf = io.StringIO()
    state = SessionState()
    ctx = CommandContext(renderer=PlainRenderer(state=state, stream=buf),
                         state=state, user_id="local", args=["list"])
    handlers._h_apps(ctx)
    assert seen["via"] == "repl"


def test_repl_apps_declares_a_write_for_a_deciding_subcommand(monkeypatch, tmp_path):
    """057 WS-G: ``approve``/``reject``/``kill`` MUTATE the shared home, so the
    euid seam must be told — a root run has to refuse, not merely warn."""
    import io

    from cli.ui.commands import handlers
    from cli.ui.commands.registry import CommandContext
    from cli.ui.plain_renderer import PlainRenderer
    from cli.ui.state import SessionState

    writes = []
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    import surfaces.telegram.apps_ops as apps_ops
    monkeypatch.setattr(apps_ops, "apps_reply",
                        lambda *a, **k: "ok")
    import cli._admin_home as admin_home
    real = admin_home.admin_data_dir
    monkeypatch.setattr(admin_home, "admin_data_dir",
                        lambda *, write=None: (writes.append(write), real(write=write))[1])

    def _run(args):
        buf = io.StringIO()
        state = SessionState()
        handlers._h_apps(CommandContext(
            renderer=PlainRenderer(state=state, stream=buf), state=state,
            user_id="local", args=args))

    _run(["list"])
    _run(["approve", "slug"])
    assert writes == [False, True]


# ---------------------------------------------------------------------------
# 4. A dapp-bridge READ never materialises the session store
# ---------------------------------------------------------------------------


def test_durably_revoked_does_not_create_the_store(monkeypatch, tmp_path):
    """The cross-process revoke check runs on EVERY page request. It called
    ``get_dapp_session_store()``, which runs ``init_schema(..., mkdir=True)``
    — so simply asking "was I revoked?" created the db. An arming WRITES the
    row first, so an absent file is "nothing was ever armed here"."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from core.dapp_session_store import default_dapp_session_store_path
    from tools.dapp_browser.bridge import WalletBridge

    db = default_dapp_session_store_path()
    assert not os.path.exists(db)

    fake = types.SimpleNamespace(
        _ctx=types.SimpleNamespace(session_id="sess-1"),
        envelope=types.SimpleNamespace(revoked=False))
    assert WalletBridge._durably_revoked(fake) is False
    assert not os.path.exists(db), f"a READ created {db}"


def test_durably_revoked_reads_a_real_revocation(monkeypatch, tmp_path):
    """The guard must still SEE an owner's out-of-process revoke once the
    store exists — the exists-check may not turn the backstop off."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from core.dapp_session_store import (
        default_dapp_session_store_path, get_dapp_session_store,
    )
    import core.dapp_session_store as store_mod
    from tools.dapp_browser.bridge import WalletBridge

    store_mod._INSTANCES.clear()
    store = get_dapp_session_store(default_dapp_session_store_path())
    store.save("sess-1", "local", {"chain": "base"})
    store.mark_revoked("sess-1")

    fake = types.SimpleNamespace(
        _ctx=types.SimpleNamespace(session_id="sess-1"),
        envelope=types.SimpleNamespace(revoked=False))
    assert WalletBridge._durably_revoked(fake) is True
    assert fake.envelope.revoked is True
    store_mod._INSTANCES.clear()


# ---------------------------------------------------------------------------
# 5. `goals tree` truncation names a remedy that is actually reachable
# ---------------------------------------------------------------------------


def test_goals_tree_truncation_does_not_promise_the_full_set():
    """The note said "use `goals list --json` for the full set"; that verb is
    capped at 500 rows too, so the remedy was a promise no seat keeps."""
    import inspect

    from cli.commands import goals
    src = inspect.getsource(goals.goals_tree.callback)
    assert "for the full set" not in src
    assert "--status" in src, "the truncation must name a way to NARROW"


def test_goals_tree_never_answers_with_a_blank(tmp_path, monkeypatch, capsys):
    """An empty board printed NOTHING at all, which reads identically to "the
    command did nothing". Every other board view on every other seat answers
    with the ONE empty grammar."""
    import click.testing

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.commands.goals import goals
    res = click.testing.CliRunner().invoke(goals, ["tree"])
    assert res.exit_code == 0, res.output
    assert res.output.strip(), "`goals tree` answered with a blank"
    assert "no objectives or goals" in res.output


# ---------------------------------------------------------------------------
# 6. Every remedy the REPL prints names something the REPL can actually run
# ---------------------------------------------------------------------------


def test_goals_view_empty_remedy_is_a_verb_that_exists(monkeypatch, tmp_path):
    """``/goals`` on an empty board said "create one with `/goal create`".
    ``/goal`` STEERS an existing goal (show|ready|pause|…) — there is no
    ``create`` subcommand — so the remedy handed the owner a usage line."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from cli.ui.commands.h_goals_view import goals_view
    out = goals_view("local")
    assert "/goal create" not in out
    # Whatever it names must be reachable from this seat.
    from cli.ui.commands import build_default_registry
    reg = build_default_registry()
    import re
    for slash in re.findall(r"`/([a-z-]+)", out):
        assert reg.lookup(slash) is not None, f"remedy names /{slash}, which does not exist"


def test_goals_view_window_note_does_not_promise_every_row():
    """`polyrob goals list` is itself windowed (``-n``, max 500), so "shows
    them all" was a promise the named verb does not keep."""
    import inspect

    from cli.ui.commands import h_goals_view
    src = inspect.getsource(h_goals_view.goals_view)
    assert "shows them all" not in src


# ---------------------------------------------------------------------------
# 7. A money verb SPENDS under the same tenant this box's ledger READS
# ---------------------------------------------------------------------------


def test_owner_money_context_uses_the_deployed_tenant(monkeypatch):
    """Every money VIEW on this seat resolves ``admin_owner_principal()`` (which
    reads the deployment's own declaration), while every money ACTION built its
    context from the bare ``resolve_owner_user_id()`` — the shell's answer. On a
    deployed box systemd exports ``POLYROB_OWNER_USER_ID`` and an owner's SSH
    shell does not, so `wallet claim|launch|deploy|nft transfer|bridge`,
    `identity register` and `wallet lp add` would RECORD the spend under one
    tenant while the caps, `wallet book` and `finance` read another — the 033
    tenantless-``wallet_spend`` defect in a new place."""
    import cli.commands.wallet as wallet

    monkeypatch.setattr(wallet, "_admin_tenant", lambda user_id=None: "deployed-owner")
    assert wallet._owner_ctx().user_id == "deployed-owner"

    from cli.commands import identity
    assert identity._owner_ctx().user_id == "deployed-owner"


def test_no_money_seat_resolves_the_shell_tenant_directly():
    """A second copy of the rule is how the two drifted the first time."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    offenders = []
    for name in ("wallet.py", "wallet_lp.py", "identity.py"):
        src = (root / "cli" / "commands" / name).read_text(encoding="utf-8")
        for i, line in enumerate(src.splitlines(), 1):
            if "resolve_owner_user_id()" in line and not line.strip().startswith("#"):
                offenders.append(f"cli/commands/{name}:{i}")
    assert not offenders, ("a money seat resolves the SHELL tenant instead of "
                           f"`_admin_tenant`: {offenders}")


# ---------------------------------------------------------------------------
# 8. The published REPL reference names verbs this REPL actually has
# ---------------------------------------------------------------------------


def test_cli_guide_names_only_real_repl_verbs():
    """``docs/guide/cli.md`` is the published command reference. It still
    carried ``/approve [list|add|remove]`` under **control** with "configures
    approval policy; it does not decide a pending request" — the exact
    semantics C5 retired — and listed ``/identity`` as an alias of ``/self``
    after ``/identity`` became the ERC-8004 verb. A reference that teaches a
    retired grammar is a dependent component of the change that retired it."""
    import re
    from pathlib import Path

    from cli.ui.commands import build_default_registry
    reg = build_default_registry()
    root = Path(__file__).resolve().parents[3]
    text = (root / "docs" / "guide" / "cli.md").read_text(encoding="utf-8")
    # Only the REPL tables use a leading `/verb` inside a backticked cell.
    named = {m for m in re.findall(r"\|\s*`/([a-z][a-z0-9-]*)", text)}
    missing = sorted(v for v in named if reg.lookup(v) is None)
    assert not missing, f"cli.md documents REPL verbs that do not exist: {missing}"


def test_cli_guide_does_not_teach_the_retired_approve_semantics():
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    text = (root / "docs" / "guide" / "cli.md").read_text(encoding="utf-8")
    assert "it does not decide a pending" not in text
    assert "/gates" in text, "the gate manager's own name must be documented"


# ---------------------------------------------------------------------------
# 9. The invoice vocabulary and the refund liability reach the terminal
# ---------------------------------------------------------------------------


def test_repl_invoices_accepts_every_ssot_status():
    """``h_owner`` carried its own ``("pending","completed","expired")``, so
    ``/invoices refund_due`` (money taken and nothing delivered) and
    ``/invoices settling`` answered *usage* — the owner could not ask for the
    two states that most need asking about."""
    from modules.x402.invoicing import INVOICE_STATUSES
    from cli.ui.commands.h_owner import _invoice_statuses
    assert tuple(_invoice_statuses()) == tuple(INVOICE_STATUSES)
    assert "refund_due" in _invoice_statuses()


def _ledger(refund_count, refund_usd=0.5):
    return {
        "window_days": 7, "user_id": "local", "settled_payments": 3,
        "treasury": {"available": True, "income_usd": 12.0, "spend_usd": 2.0,
                     "pending_usd": 1.0, "pending_count": 2, "net_usd": 10.0,
                     "balance_usd": None, "refund_due_usd": refund_usd,
                     "refund_due_count": refund_count},
        "runtime": {"available": True, "spend_window_usd": 1.0,
                    "spend_total_usd": 5.0, "calls_window": 3, "calls_total": 9,
                    "provider_balance_usd": None},
        "caps": {},
    }


def test_finance_names_a_refund_we_owe():
    """A settled payment we did not deliver on is a LIABILITY: it is excluded
    from ``income`` by ``INCOME_STATUSES``, so without its own line the balance
    sheet simply does not mention money we took."""
    from cli.ui.commands.h_finance import render_finance_text
    out = render_finance_text(_ledger(2), days=7, user_id="local")
    assert "refund owed" in out and "$0.50" in out and "2 settled payment(s)" in out


def test_finance_prints_no_refund_row_when_none_is_owed():
    """A permanent "$0.00 owed" row trains the eye to skip the line that
    matters."""
    from cli.ui.commands.h_finance import render_finance_text
    assert "refund owed" not in render_finance_text(_ledger(0, 0.0), days=7,
                                                    user_id="local")


def test_finance_refund_row_does_not_repad_the_rows_above_it():
    """``candy.kv_lines`` pads to the longest label in ONE call, so folding a
    12-character label into ``treasury_rows`` would shift every value column
    above it. The refund line gets its own scope, exactly as `today's cap`
    does."""
    from cli.ui.commands.h_finance import render_finance_text
    with_refund = render_finance_text(_ledger(2), days=7, user_id="local")
    without = render_finance_text(_ledger(0, 0.0), days=7, user_id="local")
    for row in ("income", "spend", "pending", "net"):
        a = next(l for l in with_refund.splitlines() if l.strip().startswith(row))
        b = next(l for l in without.splitlines() if l.strip().startswith(row))
        assert a == b, f"the refund line re-padded the {row!r} row"


def test_journey_does_not_drop_a_refund_owed(monkeypatch, tmp_path):
    """``h_journey`` renders four of the kinds ``build_recap`` produces and
    drops the rest. ``payment_refund_due`` is an OWNER-NOTIFIED kind — a window
    that contains one may not read as if it did not."""
    from core.event_kinds import PAYMENT_REFUND_DUE
    from core import recap
    from cli.ui.commands import h_journey

    entry = recap.RecapEntry(ts=1.0, kind=PAYMENT_REFUND_DUE, text="refund")
    monkeypatch.setattr(recap, "build_recap", lambda *a, **k: [entry])
    out = h_journey.render_journey(user_id="local", since_label="7d",
                                   data_dir=str(tmp_path))
    assert "refund owed" in out


def test_telemetry_counts_every_kind(tmp_path, monkeypatch):
    """``/telemetry`` must pass NO kind filter — a new owner-notified kind has
    to appear the day it is first emitted, with no seat to update."""
    import inspect

    from cli.ui.commands import handlers
    src = inspect.getsource(handlers._h_telemetry)
    assert "kind=" not in src, "/telemetry filters the event log by kind"
