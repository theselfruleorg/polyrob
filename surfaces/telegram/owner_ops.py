"""Owner write verbs for chat: cron, goals, wallet, invoices, settle (G13).

Before this, creating a cron job, steering a goal, reading the wallet, listing
invoices or attesting a payment existed on exactly ONE seat — the `polyrob` CLI.
A phone-only owner could watch all of it and change none of it.

These are thin plumbing over the SAME primitives the CLI calls (``CronService``,
``GoalBoard``, ``get_agent_wallet``, ``modules.x402.invoicing``) — never an
import of ``cli/``, and never a second implementation of the rule. They live
here rather than in ``harness.py`` because that file is already god-file sized,
and in ``surfaces/`` rather than ``core/`` because the surfaces tier may import
agents/tools/modules freely (a ``core/`` home would need a new row in the
shrink-only layering ratchet).

Two rules every handler follows:

- **Phone-sized replies.** Lists cap at :data:`_LIST_LIMIT` with a count line;
  every id and path rides in backticks so the renderer emits ``<code>`` and the
  client stops linkifying it.
- **Honest about what will actually happen.** A stored cron job with the ticker
  off, an invoice settled with no watcher running, a cap that needs a restart —
  each says so rather than showing a green checkmark on a no-op.
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Rows one chat listing renders before collapsing the rest into a count.
_LIST_LIMIT = 10

#: Separator for `/cron add <schedule> | <task>`. A schedule spec contains
#: spaces ("every monday 09:00") and so does a task, so positional splitting
#: cannot tell them apart.
_CRON_SEP = "|"


def _code(text: Any) -> str:
    return f"`{text}`"


def _tail(shown: int, total: int, what: str) -> Optional[str]:
    if total <= shown:
        return None
    return f"(+{total - shown} more {what} — see the console or ask me)"


# ---------------------------------------------------------------------------
# id prefixes
# ---------------------------------------------------------------------------

def resolve_prefix(prefix: str, ids: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a short id PREFIX against IDS -> ``(full_id, error)``.

    Every chat listing shows truncated ids (`a1b2c3d4`), so requiring the full
    uuid back would make the listings unusable. An ambiguous prefix is an error,
    never a guess — these verbs cancel goals and settle invoices.
    """
    p = (prefix or "").strip()
    if not p:
        return None, "no id given"
    if p in ids:
        return p, None
    matches = [i for i in ids if i.startswith(p)]
    if not matches:
        return None, f"no match for {_code(p)}"
    if len(matches) > 1:
        return None, (f"{_code(p)} matches {len(matches)} ids — "
                      f"use more characters")
    return matches[0], None


# ---------------------------------------------------------------------------
# /cron
# ---------------------------------------------------------------------------

def _cron_service(data_dir: str):
    from cron.jobs import CronJobStore
    from cron.service import CronService
    from core.runtime_paths import cron_db_path
    return CronService(CronJobStore(cron_db_path(data_dir)))


def _cron_off_note() -> str:
    """A stored job only runs if the ticker is on — never mislead the owner."""
    try:
        from tools.cronjob_tools import cron_enabled
        if cron_enabled():
            return ""
    except Exception:
        logger.debug("cron enablement probe failed", exc_info=True)
        return ""
    return ("\n⚠️ CRON_ENABLED is off — the job is stored but no ticker will run it.")


def _fmt_job(job) -> str:
    nxt = job.next_run_at.strftime("%Y-%m-%d %H:%M") if job.next_run_at else "-"
    shot = "once" if job.one_shot else "recurring"
    return (f"• {_code(job.id[:8])} [{job.status}] {job.task}\n"
            f"   {job.schedule_spec} · {shot} · next {nxt}")


