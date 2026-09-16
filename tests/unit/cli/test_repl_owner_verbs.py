"""REPL owner control-plane verbs (proposal 030 WS-C C2, finding G1).

The local REPL was the only interactive owner seat without the kill switch and
the money/admin verbs. These tests pin the new slash commands — /halt /resume
/asks /fulfill /allow /deny /allowlist /invoices /settle — as thin renderers
over the SAME core primitives `polyrob owner …` and telegram's owner admin
call (core.surfaces.owner_admin, the goal-board asks store, the outbound
allowlist, modules.x402.invoicing).

Harness pattern mirrors tests/unit/cli/test_pending_command.py: a recorder
renderer + a partial CommandContext, handlers invoked directly.
"""
from core.autonomy_control import PAUSE_FILENAME as _PAUSE_FILENAME  # 031: the one record
import asyncio

import pytest


class _Recorder:
    def __init__(self):
        self.lines = []

    def print_block(self, text, *, title="", style=""):
        self.lines.append(text)

    @property
    def text(self):
        return "\n".join(self.lines)


def _ctx(args=(), user_id="local", container=None):
    from cli.ui.commands.registry import CommandContext

    rec = _Recorder()
    ctx = CommandContext(
        renderer=rec,
        container=container,
        user_id=user_id,
        args=list(args),
    )
    return ctx, rec


NEW_VERBS = ("halt", "resume", "asks", "fulfill", "allow", "deny",
             "allowlist", "invoices", "settle", "missed")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_every_owner_verb_registered_with_help():
    from cli.ui.commands.handlers import build_default_registry

    reg = build_default_registry()
    for name in NEW_VERBS:
        cmd = reg.lookup(name)
        assert cmd is not None, f"/{name} not registered"
        assert cmd.name == name, f"/{name} resolves to /{cmd.name}, not itself"
        assert (cmd.help or "").strip(), f"/{name} has no help text"


def test_resume_is_no_longer_a_replay_alias():
    """030 WS-C C2: /resume moved to the kill-switch resume; /replay keeps the
    feed replay under its canonical name."""
    from cli.ui.commands.handlers import build_default_registry

    reg = build_default_registry()
    assert reg.lookup("resume").name == "resume"
    assert reg.lookup("replay").name == "replay"


# ---------------------------------------------------------------------------
# /halt and /resume — kill switch against a tmp data home
# ---------------------------------------------------------------------------


@pytest.fixture()
def _halt_env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AUTONOMY_HALT", raising=False)
    monkeypatch.delenv("DATA_ROOT", raising=False)
    return tmp_path


def test_halt_writes_marker_and_resume_clears(_halt_env):
    from cli.ui.commands.h_owner import h_halt, h_resume

    tmp_path = _halt_env
    ctx, rec = _ctx()
    h_halt(ctx)
    assert (tmp_path / _PAUSE_FILENAME).exists()
    assert "Paused everything" in rec.text

    ctx2, rec2 = _ctx()
    h_resume(ctx2)
    assert not (tmp_path / _PAUSE_FILENAME).exists()
    assert "RESUMED" in rec2.text


def test_resume_without_halt_is_honest(_halt_env):
    from cli.ui.commands.h_owner import h_resume

    ctx, rec = _ctx()
    h_resume(ctx)
    assert "was not paused" in rec.text


def test_resume_reports_env_halt_honestly(_halt_env, monkeypatch):
    """AUTONOMY_HALT set in the env also halts — /resume must never claim
    RESUMED while the runtime still reports autonomy as halted."""
    from cli.ui.commands.h_owner import h_halt, h_resume

    ctx, _ = _ctx()
    h_halt(ctx)
    monkeypatch.setenv("AUTONOMY_HALT", "1")
    ctx2, rec2 = _ctx()
    h_resume(ctx2)
    assert "RESUMED" not in rec2.text
    assert "AUTONOMY_HALT" in rec2.text  # names the env var + why it still halts


# ---------------------------------------------------------------------------
# /asks and /fulfill — the goal-board asks store
# ---------------------------------------------------------------------------


def _board(tmp_path):
    from agents.task.goals.board import GoalBoard
    return GoalBoard(str(tmp_path / "goals.db"))


def test_asks_lists_open_ask(tmp_path, monkeypatch):
    from cli.ui.commands.h_owner import h_asks

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    _board(tmp_path).create_ask(user_id="u1", what="Grant Twitter write access",
                                why="X objective needs twitter_post")
    ctx, rec = _ctx(user_id="u1")
    h_asks(ctx)
    assert "Grant Twitter write access" in rec.text
    assert "/fulfill" in rec.text  # tells the owner how to act