def cron_reply(user_id: str, data_dir: str, args: List[str]) -> str:
    """`/cron` — list / show / add / cancel durable scheduled runs."""
    svc = _cron_service(data_dir)
    sub = (args[0].lower() if args else "list")
    rest = args[1:]

    if sub in ("list", "ls"):
        jobs = svc.list_jobs(user_id=user_id)
        if not jobs:
            return ("No cron jobs.\n"
                    f"Add one: /cron add <schedule> {_CRON_SEP} <task>\n"
                    "e.g. /cron add every monday 09:00 | summarise the week")
        shown = jobs[:_LIST_LIMIT]
        lines = [f"{len(jobs)} cron job(s):"] + [_fmt_job(j) for j in shown]
        more = _tail(len(shown), len(jobs), "job(s)")
        if more:
            lines.append(more)
        return "\n".join(lines)

    if sub == "show":
        if not rest:
            return "Usage: /cron show <id>"
        jobs = svc.list_jobs(user_id=user_id)
        job_id, err = resolve_prefix(rest[0], [j.id for j in jobs])
        if err:
            return f"{err} — see /cron."
        job = next(j for j in jobs if j.id == job_id)
        out = [_fmt_job(job),
               f"   created {job.created_at} · last run {job.last_run_at or '-'}",
               f"   max duration {job.max_duration_seconds}s"]
        if job.payload:
            out.append(f"   payload: {_code(job.payload)}")
        return "\n".join(out)

    if sub in ("add", "schedule", "new"):
        raw = " ".join(rest)
        if _CRON_SEP not in raw:
            return (f"Usage: /cron add <schedule> {_CRON_SEP} <task>\n"
                    "Schedules: a duration (30m), 'every monday 09:00', a 5-field "
                    "cron line, or an ISO timestamp.\n"
                    "e.g. /cron add every monday 09:00 | summarise the week")
        spec, _, task = raw.partition(_CRON_SEP)
        spec, task = spec.strip(), task.strip()
        if not spec or not task:
            return f"Both parts are required: /cron add <schedule> {_CRON_SEP} <task>"
        from cron.schedule import ScheduleError
        try:
            job = svc.schedule(task=task, schedule_spec=spec, user_id=user_id)
        except ScheduleError as e:
            return f"Invalid schedule {_code(spec)}: {e}"
        nxt = job.next_run_at.strftime("%Y-%m-%d %H:%M") if job.next_run_at else "-"
        return (f"✅ Scheduled {_code(job.id[:8])} — next run {nxt}."
                + _cron_off_note())

    if sub in ("cancel", "rm", "delete"):
        if not rest:
            return "Usage: /cron cancel <id>"
        jobs = svc.list_jobs(user_id=user_id)
        job_id, err = resolve_prefix(rest[0], [j.id for j in jobs])
        if err:
            return f"{err} — see /cron."
        if svc.cancel(job_id, user_id=user_id):
            return f"✅ Cancelled {_code(job_id[:8])}."
        return f"Could not cancel {_code(job_id[:8])} — see /cron."

    return ("Usage: /cron [list] · /cron show <id> · "
            f"/cron add <schedule> {_CRON_SEP} <task> · /cron cancel <id>")


# ---------------------------------------------------------------------------
# /trade — the owner launches a money-granted run
# ---------------------------------------------------------------------------

#: What an owner-launched trading run carries. ``defi_trade`` is the money verb;
#: ``defi_data`` is how it reads the market it is about to act in. Nothing else
#: is added — a money run is not the place for a wide toolset.
_TRADE_TOOLS = ["defi_trade", "defi_data", "knowledge", "filesystem", "task"]


def trade_reply(user_id: Optional[str], data_dir: str, args: List[str],
                board: Optional[Any] = None) -> str:
    """``/trade <what to do>`` — seed a run that actually carries the money verb.

    A money verb reaches a run only through a stream leg in
    ``data/streams/streams.yaml``, which seeds on a timer. Everything the agent
    writes for ITSELF has money stripped by ``goal_create`` — correctly, since
    an injected goal must never trade — so "bridge my SOL" produced a goal that
    looked fine and could never execute, roughly fifty times over.

    The signal the system was throwing away is that the OWNER asking, from an
    authenticated seat, IS the authorization. This verb carries it into the
    payload. The agent still cannot self-grant; nothing here widens a cap; and
    every spend is still bounded by the per-transaction ceiling and still
    queues for ``/approve`` above the autonomous ceiling.
    """
    if not user_id:
        return "Only the owner can launch a trading run."
    task = " ".join(args).strip()
    if not task:
        return ("Usage: /trade <what to do>\n"
                "e.g. /trade bridge 0.93 SOL to USDC on Base\n"
                "Seeds a run that carries the money verb. Spends above the "
                "autonomous ceiling still come to you via /pending.")

    if board is None:
        from agents.task.goals.board import GoalBoard
        from core.runtime_paths import goals_db_path
        board = GoalBoard(goals_db_path(data_dir))

    body = (
        f"{task}\n\n"
        "The owner asked for this directly, so this run carries `defi_trade`. "
        "Read the treasury-trading skill before acting. Every spend is still "
        "bounded by the wallet caps, and anything above the autonomous ceiling "
        "returns lane=owner_queue — that is normal, not a blocker: report it "
        "and stop, the owner approves from chat. Do NOT create follow-up goals "
        "to 'get the tool granted' — you have it here."
    )
    try:
        goal = board.create(
            user_id=user_id,
            title=f"Owner-launched: {task}"[:200],
            body=body,
            priority=9,                     # the owner asked; it goes first
            payload={"tools": list(_TRADE_TOOLS), "max_steps": 40,
                     "owner_granted": True},
        )
    except Exception as e:
        logger.warning("trade run could not be seeded: %s", e)
        return f"Could not seed the run: {e}"

    return (f"✅ Launched {_code(str(getattr(goal, 'id', '?'))[:8])} with "
            f"`defi_trade` granted.\n{task}\n\n"
            "It runs on the next dispatcher tick. Spends above the autonomous "
            "ceiling come to you for approval — /pending, then /approve <id>. "
            "Progress: /goals")


# ---------------------------------------------------------------------------
# /goal
# ---------------------------------------------------------------------------

#: verb -> (target status, reset failures, allowed source statuses or None,
#: past participle for the refusal message — `f"{verb}ed"` produces "retryed")
_GOAL_TRANSITIONS = {
    "ready": ("ready", False, ("triage", "blocked"), "marked ready"),
    "pause": ("blocked", False, None, "paused"),
    "resume": ("ready", False, ("blocked",), "resumed"),
    "retry": ("ready", True, ("blocked",), "retried"),
    "cancel": ("cancelled", False, None, "cancelled"),
}


_OBJECTIVE_STATUS_VERBS = {"pause": "paused", "activate": "active", "drop": "dropped"}


def _objective_reply(user_id: str, board: Any, rest: List[str]) -> str:
    """`/goal objective <list|pause|activate|drop> [id]` — steer a whole stream.

    Objectives were CLI-only: a phone-only owner could steer one goal but could
    not see, pause or resume the standing stream behind it. Read plus the three
    lifecycle transitions is the whole surface; CREATE stays on the CLI, because a
    stream needs a body, success criteria and a toolset that chat cannot express
    well (and, for a money stream, a manifest entry rather than a board row).
    """
    from agents.task.goals.board import OBJ_ACTIVE
    sub = (rest[0].lower() if rest else "list")
    if sub == "list":
        objs = board.objectives(user_id=user_id)
        if not objs:
            return ("No objectives. Create one on the CLI: "
                    "polyrob goals objective add \"<title>\"")
        lines = [f"{len(objs)} objective(s):"]
        for o in objs:
            try:
                live = len(board.children_of(user_id, o.id))
            except Exception:
                live = 0
            lines.append(f"• {_code(o.id[:8])} [{o.status}] {o.title} — {live} live")
        lines.append("Steer with /goal objective <pause|activate|drop> <id>.")
        return "\n".join(lines)

    if sub not in _OBJECTIVE_STATUS_VERBS:
        return ("Usage: /goal objective <list|pause|activate|drop> [id]\n"
                "Create an objective on the CLI: polyrob goals objective add \"<title>\"")
    if len(rest) < 2:
        return f"Usage: /goal objective {sub} <id> (see /goal objective list)"

    mine = board.objectives(user_id=user_id)
    oid, err = resolve_prefix(rest[1], [o.id for o in mine])
    if err:
        return f"{err} — see /goal objective list."
    target = _OBJECTIVE_STATUS_VERBS[sub]
    if not board.set_objective_status(oid, target, user_id=user_id):
        return f"Could not update {_code(oid[:8])}."
    note = ("\nIts goals keep running; the planner stops adding new ones."
            if target != OBJ_ACTIVE else "")
    return f"✅ {_code(oid[:8])} → {target}.{note}"