def test_asks_empty_and_excludes_tool_approval(tmp_path, monkeypatch):
    from cli.ui.commands.h_owner import h_asks

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    # A tool_approval ask has its OWN surface (/pending) — never double-listed.
    _board(tmp_path).create_ask(
        user_id="u1", what="Approve x402_request? [feedface]",
        extra_payload={"ask_kind": "tool_approval", "request_hash": "feedface"},
        force=True)
    ctx, rec = _ctx(user_id="u1")
    h_asks(ctx)
    assert "no open asks" in rec.text
    assert "feedface" not in rec.text


def test_fulfill_marks_ask_fulfilled(tmp_path, monkeypatch):
    from agents.task.goals.board import ASK_FULFILLED
    from cli.ui.commands.h_owner import h_fulfill

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    board = _board(tmp_path)
    a = board.create_ask(user_id="u1", what="Grant Twitter write access")
    ctx, rec = _ctx(args=[a.id], user_id="u1")
    h_fulfill(ctx)
    assert board.get(a.id).status == ASK_FULFILLED
    assert "fulfilled" in rec.text


def test_fulfill_unknown_ask_is_honest(tmp_path, monkeypatch):
    from cli.ui.commands.h_owner import h_fulfill

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ctx, rec = _ctx(args=["nope"], user_id="u1")
    h_fulfill(ctx)
    assert "no open ask 'nope'" in rec.text


# ---------------------------------------------------------------------------
# /allow, /deny, /allowlist — the outbound-send allowlist
# ---------------------------------------------------------------------------


def test_allow_then_allowlist_shows_entry(tmp_path, monkeypatch):
    from cli.ui.commands.h_owner import h_allow, h_allowlist

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ctx, rec = _ctx(args=["telegram", "12345"], user_id="u1")
    h_allow(ctx)
    assert "allowed telegram:12345" in rec.text

    ctx2, rec2 = _ctx(user_id="u1")
    h_allowlist(ctx2)
    assert "telegram:12345" in rec2.text
    assert "active" in rec2.text


def test_allowlist_is_tenant_scoped(tmp_path, monkeypatch):
    from cli.ui.commands.h_owner import h_allow, h_allowlist

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ctx, _ = _ctx(args=["email", "a@x.com"], user_id="u1")
    h_allow(ctx)
    ctx2, rec2 = _ctx(user_id="other")
    h_allowlist(ctx2)
    assert "a@x.com" not in rec2.text
    assert "no allowlist entries" in rec2.text


def test_deny_revokes_and_reports_missing(tmp_path, monkeypatch):
    from cli.ui.commands.h_owner import h_allow, h_deny

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ctx, _ = _ctx(args=["telegram", "12345"], user_id="u1")
    h_allow(ctx)
    ctx2, rec2 = _ctx(args=["telegram", "12345"], user_id="u1")
    h_deny(ctx2)
    assert "denied telegram:12345" in rec2.text
    # A second deny on the now-revoked entry is honest, not a crash.
    ctx3, rec3 = _ctx(args=["telegram", "12345"], user_id="u1")
    h_deny(ctx3)
    assert "no active allowlist entry" in rec3.text


def test_allow_usage_line_on_missing_args(tmp_path, monkeypatch):
    from cli.ui.commands.h_owner import h_allow

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ctx, rec = _ctx(args=["telegram"], user_id="u1")
    h_allow(ctx)
    assert "usage: /allow" in rec.text


# ---------------------------------------------------------------------------
# /invoices and /settle — against a seeded tmp bot.db (same seam as
# tests/unit/cli/test_owner_invoices_cli.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _invoice_db(tmp_path, monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTREASURY")
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX",
                "X402_INVOICE_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    db_path = tmp_path / "bot.db"

    async def setup():
        from modules.database.connection import DatabaseConnection
        from modules.database.user_profiles import UserProfiles
        from modules.database.x402_tables import X402Tables
        from modules.x402 import invoicing

        db = DatabaseConnection(db_path)
        await db.connect()
        try:
            await UserProfiles(db).create_table()
            await X402Tables(db).create_tables()
            inv = await invoicing.create_payment_request(
                user_id="u1", session_id="s1", amount_usd=5.0,
                purpose="widget", payer_contact="Alice <a@x.com>", db=db)
        finally:
            await db.close()
        return inv

    inv = asyncio.run(setup())
    monkeypatch.setenv("DB_PATH", str(db_path))
    return inv