def goal_reply(user_id: str, data_dir: str, args: List[str],
               board: Optional[Any] = None) -> str:
    """`/goal <verb> <id>` — the WRITE counterpart to the read-only `/goals`.

    ``board`` lets the caller share an already-open GoalBoard (the harness opens
    one per request) instead of opening a second connection to the same file.
    """
    from agents.task.goals.board import KIND_GOAL, GoalBoard
    if board is None:
        from core.runtime_paths import goals_db_path
        board = GoalBoard(goals_db_path(data_dir))

    verb = (args[0].lower() if args else "")
    rest = args[1:]
    if verb == "objective":
        return _objective_reply(user_id, board, rest)
    if verb not in _GOAL_TRANSITIONS and verb != "show":
        return ("Usage: /goal <show|ready|pause|resume|retry|cancel> <id>\n"
                "See /goals for the board.")
    if not rest:
        return f"Usage: /goal {verb} <id> (see /goals)"

    mine = [g for g in board.list(user_id=user_id, limit=1000) if g.kind == KIND_GOAL]
    goal_id, err = resolve_prefix(rest[0], [g.id for g in mine])
    if err:
        return f"{err} — see /goals."
    goal = next(g for g in mine if g.id == goal_id)

    if verb == "show":
        out = [f"{_code(goal.id[:8])} [{goal.status}] {goal.title}"]
        if goal.body:
            out.append(goal.body[:400])
        out.append(f"priority {goal.priority} · failures {goal.consecutive_failures}")
        if goal.last_failure_error:
            out.append(f"last failure: {goal.last_failure_error[:200]}")
        return "\n".join(out)

    target, reset, allowed, participle = _GOAL_TRANSITIONS[verb]
    if allowed is not None and goal.status not in allowed:
        base = (f"{_code(goal.id[:8])} is {goal.status} — only "
                f"{'/'.join(allowed)} goals can be {participle}.")
        # `waiting` is the ORDINARY state for a seeded chain whose prerequisite
        # has not run, and the bare refusal above was a dead end: the owner was
        # told no with no reason, no prerequisite and no next step
        # (2026-09-08). Forcing the transition would be wrong — the dependency
        # is doing its job — so explain it instead.
        if goal.status == "waiting":
            try:
                pending = [d for d in (board.dependencies(goal.id) or [])
                           if (board.get(d) is not None
                               and getattr(board.get(d), "status", None) != "done")]
            except Exception:
                pending = []
            if pending:
                out = [base, "It is queued behind:"]
                for dep_id in pending:
                    dep = board.get(dep_id)
                    out.append(f"  • {_code(dep_id[:8])} [{dep.status}] {dep.title}")
                out.append("That runs first, then this one is picked up "
                           "automatically. Detail: /goal show <id>")
                return "\n".join(out)
            return (f"{base}\nNothing unfinished is blocking it — it looks "
                    f"stranded and should be picked up on the next sweep. "
                    f"Detail: /goal show {goal.id[:8]}")
        return base
    if goal.status == target:
        return f"{_code(goal.id[:8])} is already {target}."
    warning = ""
    if verb == "pause" and goal.status == "running":
        warning = "\n⚠️ It is running right now — the pause takes effect after this run."
    ok = board.update_status(goal_id, target, reset_failures=reset, user_id=user_id)
    if not ok:
        return f"Could not update {_code(goal.id[:8])} — see /goals."
    suffix = " (failures cleared)" if reset else ""
    return f"✅ {_code(goal.id[:8])} → {target}{suffix}.{warning}"


# ---------------------------------------------------------------------------
# /wallet  (read-only by design)
# ---------------------------------------------------------------------------

#: Why the cap-raising path is deliberately absent from chat.
_CAP_NOTE = (
    "Caps are env-authoritative and a raise needs the env file the running "
    "process reads (a systemd deploy reads its own, e.g. "
    "`/etc/polyrob/polyrob.env`) plus a restart — which chat must never write.\n"
    "To TIGHTEN one from here: /config set budget.wallet_daily_usd <usd> "
    "(guarded → /pending → /approve, applies live, can only lower)."
)


def _set_autonomous_ceiling(rest: List[str], user_id: Optional[str],
                            data_dir: Optional[str]) -> str:
    """Write ``budget.defi_autonomous_usd`` — how much runs without asking.

    The owner had to SSH in and edit an env file to change this, which is why
    a $96 bridge sat behind a $5 ceiling for a day. It is safe from chat
    precisely because it is not a loss limit: ``tx_guard.autonomous_max_usd``
    clamps it to the catastrophic per-transaction ceiling, so raising it can
    only reduce how often the owner is interrupted.
    """
    if not user_id:
        return "Only the owner can change spend settings."
    if not rest:
        return ("Usage: /wallet autonomous <usd>\n"
                "How much may execute without asking you. Anything above it "
                "queues for /approve. It can never exceed the per-transaction "
                "ceiling, which stays env-only on purpose.")
    try:
        value = float(str(rest[0]).lstrip("$"))
    except (TypeError, ValueError):
        return f"{_code(rest[0])} is not a number — usage: /wallet autonomous <usd>"
    if value < 0:
        return "A ceiling cannot be negative."
    try:
        from core import prefs
        from core.runtime_paths import prefs_home_dir
        prefs.write_preference(prefs_home_dir(), user_id,
                               "budget.defi_autonomous_usd", value)
        from core.wallet import tx_guard
        effective = tx_guard.autonomous_max_usd(user_id, prefs_home_dir())
    except Exception as e:
        logger.warning("autonomous ceiling not written: %s", e)
        return f"Could not save it: {e}"
    out = [f"✅ Autonomous ceiling set to ${value:,.2f}.",
           f"Effective now: ${effective:,.2f} — spends up to this run without "
           f"asking you; above it they queue for /approve."]
    if effective < value:
        out.append(f"⚠️ Clamped to the per-transaction ceiling ${effective:,.2f}. "
                   f"That one is a catastrophic-loss limit and is not settable "
                   f"from chat by design.")
    return "\n".join(out)


def wallet_reply(args: List[str], user_id: Optional[str] = None,
                 data_dir: Optional[str] = None) -> str:
    """`/wallet` — addresses, network, caps, and the one cap the owner may set.

    On-chain balance probes are network reads, so they are opt-in via
    `/wallet balances` rather than paid for on every status glance.

    `/wallet autonomous <usd>` sets how much executes WITHOUT interrupting the
    owner. That is deliberately the only cap writable from chat: it cannot
    widen maximum loss, because the catastrophic per-transaction ceiling still
    binds above it (and is clamped to it at the read site). The catastrophic
    ceiling itself stays env-only — if a chat surface could raise it, a
    compromised chat surface could drain the treasury.
    """
    from core.wallet.authority import owner_refusal
    refusal = owner_refusal(user_id)
    if refusal:
        return refusal
    if args and args[0].lower() in ("autonomous", "auto", "ceiling"):
        return _set_autonomous_ceiling(args[1:], user_id, data_dir)
    if not args or args[0].lower() in ("overview", "accounts"):
        from core.wallet.view import wallet_view, render_wallet
        try:
            return render_wallet(wallet_view(user_id, data_dir=data_dir))
        except PermissionError as exc:
            return str(exc)
    want_balances = bool(args) and args[0].lower() in ("balances", "balance", "full")
    try:
        from core.wallet.factory import get_agent_wallet
        w = get_agent_wallet()
    except ValueError as e:
        return f"Wallet unavailable: {e}"
    except Exception as e:
        logger.debug("wallet read failed", exc_info=True)
        return f"Wallet unavailable: {e}"
    if w is None:
        return "Agent wallet is not enabled (AGENT_WALLET_ENABLED)."

    cfg = w.config
    try:
        address = w.address
    except ValueError as e:
        return f"Wallet MISCONFIGURED: {e}"
    lines = [f"Wallet · network {cfg.network} · venue {w.operational_venue}",
             f"Fund: {_code(address)}"]
    # The Solana identity off the same seed (2026-08-27): the owner must see —
    # and be able to fund — this address from the same glance, or it stays
    # invisible until a Solana trade fails on an empty fee balance.
    try:
        sol_addr = w.solana_address
    except Exception:
        sol_addr = None
    if sol_addr:
        lines.append(f"Solana: {_code(sol_addr)}")
    if want_balances:
        if cfg.network != "mainnet":
            lines.append("(balances are mainnet-only — network is "
                         f"{cfg.network})")
        else:
            lines.extend(_wallet_balance_lines(w))
    daily = (f"${cfg.daily_cap_usd:.2f}" if cfg.daily_cap_usd is not None
             else "UNLIMITED")
    lines.append(f"Caps: ${cfg.max_per_tx_usd:.2f}/tx · daily {daily}")
    if cfg.daily_cap_usd is None:
        lines.append("⚠️ The per-tx cap is a catastrophic-loss ceiling, not a "
                     "budget, and the daily cap is unlimited.")
    if not want_balances:
        lines.append("/wallet balances reads the on-chain amounts.")
    lines.append(_CAP_NOTE)
    return "\n".join(lines)


#: Venues that hold a same-chain float at their DERIVED address. hyperliquid
#: (delegated signer) and polymarket (per-user proxy) never do, and showing a
#: balance there re-creates the fund-the-wrong-address footgun. Mirrors
#: ``cli/commands/wallet.py::_FUNDABLE`` — the CLI keeps its own copy; both read
#: the chain map and the balance reader from ``core.wallet.onchain``.
_FUNDABLE_VENUES = ("treasury", "x402")