def test_invoices_lists_seeded_row(_invoice_db):
    from cli.ui.commands.h_owner import h_invoices

    ctx, rec = _ctx(user_id="u1")
    h_invoices(ctx)
    assert "widget" in rec.text
    assert "billed to Alice <a@x.com>" in rec.text
    assert "/settle" in rec.text  # tells the owner how to act


def test_invoices_flag_off_note_is_honest(_invoice_db):
    """X402_INVOICE_ENABLED off: rows list fine, but the same honest
    feature-off + remedy note the CLI prints must appear (no stack trace)."""
    from cli.ui.commands.h_owner import h_invoices

    ctx, rec = _ctx(user_id="u1")
    h_invoices(ctx)
    assert "X402_INVOICE_ENABLED is off" in rec.text
    assert "polyrob config set X402_INVOICE_ENABLED true" in rec.text


def test_invoices_rejects_unknown_status(_invoice_db):
    from cli.ui.commands.h_owner import h_invoices

    ctx, rec = _ctx(args=["bogus"], user_id="u1")
    h_invoices(ctx)
    assert "usage: /invoices" in rec.text


def test_settle_fake_invoice_errors_honestly(_invoice_db):
    from cli.ui.commands.h_owner import h_settle

    ctx, rec = _ctx(args=["not-a-real-id"], user_id="u1")
    h_settle(ctx)
    assert "not settled" in rec.text
    assert "Traceback" not in rec.text


def test_settle_pending_invoice_completes(_invoice_db):
    from cli.ui.commands.h_owner import h_settle

    request_id = _invoice_db["request_id"]
    ctx, rec = _ctx(args=[request_id], user_id="u1")
    h_settle(ctx)
    assert f"settled {request_id}" in rec.text


def test_settle_usage_line_on_missing_args():
    from cli.ui.commands.h_owner import h_settle

    ctx, rec = _ctx()
    h_settle(ctx)
    assert "usage: /settle" in rec.text


# ---------------------------------------------------------------------------
# /missed — owner notices the delivery rail could not send live (A7 / A40)
# ---------------------------------------------------------------------------


def test_missed_renders_three_kinds(tmp_path, monkeypatch):
    from core.event_log import TelemetryEventLog
    from cli.ui.commands.h_owner import h_missed

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    for t in ("[suppressed by daily proactive-message cap; source=a] capped one",
              "[held by owner pause; source=b] paused one",
              "[undelivered; source=c] undelivered one"):
        log.record("owner_notice", user_id="u1", source="user_delivery", attrs={"text": t})
    ctx, rec = _ctx(user_id="u1")
    h_missed(ctx)
    assert "[capped] capped one" in rec.text
    assert "[paused] paused one" in rec.text
    assert "[undelivered] undelivered one" in rec.text
    assert "delivery.daily_cap" in rec.text


def test_missed_empty_is_honest(tmp_path, monkeypatch):
    from core.event_log import TelemetryEventLog
    from cli.ui.commands.h_owner import h_missed

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    ctx, rec = _ctx(user_id="u1")
    h_missed(ctx)
    assert "no missed messages" in rec.text.lower()


def test_missed_bad_arg_is_honest(tmp_path, monkeypatch):
    from cli.ui.commands.h_owner import h_missed

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ctx, rec = _ctx(args=["abc"], user_id="u1")
    h_missed(ctx)
    assert "usage: /missed" in rec.text


def test_missed_wraps_long_text_without_clipping(tmp_path, monkeypatch):
    """Fix round 1 (2026-09-14): wrapped, never clipped — see the CLI sibling
    test in tests/unit/cli/test_owner_missed_cli.py."""
    import time

    from core.event_log import TelemetryEventLog
    from cli.ui.commands.h_owner import h_missed

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ts = 1_700_000_000.0
    words = [f"word{i:03d}" for i in range(40)]
    long_text = " ".join(words)
    assert len(long_text) >= 300
    log = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    log.record("owner_notice", user_id="u1", source="user_delivery", ts=ts,
               attrs={"text": f"[undelivered; source=a] {long_text}"})
    ctx, rec = _ctx(user_id="u1")
    h_missed(ctx)

    lines = rec.text.splitlines()
    assert lines, "expected output"
    assert all(len(line) <= 80 for line in lines)  # (a) every line fits 80 cols

    stamp = time.strftime("%m-%d %H:%M", time.gmtime(ts))
    prefix = f"  {stamp}Z — [undelivered] "
    indent = " " * len(prefix)
    chunks = []
    for line in lines:
        if line.startswith(prefix):
            chunks.append(line[len(prefix):])
        elif chunks and line.startswith(indent) and line.strip():
            chunks.append(line[len(indent):])
    # (b) the full 300+ char notice survives across the wrapped lines
    assert " ".join(chunks) == long_text