def _wallet_balance_lines(w) -> List[str]:
    """Per-venue on-chain balances, fail-open to a note (never a traceback)."""
    from core.wallet.onchain import VENUE_CHAIN, balances as _balances
    out = []
    for venue in _FUNDABLE_VENUES:
        try:
            addr = w.signer_for(venue).address
            native, usdc = _balances(addr, VENUE_CHAIN[venue])
        except Exception:
            logger.debug("balance probe failed for %s", venue, exc_info=True)
            out.append(f"  {venue}: balance unavailable")
            continue
        u = f"{usdc:.2f}" if usdc is not None else "n/a"
        n = f"{native:.5f}" if native is not None else "n/a"
        out.append(f"  {venue}: USDC={u} gas={n}")
    try:
        sol_addr = w.solana_address
    except Exception:
        sol_addr = None
    if sol_addr:
        try:
            from core.wallet import chains as _chains
            from core.wallet import solana_onchain
            sol_bal = solana_onchain.native_balance(sol_addr)
            usdc_raw = None
            row = _chains.get("solana")
            if row and row.usdc:
                tb = solana_onchain.token_balances(sol_addr)
                usdc_raw = None if tb is None else tb.get(row.usdc, 0)
            u = f"{usdc_raw / 1e6:.2f}" if usdc_raw is not None else "n/a"
            s = f"{sol_bal:.5f}" if sol_bal is not None else "n/a"
            out.append(f"  solana: USDC={u} SOL={s}")
        except Exception:
            logger.debug("solana balance probe failed", exc_info=True)
            out.append("  solana: balance unavailable")
    return out


# ---------------------------------------------------------------------------
# /invoices and /settle
# ---------------------------------------------------------------------------

def _invoicing_off_note() -> str:
    """The ONE feature-off note (``modules.x402.invoicing_note``), on its own
    line for a chat bubble."""
    from modules.x402.invoicing_note import invoicing_off_note
    note = invoicing_off_note()
    return f"\n⚠️ {note}" if note else ""


async def invoices_reply(user_id: str, args: List[str]) -> str:
    """`/invoices [pending|completed|expired]` — the agent's receivables."""
    status = None
    if args:
        candidate = args[0].lower()
        if candidate in ("pending", "completed", "expired"):
            status = candidate
        else:
            return ("Usage: /invoices [pending|completed|expired]")
    from modules.x402.invoicing import list_payment_requests
    try:
        rows = await list_payment_requests(user_id=user_id, status=status, limit=50)
    except Exception as e:
        logger.warning("telegram /invoices read failed: %s", e, exc_info=True)
        return f"Could not read invoices: {e}"
    scope = f"tenant {_code(user_id)}" + (f" · {status}" if status else "")
    if not rows:
        return f"No invoices ({scope})."
    shown = rows[:_LIST_LIMIT]
    lines = [f"{len(rows)} invoice(s) — {scope}:"]
    for r in shown:
        line = (f"• {_code(str(r['request_id'])[:8])} [{r['status']}] "
                f"${float(r.get('amount_usd') or 0):.2f} — "
                f"{r.get('purpose') or '(no purpose)'}")
        if r.get("payer_contact"):
            line += f" · billed to {r['payer_contact']}"
        lines.append(line)
    more = _tail(len(shown), len(rows), "invoice(s)")
    if more:
        lines.append(more)
    lines.append("Mark one paid: /settle <id> [tx-hash]")
    note = _invoicing_off_note()
    if note:
        lines.append(note.strip())
    return "\n".join(lines)


async def settle_reply(user_id: str, args: List[str]) -> str:
    """`/settle <id> [tx-hash]` — attest an invoice as PAID (pending → completed).

    Owner attestation, not a payment: it records that money arrived. Scoped to
    the caller's own tenant, so a chat id can never settle another tenant's row.
    """
    if not args:
        return "Usage: /settle <invoice-id> [tx-hash] (see /invoices)"
    from modules.x402.invoicing import list_payment_requests, settle_payment_request
    try:
        rows = await list_payment_requests(user_id=user_id, status="pending", limit=200)
    except Exception as e:
        logger.warning("telegram /settle lookup failed: %s", e, exc_info=True)
        return f"Could not read invoices: {e}"
    request_id, err = resolve_prefix(args[0], [str(r["request_id"]) for r in rows])
    if err:
        return f"{err} among your PENDING invoices — see /invoices pending."
    tx_hash = args[1] if len(args) > 1 else None
    try:
        settled = await settle_payment_request(request_id, transaction_hash=tx_hash)
    except Exception as e:
        logger.warning("telegram /settle failed: %s", e, exc_info=True)
        return f"Settle failed: {e}"
    if not settled:
        return (f"{_code(request_id[:8])} was not settled — it may already be "
                "completed, or that tx hash already settled another invoice.")
    out = f"✅ Settled {_code(request_id[:8])}."
    note = _invoicing_off_note()
    out += (note if note else
            "\nThe settlement watcher will wake the originating session.")
    return out


# ---------------------------------------------------------------------------
# /bridge (037)
# ---------------------------------------------------------------------------

def bridge_reply(user_id: Optional[str], data_dir: str, args: List[str]) -> str:
    """`/bridge <from> <to> <amount> [go]` — move NATIVE value between chains.

    Why this exists: the bridge shipped CLI-only, which meant the one person
    allowed to run it had to SSH to the box. The owner's standing directive is
    that he can do anything from the chat, and he is usually on a phone. Without
    this verb the whole rail is unreachable from where he actually is.

    Bare form QUOTES (dry run — asserts everything, broadcasts nothing). Adding
    `go` executes: within the autonomous ceiling it runs and reports; above it,
    the durable owner-approval queue holds it — typing `go` is a deliberate
    second act, not the approval itself.
    """
    if not user_id:
        return "Only the owner can bridge."
    if len(args) < 3:
        return ("Usage: /bridge <from> <to> <amount> [go]\n"
                "e.g. /bridge solana robinhood 0.9      — quote only\n"
                "     /bridge solana robinhood 0.9 go   — execute\n\n"
                "Chains: solana, base, robinhood, ethereum, arbitrum, polygon.\n"
                "NATIVE asset only (SOL on solana, ETH on an EVM chain).\n"
                "Under your autonomous ceiling it runs and reports; above it, "
                "it waits for you in /pending.")

    from_chain, to_chain, amount_raw = args[0], args[1], args[2]
    execute = len(args) > 3 and args[3].lower() in ("go", "execute", "confirm")
    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return f"Amount must be a positive number, got {amount_raw!r}."

    import asyncio
    from types import SimpleNamespace

    try:
        from tools.defi.bridge_verb import perform_bridge
        from tools.defi.trade_tool import BridgeParams, DefiTradeTool
    except Exception as exc:                       # pragma: no cover - import guard
        return f"The bridge rail is unavailable: {exc}"

    params = BridgeParams(from_chain=from_chain, to_chain=to_chain,
                          amount=amount, dry_run=not execute)
    # A genuine owner chat turn. Not forged, not a sub-agent — the same seat the
    # CLI is, reached from the phone instead of a shell.
    ctx = SimpleNamespace(user_id=user_id, role="owner", is_sub_agent=False)
    try:
        result = asyncio.run(perform_bridge(DefiTradeTool(), params, ctx))
    except RuntimeError:
        # Already inside a loop (the surface runs async) — hand it to a thread.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(
                asyncio.run, perform_bridge(DefiTradeTool(), params, ctx)).result()
    except Exception as exc:
        logger.warning("bridge verb failed", exc_info=True)
        return f"The bridge did not run: {exc}"

    if getattr(result, "error", None):
        return f"❌ {result.error}"
    body = getattr(result, "extracted_content", None)
    if not body:
        return ("The bridge returned neither an error nor a report. That is a "
                "bug — do NOT retry until it is understood; assume nothing "
                "about what happened to the funds.")
    if not execute:
        body += ("\n\nAdd `go` to execute: "
                 f"/bridge {from_chain} {to_chain} {amount_raw} go")
    return body


# ---------------------------------------------------------------------------
# /mcp — per-tenant MCP servers
# ---------------------------------------------------------------------------

def mcp_reply(user_id: Optional[str], args: List[str]) -> str:
    """`/mcp [add <id> <url> [key] | remove <id> | test <id>]`.

    Thin plumbing over ``core.mcp_admin`` — the ONE helper set every owner seat
    renders, so Telegram, the REPL and the console can never drift into three
    different answers about which MCP servers exist.
    """
    from core import mcp_admin
    return mcp_admin.mcp_reply(user_id, args)


# ---------------------------------------------------------------------------
# /avatar — the owner SEES the agent's face, from the phone
#
# The Mindprint identity reached no chat surface at all: `pfp push` sets a
# profile picture on X and Discord and prints BotFather steps for Telegram, but
# nothing ever showed the owner the face, its traits, or the voice signature.
# Read-only on purpose -- `pfp keep` is a one-way permanent lock, so the setup
# ceremony stays on the CLI where it cannot be fired by a stray chat message.
# ---------------------------------------------------------------------------

def avatar_reply(data_dir: str, args: List[str]) -> Tuple[str, Optional[str]]:
    """``(text, png_path_or_None)`` for the instance's frozen identity.

    Returns the image path SEPARATELY rather than embedding it, so this stays a
    pure function the tests can read and the one side effect (sending a photo)
    lives at the single call site in the harness.
    """
    from core.instance import load_pfp_meta, pfp_path, resolve_instance_id

    if args:
        return ("/avatar is read-only — it shows this instance's face, traits "
                "and voice signature.\nSetting up or changing the identity is a "
                "one-time owner ceremony on the CLI: `polyrob pfp generate`, "
                "`polyrob pfp randomize`, then `polyrob pfp keep` (permanent).",
                None)

    instance_id = resolve_instance_id()
    png = pfp_path(data_dir, instance_id)
    if not png.is_file():
        return (f"*{instance_id}* — avatar not set up.\n"
                f"That is a normal optional state. To give this instance a face: "
                f"`polyrob pfp generate`, re-roll with `polyrob pfp randomize`, "
                f"then `polyrob pfp keep` (permanent).", None)

    meta = load_pfp_meta(data_dir, instance_id)
    if not isinstance(meta, dict):
        # The image is there but its record does not parse. Neither "kept" nor
        # "not set up" is true, and both would read as confident.
        return (f"*{instance_id}* — the avatar image exists but its record "
                f"(pfp.json) is unreadable, so I cannot show its traits.",
                str(png))

    kept = bool(meta.get("locked", True))
    traits = meta.get("traits") if isinstance(meta.get("traits"), dict) else {}
    voice = meta.get("voice") if isinstance(meta.get("voice"), dict) else {}
    lines = [f"*{instance_id}* — avatar "
             + ("kept (permanent)" if kept else "DRAFT — not kept yet")]
    if meta.get("seed_hex"):
        lines.append(f"seed {meta['seed_hex']} · {meta.get('generator', '?')}")
    if traits:
        lines.append("traits: " + ", ".join(f"{k} {v}" for k, v in sorted(traits.items())))
    if voice:
        lines.append("voice: pitch {p} · rate {r} · timbre {t}".format(
            p=voice.get("pitch", "?"), r=voice.get("rate", "?"),
            t=voice.get("timbre", "?")))
    if not kept:
        lines.append("_re-roll:_ `polyrob pfp randomize` · _accept:_ "
                     "`polyrob pfp keep` (permanent)")
    return ("\n".join(lines), str(png))


# --------------------------------------------------------------------------- #
# 043 D1 — /inbox and /book on the phone
# --------------------------------------------------------------------------- #

#: Phone width. The 80-column renderer is right for a terminal; a chat client
#: re-wraps proportional text, so a narrower measure keeps a line one line.
_CHAT_WIDTH = 60
#: Rows per section before the reply becomes a scroll rather than a decision.
_CHAT_ITEMS = 5


def inbox_reply(user_id: str, data_dir: str) -> str:
    """Everything waiting on an owner decision, blocking first.

    The SAME composition the console and the REPL render
    (``core.surfaces.inbox`` over ``surfaces.inbox_sources``), through the SAME
    text renderer (``core.surfaces.inbox_render``) with this seat's own remedy
    verbs — so three seats can never disagree about what is waiting.

    ⚠️ A composer failure is reported as UNKNOWN, never as "nothing needs you".
    """
    from core.surfaces.inbox_render import CHAT_REMEDIES, render_inbox
    from surfaces.inbox_sources import build_inbox
    try:
        body = build_inbox(user_id, data_dir=data_dir)
    except Exception as exc:
        logger.warning("inbox: composition failed", exc_info=True)
        return (f"I could not read the inbox at all ({exc}). That is UNKNOWN, "
                f"not 'nothing needs you'.")
    return render_inbox(body, remedies=CHAT_REMEDIES, limit=_CHAT_ITEMS,
                        width=_CHAT_WIDTH)


async def book_reply(user_id: str, data_dir: str) -> str:
    """The ledger against every money chain — one verdict, then what disagrees."""
    from core.surfaces.inbox_render import render_book
    from tools.defi.book import read_book
    try:
        body = await read_book(user_id, data_dir)
    except Exception as exc:
        logger.warning("book: read failed", exc_info=True)
        return (f"I could not read the book ({exc}). That is UNKNOWN, not a "
                f"clean book — do not trade on it.")
    return render_book(body, width=_CHAT_WIDTH)
