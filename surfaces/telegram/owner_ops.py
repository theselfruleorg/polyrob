"""Owner write verbs for chat: goals, wallet, invoices, settle (G13).

Scheduled jobs (`/jobs`, `/job …`) live in ``surfaces/telegram/job_ops.py``.

Before this, creating a scheduled job, steering a goal, reading the wallet, listing
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
- **Honest about what will actually happen.** A stored job with the scheduler
  off, an invoice settled with no watcher running, a cap that needs a restart —
  each says so rather than showing a green checkmark on a no-op.
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional, Tuple

from core.surfaces.cards import is_go_word

logger = logging.getLogger(__name__)

#: Rows one chat listing renders before collapsing the rest into a count.
_LIST_LIMIT = 10

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
# /trade — the owner launches a money-granted run
# ---------------------------------------------------------------------------

#: What an owner-launched trading run carries. ``defi_trade`` is the money verb;
#: ``defi_data`` is how it reads the market it is about to act in. Nothing else
#: is added — a money run is not the place for a wide toolset.
_TRADE_TOOLS = ["defi_trade", "defi_data", "knowledge", "filesystem", "task"]


def trade_reply(user_id: Optional[str], data_dir: str, args: List[str],
                board: Optional[Any] = None) -> str:
    """``/trade <what to do>`` — seed a run that actually carries the money verb.

    Everything the agent writes for ITSELF has money stripped by ``goal_create``
    — correctly, since an injected goal must never trade — so "bridge my SOL"
    produced a goal that looked fine and could never execute, roughly fifty
    times over.

    ⚠️ D71: this used to say a money verb "reaches a run only through a stream
    leg in ``data/streams/streams.yaml``". That file does not exist: the shipped
    manifest, its seeder and its timer were DELETED on 2026-09-11 (the harness
    ships no work). A run carries a money verb because an OWNER put one in
    ``payload.tools`` from an authenticated seat — this verb, ``polyrob goals
    create --tools``, or the console. That is the whole list.

    The signal the system was throwing away is that the OWNER asking, from an
    authenticated seat, IS the authorization. This verb carries it into the
    payload. The agent still cannot self-grant; nothing here widens a cap; and
    every spend is still bounded by the per-transaction ceiling. Above the
    autonomous ceiling a spend raises an owner ask; the owner's tap re-arms the
    run, and its next attempt of the SAME call is sent (the owner grant,
    2026-09-26). Until then an approved spend was re-refused — "NOT SENT".
    """
    if not user_id:
        return "Only the owner can launch a trading run."
    task = " ".join(args).strip()
    if not task:
        return ("Usage: /trade <what to do>\n"
                "e.g. /trade bridge 0.93 SOL to USDC on Base\n"
                "Seeds a run that carries the money verb. A spend above the "
                "autonomous ceiling asks you first (/pending); once you approve "
                "it, the run's next attempt sends it.")

    if board is None:
        from agents.task.goals.board import GoalBoard
        from core.runtime_paths import goals_db_path
        board = GoalBoard(goals_db_path(data_dir))

    body = (
        f"{task}\n\n"
        "The owner asked for this directly, so this run carries `defi_trade`. "
        "Read the treasury-trading skill before acting. Every spend is still "
        "bounded by the wallet caps. A live spend above the autonomous ceiling "
        "is refused with an owner ask (it is in /pending). Tell the owner the "
        "amount and the ceiling, and stop. The owner's approval re-arms this "
        "run: make the SAME call again, with the same parameters — a changed or "
        "split call is a different request and asks again. Do NOT create "
        "follow-up goals to 'get the tool granted' — you have it here."
    )
    try:
        goal = board.create(
            user_id=user_id,
            title=f"Owner-launched: {task}"[:200],
            body=body,
            priority=9,                     # the owner asked; it goes first
            payload={"tools": list(_TRADE_TOOLS), "owner_granted": True},
            actor="owner_seat",   # 036 §3.3: the owner asking IS the grant (audited)
        )
    except Exception as e:
        logger.warning("trade run could not be seeded: %s", e)
        return f"Could not seed the run: {e}"

    return (f"✅ Launched {_code(str(getattr(goal, 'id', '?'))[:8])} with "
            f"`defi_trade` granted.\n{task}\n\n"
            + "\n".join(_trade_run_notes(data_dir)) + "\nProgress: /goals")


def _trade_run_notes(data_dir: str) -> List[str]:
    """A10/O17: what the seeded run can actually do, read at launch.

    Views only — this reads the pause record and the turn-trading flag the
    guard reads; it decides nothing. Each read fails toward the cautious line.
    """
    notes: List[str] = []
    try:
        from core.autonomy_control import allows
        dispatch, entry = allows("dispatch", data_dir), allows("trade_entry", data_dir)
    except Exception:
        dispatch = entry = None
    if dispatch is None:
        notes.append("I could not read the pause state; /status shows it.")
    elif not dispatch.allowed:
        notes.append("Paused — it runs after /resume.")
    else:
        notes.append("It runs on the next dispatcher tick.")
        if entry is not None and not entry.allowed:
            notes.append("Trading is paused: it can read and quote, but it starts "
                         "no buy until /resume trading.")
    try:
        from core.wallet.tx_guard import autonomous_turn_trading_enabled
        can_sign = autonomous_turn_trading_enabled()
    except Exception:
        can_sign = False
    if not can_sign:
        notes.append("Unattended runs may not sign on this instance: it can read "
                     "and quote, but it cannot send a transaction.")
    if can_sign:
        notes.append("A spend above the autonomous ceiling asks you first "
                     "(/pending); once you approve it, the run's next attempt "
                     "sends it.")
    return notes


# ---------------------------------------------------------------------------
# /goal
# ---------------------------------------------------------------------------

#: How many recent rows a short-id prefix is resolved against (D11/E21).
#: ``list_recent`` is newest-first, so this window holds what an owner is
#: plausibly looking at; a FULL id still resolves through ``board.get``
#: whatever its age.
_ID_WINDOW = 200


def _resolve_goal(board: Any, user_id: str, ref: str):
    """``(goal, None)`` or ``(None, refusal_text)`` for a goal id or prefix.

    ⚠️ D11/E21: this used ``board.list(user_id=…, limit=1000)``, which is the
    DISPATCHER's order (``priority DESC, created_at ASC``). Used as a lookup
    window it evicts the newest low-priority rows first — exactly the eviction
    that made the agent's own ``goal_list`` show the oldest 100 rows and zero
    stream legs on 2026-08-29 — so the owner could be told "no match" for a
    goal he was reading about one message earlier.

    Order of attempts: the FULL id through ``board.get`` (tenant-scoped, any
    age, no window at all), then a prefix over ``list_recent`` (newest-first).
    """
    ref = (ref or "").strip()
    if not ref:
        return None, "Usage: /goal <verb> <id> (see /goals)"
    try:
        exact = board.get(ref, user_id=user_id)
    except Exception as exc:
        logger.warning("goal lookup failed for %r", ref, exc_info=True)
        return None, (f"I could not read the goal board ({type(exc).__name__}: "
                      f"{str(exc)[:100]}) — that is UNKNOWN, not 'no such goal'.")
    from agents.task.goals.board import KIND_GOAL
    if exact is not None and exact.kind == KIND_GOAL:
        return exact, None
    try:
        recent = [g for g in board.list_recent(user_id=user_id, limit=_ID_WINDOW)
                  if g.kind == KIND_GOAL]
    except Exception as exc:
        logger.warning("goal board list_recent failed", exc_info=True)
        return None, (f"I could not read the goal board ({type(exc).__name__}: "
                      f"{str(exc)[:100]}) — that is UNKNOWN, not 'no such goal'.")
    goal_id, err = resolve_prefix(ref, [g.id for g in recent])
    if err:
        return None, (f"{err} among my {len(recent)} most recent goals — "
                      f"see /goals, or give the full id.")
    return next(g for g in recent if g.id == goal_id), None


def _allow_reply(user_id: str, board: Any, rest: List[str]) -> str:
    """``/goal allow <id|title>`` — turn a switched-off goal back on."""
    from core.goal_suppressions import SCOPE_TITLE, allow, match
    ref = " ".join(rest).strip()
    if not ref:
        return ("Usage: /goal allow <id|title> — turns a goal you switched off back "
                "on. /goals lists what is off.")
    hits = []
    # An id (or prefix) of a goal row names its title.
    if " " not in ref:
        goal, err = _resolve_goal(board, user_id, ref)
        if goal is not None:
            hits = match(board.db_path, user_id=user_id, text=goal.title)
    if not hits:
        hits = match(board.db_path, user_id=user_id, text=ref)
    if not hits:
        return f"Nothing switched off matches {_code(ref[:40])} — /goals lists what is off."
    if len(hits) > 1:
        return ("That matches more than one: " + "; ".join(h.label for h in hits[:5])
                + " — use more of the title.")
    h = hits[0]
    allow(board.db_path, user_id=user_id, scope=h.scope, value=h.key)
    return f"✅ Back ON: “{h.label}”. It can be created again."


_GOAL_USAGE = (
    "Usage: /goal new <what> · /goal <show|retry|pause|stop|allow> <id>\n"
    "  /goal stop <id> stops it once; add --forever so it is never created again.\n"
    "  /goal retry <id> re-queues a blocked or paused goal (a stopped one stays stopped).\n"
    "See /goals for the board.")


def _goals_off_note() -> str:
    """A stored goal only runs while the dispatcher is on — say so (F-5)."""
    try:
        from core.config_policy import AutonomyConfig
        if AutonomyConfig.goals_enabled():
            return ""
    except Exception as exc:
        logger.warning("goals enablement probe failed", exc_info=True)
        return (f"\n⚠️ I could not check whether my own work is switched on "
                f"({type(exc).__name__}) — the goal is stored. Check with /status.")
    return ("\n⚠️ My own work is switched off — the goal is stored, but nothing "
            "will run it until it is on (/status says how).")


def _goal_new(user_id: str, board: Any, rest: List[str]) -> str:
    """``/goal new <what>`` — a background goal with the owner's grant, the SAME
    ``core.owner_create.create_goal`` the console and the CLI use (F-11)."""
    from agents.task.goals.board import DuplicateGoalError, SuppressedGoalError
    from core.owner_create import create_goal, cron_options_payload, split_cron_options
    try:   # 085 W3: the same tools=/rig=/target=/chain=/skills= grant as /job new
        rest, options = split_cron_options(rest)
        tools, extra = cron_options_payload(options)
    except ValueError as e:
        return f"❌ {e}"
    text = " ".join(rest).strip()
    if not text:
        return "Usage: /goal new <what to do> — I work on it in the background."
    title, body = text, ""
    if len(text) > 120:
        title, body = text[:117].rstrip() + "…", text
    try:
        goal = create_goal(board, user_id=user_id, title=title, body=body,
                           tools=tools, extra_payload=extra)
    except SuppressedGoalError:
        return ("You switched that goal off for good. Turn it back on first: "
                "/goal allow <title>")
    except DuplicateGoalError as e:
        return (f"I already have a goal much like that: {_code(e.match_id[:8])} "
                f"“{e.match_title}” — /goal show {e.match_id[:8]}")
    except ValueError as e:
        return f"I could not add that goal: {e}"
    return (f"✅ Goal {_code(goal.id[:8])} [{goal.status}]: {goal.title}\n"
            f"Follow it: /goal show {goal.id[:8]}{_goals_off_note()}")


def _goal_grant(user_id: str, board: Any, rest: List[str]) -> str:
    """``/goal grant <id> tools=… [rig=…] [target=… chain=…]`` — the owner's
    grant on an existing goal (085 W3: the line an agent's money ``goal_update``
    card carries). Written with the owner seat's actor."""
    usage = "Usage: `/goal grant <id> tools=a,b [rig=<rig>] [target=0x… chain=<network>]`"
    if len(rest) < 2:
        return usage
    goal, err = _resolve_goal(board, user_id, rest[0])
    if err:
        return err
    from core.owner_create import grant_payload
    try:
        patch = grant_payload(rest[1:])
    except ValueError as e:
        return f"❌ {e}\n{usage}"
    patch["owner_granted"] = True
    if not board.update_fields(goal.id, user_id=user_id, payload_patch=patch):
        return f"Goal {_code(goal.id[:8])} is finished or gone — nothing granted."
    granted = ", ".join(f"{k} {v}" for k, v in patch.items()
                        if k not in ("actor", "owner_granted", "target_authored_by"))
    return f"✅ Granted goal {_code(goal.id[:8])}: {granted}."


def _goal_show(user_id: str, board: Any, goal: Any) -> str:
    """``/goal show`` — what it is, why it is where it is, what it produced, what
    it cost (when a run recorded it) and the session to open (F-12)."""
    from core.goal_owner_verbs import reason
    out = [f"{_code(goal.id[:8])} [{goal.status}] {goal.title}",
           reason(board, goal, user_id)]
    if goal.body and goal.body != goal.title:
        out.append(goal.body[:400])
    out.append(f"priority {goal.priority} · failures "
               f"{goal.consecutive_failures}/{goal.max_retries}")
    if goal.result:
        res = str(goal.result)
        out.append("result: " + (res[:1500] + "…" if len(res) > 1500 else res))
    cost = (goal.payload or {}).get("cost_usd")
    if cost is not None:
        try:
            out.append(f"cost: ${float(cost):.2f}")
        except (TypeError, ValueError):
            out.append(f"cost: unavailable (unreadable value {cost!r})")
    if goal.session_id:
        out.append(f"session: {goal.session_id}")
    return "\n".join(out)


#: `--forever` as typed, and as a phone keyboard turns `--` into a dash.
FOREVER_FLAGS = ("--forever", "—forever", "–forever")


def goal_reply(user_id: str, data_dir: str, args: List[str],
               board: Optional[Any] = None) -> str:
    """`/goal <verb> …` — the WRITE counterpart to the read-only `/goals`.

    080 WS-2: ``new`` creates; ``retry`` is the ONE re-queue (``ready`` and
    ``resume`` are deleted); ``pause`` writes ``paused``; ``stop`` is once by
    default and ``--forever`` switches the title off — the SAME
    ``core.goal_suppressions.stop_goal`` every path calls.

    ``board`` lets the caller share an already-open GoalBoard (the harness opens
    one per request) instead of opening a second connection to the same file.
    """
    from agents.task.goals.board import GoalBoard
    from core import goal_owner_verbs as V
    from core.goal_suppressions import stop_goal
    if board is None:
        from core.runtime_paths import goals_db_path
        board = GoalBoard(goals_db_path(data_dir))

    verb = (args[0].lower() if args else "")
    rest = args[1:]
    if verb == "allow":
        return _allow_reply(user_id, board, rest)
    if verb == "new":
        return _goal_new(user_id, board, rest)
    if verb == "grant":
        return _goal_grant(user_id, board, rest)
    if verb not in V.OWNER_GOAL_VERBS:
        return _GOAL_USAGE
    forever = any(a in FOREVER_FLAGS for a in rest)
    rest = [a for a in rest if a not in FOREVER_FLAGS]
    if not rest:
        return f"Usage: /goal {verb} <id> (see /goals)"

    goal, err = _resolve_goal(board, user_id, rest[0])
    if err:
        return err
    if verb == "show":
        return _goal_show(user_id, board, goal)
    if verb == "stop":
        res = stop_goal(board, user_id=user_id, goal=goal, forever=forever,
                        reason=" ".join(rest[1:]))
    elif verb == "pause":
        res = V.pause_goal(board, user_id=user_id, goal=goal)
    else:
        res = V.retry_goal(board, user_id=user_id, goal=goal)
    return ("✅ " if res.ok else "") + res.message


# ---------------------------------------------------------------------------
# /wallet  (read-only by design)
# ---------------------------------------------------------------------------

#: How the owner changes a cap FROM CHAT (D17).
#:
#: ⚠️ This used to send him to SSH: "caps are env-authoritative … plus a
#: restart". That stopped being true on 2026-09-18, when the per-transaction
#: ceiling became an owner-approved preference that RAISES the env value,
#: clamped by the daily cap. He approved a raise twice while this note told him
#: it could not work, and the agent then asked him to edit an env file.
_CAP_NOTE = (
    "Change the per-transaction ceiling from here: /config set "
    "budget.wallet_per_tx_usd <usd> (guarded → /pending → /approve, applies "
    "live). It is clamped to the daily cap, so the most a single move can "
    "lose is never more than a whole day may.\n"
    "Tighten the daily cap the same way: /config set budget.wallet_daily_usd "
    "<usd> — that one can only ever LOWER what the operator set, so no chat "
    "message can widen the maximum daily loss.\n"
    "When the wallet signer is installed, its hard caps bound both: a raise "
    "from chat stops at the signer's cap, which only root changes on the box.\n"
    "Change how much runs without asking you: /wallet autonomous <usd>."
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
                "ceiling; change that one with /config set "
                "budget.wallet_per_tx_usd <usd> (owner-approved, clamped to "
                "the daily cap).")
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
                   f"That one is a loss limit: raise it with /config set "
                   f"budget.wallet_per_tx_usd <usd> (guarded → /approve), and it "
                   f"stays clamped to the daily cap.")
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
    if args and args[0].lower() in ("tokens", "token", "trust", "untrust"):
        # W1: which tokens are trusted, and the owner's trust / untrust — the
        # chat seat that replaces `polyrob wallet pin-token` on the server.
        from core.open_positions import open_positions_db_path
        from core.wallet.token_trust import tokens_reply
        return tokens_reply(user_id, list(args),
                            positions_db=(open_positions_db_path(data_dir)
                                          if data_dir else None))
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
        # D72: name what the owner can DO. A flag name on a phone screen is
        # something he can only act on by opening a shell.
        return ("I have no wallet — one has not been enabled for me, so I "
                "hold no funds and can move none. Arm it with "
                "`polyrob config set AGENT_WALLET_ENABLED true` (it applies on "
                "my next restart).")

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
    #
    # ⚠️ D40: a derivation FAULT used to hide the line entirely, which reads
    # exactly like "this wallet has no Solana identity" — and the owner then
    # funds nothing and a Solana trade fails later on an empty fee balance. An
    # address we could not derive is named as unavailable, with the reason.
    try:
        sol_addr = w.solana_address
    except Exception as exc:
        logger.warning("solana address derivation failed", exc_info=True)
        sol_addr = None
        lines.append(f"Solana: unavailable ({type(exc).__name__}: "
                     f"{str(exc)[:80]}) — I could not derive it, which is not "
                     f"the same as not having one.")
    if sol_addr:
        lines.append(f"Solana: {_code(sol_addr)}")
    if want_balances:
        if cfg.network != "mainnet":
            lines.append("(balances are mainnet-only — network is "
                         f"{cfg.network})")
        else:
            lines.extend(_wallet_balance_lines(w))
    lines.extend(_cap_lines(user_id, cfg))
    if not want_balances:
        lines.append("/wallet balances reads the on-chain amounts.")
    lines.append(_CAP_NOTE)
    return "\n".join(lines)


def _cap_lines(user_id: Optional[str], cfg: Any) -> List[str]:
    """The caps the GATE will actually apply, not the ones it was BUILT with.

    ⚠️ D16: ``cfg`` is the ``WalletConfig`` frozen when the process-wide wallet
    singleton was constructed. Since 2026-09-18 the live gate re-resolves both
    caps per spend (``PolicyGate._refresh_caps`` → ``effective_max_per_tx_usd``
    / ``effective_daily_cap_usd``), so an owner-approved raise applied
    immediately to SPENDING and never to this readout — the one screen he
    checks to confirm the raise landed. Read the same resolvers the gate does.

    Fail-open to the constructed values, LABELLED: a cap we could not re-resolve
    must not be printed as though it were the live one.
    """
    from core.runtime_paths import prefs_home_dir
    try:
        from core.wallet.config import (effective_daily_cap_usd,
                                        effective_max_per_tx_usd)
        home = prefs_home_dir()
        per_tx = effective_max_per_tx_usd(user_id, home)
        daily_value = effective_daily_cap_usd(user_id, home)
        stale = ""
    except Exception as exc:
        logger.warning("effective wallet caps unreadable", exc_info=True)
        per_tx, daily_value = cfg.max_per_tx_usd, cfg.daily_cap_usd
        stale = (f" ⚠️ these are the values I started with — I could not "
                 f"re-read the live ones ({type(exc).__name__})")
    daily = f"${daily_value:.2f}" if daily_value is not None else "UNLIMITED"
    out = [f"Caps: ${per_tx:.2f}/tx · daily {daily}{stale}"]
    if daily_value is None:
        out.append("⚠️ The per-tx cap is a catastrophic-loss ceiling, not a "
                   "budget, and the daily cap is unlimited.")
    if not stale:
        out.extend(_signer_bound_lines(user_id))
    return out


def _signer_bound_lines(user_id: Optional[str]) -> List[str]:
    """Where a setting sits above the signer's hard cap, name the cap that
    binds and where to raise it (the signer is the envelope). Fail-open: no
    signer, or no answer, is no line."""
    try:
        from core.runtime_paths import prefs_home_dir
        from core.wallet.config import configured_caps_now
        from core.wallet.signer_envelope import (bound_legs, bound_text, reported_caps,
                                                 signer_unanswered, unanswered_text)
        if signer_unanswered():
            # The zero clamp of a silent signer is not a cap to raise in
            # signer.toml: say the signer is down instead.
            text = unanswered_text()
            return ["⚠️ " + text[0].upper() + text[1:]]
        caps = reported_caps()
        if caps is None:
            return []
        legs = bound_legs(caps, configured_caps_now(user_id=user_id,
                                                    home_dir=prefs_home_dir()))
    except Exception:
        logger.debug("signer bound check failed", exc_info=True)
        return []
    return ["Bound by the signer — " + bound_text(b) for b in legs]


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
    """`/invoices [<status>]` — the agent's receivables.

    ⚠️ The filter vocabulary is ``modules.x402.invoicing.INVOICE_STATUSES``,
    imported and never re-listed. This seat carried its own
    ``pending|completed|expired`` triple, so the two states that matter most
    were unaskable: ``settling`` (claimed, facilitator in flight) and
    ``refund_due`` — money the agent TOOK and owes back. A row in either state
    simply disappeared from this seat the moment it was written.
    """
    from modules.x402.invoicing import INVOICE_STATUSES, list_payment_requests
    _usage = f"Usage: /invoices [{'|'.join(INVOICE_STATUSES)}]"
    status = None
    if args:
        candidate = args[0].lower()
        if candidate in INVOICE_STATUSES:
            status = candidate
        else:
            return _usage
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
    # A `refund_due` row is money the agent TOOK and owes back, so it must not
    # sit in the list under the same one-line remedy as a pending receivable.
    _owed = [r for r in rows if str(r.get("status")) == "refund_due"]
    if _owed:
        _owed_usd = sum(float(r.get("amount_usd") or 0) for r in _owed)
        lines.append(f"⚠️ {len(_owed)} of these are REFUNDS I owe "
                     f"(${_owed_usd:.2f}) — paid for work that then failed. "
                     f"`/invoices refund_due` lists only those.")
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

def bridge_chain_names() -> str:
    """The destination chains this build actually knows, from the ONE registry.

    ⚠️ C62: this was a hand-typed list ("solana, base, robinhood, ethereum,
    arbitrum, polygon") that could only ever drift from
    ``core.wallet.chains``. Fail-open to naming the remedy rather than a stale
    list — a wrong list of money chains is worse than no list.
    """
    try:
        from core.wallet import chains as _chains
        names = sorted(set(_chains.names()) | {"solana"})
        return ", ".join(names)
    except Exception:
        logger.warning("chain registry unreadable for /bridge usage",
                       exc_info=True)
        return "(I could not read my chain registry — try `/book`)"


async def bridge_reply(user_id: Optional[str], data_dir: str,
                       args: List[str]) -> str:
    """`/bridge <from> <to> <amount> [from usdc] [as <asset>] [go]` — move value across chains.

    Why this exists: the bridge shipped CLI-only, which meant the one person
    allowed to run it had to SSH to the box. The owner's standing directive is
    that he can do anything from the chat, and he is usually on a phone. Without
    this verb the whole rail is unreachable from where he actually is.

    Bare form QUOTES (dry run — asserts everything, broadcasts nothing). Adding
    `go` executes: within the autonomous ceiling it runs and reports; above it,
    the durable owner-approval queue holds it — typing `go` is a deliberate
    second act, not the approval itself.

    ⚠️ D12: ``async`` and awaited. This used to call ``asyncio.run`` on the
    polling loop's own thread, catch the resulting ``RuntimeError`` and hand the
    coroutine to a thread pool, then BLOCK on ``.result()`` — so every inbound
    Telegram update stalled for the whole quote (a bridge quote is seconds of
    network), and any loop-affine object the verb touched belonged to the wrong
    loop. The rail underneath is async; this seat is async.

    ⚠️ C63: ``as <asset>`` names the DESTINATION asset (``BridgeParams.
    token_out``) — ``native`` by default, or a token PINNED in the destination
    chain's registry row. An arbitrary address is refused by the rail, not here.
    """
    if not user_id:
        return "Only the owner can bridge."
    if len(args) < 3:
        return ("Usage: /bridge <from> <to> <amount> [from usdc] [as <asset>] [go]\n"
                "e.g. /bridge solana robinhood 0.9          — quote only\n"
                "     /bridge solana robinhood 0.9 go       — execute\n"
                "     /bridge robinhood solana 0.1 as usdc go  — ETH in, USDC on Solana\n"
                "     /bridge base solana 50 from usdc as usdc — USDC to USDC\n\n"
                f"Chains: {bridge_chain_names()}.\n"
                "The origin is NATIVE (SOL/ETH), or `from usdc` for the chain's "
                "pinned USDC. `as` picks what ARRIVES: native (default), or a "
                "token pinned for that chain.\n"
                "Under your autonomous ceiling it runs and reports; above it, "
                "it waits for you in /pending.")

    tokens = list(args)
    execute = False
    if tokens and is_go_word(tokens[-1]):
        execute = True
        tokens.pop()

    token_out = "native"
    for i, word in enumerate(tokens):
        if word.lower() in ("as", "into", "to_asset") and i + 1 < len(tokens):
            token_out = tokens[i + 1]
            tokens = tokens[:i] + tokens[i + 2:]
            break
    token_in = "native"
    for i, word in enumerate(tokens):
        if word.lower() in ("from", "with") and i + 1 < len(tokens):
            token_in = tokens[i + 1]
            tokens = tokens[:i] + tokens[i + 2:]
            break

    if len(tokens) < 3:
        return "Usage: /bridge <from> <to> <amount> [from usdc] [as <asset>] [go]"
    from_chain, to_chain, amount_raw = tokens[0], tokens[1], tokens[2]
    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return f"Amount must be a positive number, got {amount_raw!r}."

    try:
        from tools.defi.bridge_verb import perform_bridge
        from tools.defi.trade_tool import BridgeParams, DefiTradeTool
    except Exception as exc:                       # pragma: no cover - import guard
        return f"The bridge rail is unavailable: {exc}"

    params = BridgeParams(from_chain=from_chain, to_chain=to_chain,
                          amount=amount, token_in=token_in, token_out=token_out,
                          dry_run=not execute)
    # A genuine owner chat turn. Not forged, not a sub-agent — the same seat the
    # CLI is, reached from the phone instead of a shell.
    from core.security.turn_facts import owner_seat_context
    ctx = owner_seat_context(user_id, seat="telegram")
    try:
        result = await perform_bridge(DefiTradeTool(), params, ctx)
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
        _as = ((f" from {token_in}" if token_in != "native" else "")
               + (f" as {token_out}" if token_out != "native" else ""))
        from core.surfaces.cards import with_go_hint
        body = with_go_hint(body, "\n\nAdd `go` to execute: "
                            f"/bridge {from_chain} {to_chain} {amount_raw}{_as} go")
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
# /avatar — the agent's avatar image, from the phone
#
# The avatar is ONE image slot (`core/avatar.py`); core generates no face. The
# owner sees it, sets it from a URL or an NFT's image, or clears it. Only the
# owner reaches this verb, and it is refused from a room (`_ROOM_REFUSED`).
# ---------------------------------------------------------------------------

_AVATAR_USAGE = ("`/avatar` — show the avatar\n"
                 "`/avatar set <https-url | ipfs://… | nft:chain:contract:id>` — "
                 "replace it\n"
                 "`/avatar clear` — remove it\n"
                 "(From a file on the box: `polyrob avatar set <file>`.)")


def avatar_reply(data_dir: str, args: List[str]) -> Tuple[str, Optional[str]]:
    """``(text, image_path_or_None)`` for the instance's avatar slot.

    Returns the image path SEPARATELY rather than embedding it, so this stays a
    pure function the tests can read and the one side effect (sending the
    image) lives at the single call site in the harness.
    """
    from core.avatar import describe, load_avatar
    from core.instance import resolve_instance_id

    if args:
        return (_AVATAR_USAGE, None)
    instance_id = resolve_instance_id()
    st = load_avatar(data_dir, instance_id)
    if st.state == "none":
        return (f"*{instance_id}* — avatar not set.\nThat is a normal optional "
                f"state. To give this instance a face:\n" + _AVATAR_USAGE, None)
    if st.is_default:
        return (f"*{instance_id}* — avatar: the default polyrob mark.\n"
                f"To give it its own face:\n" + _AVATAR_USAGE, str(st.path))
    lines = [f"*{instance_id}* — avatar {describe(st)}"]
    if st.is_set and st.set_at:
        lines.append(f"set {st.set_at}")
    if st.is_set and not st.is_raster:
        lines.append("_an SVG: it arrives as a file, and X/Telegram/Discord need a raster image_")
    return ("\n".join(lines), str(st.path) if st.is_set else None)


async def avatar_change(data_dir: str, args: List[str]) -> Tuple[str, Optional[str]]:
    """``/avatar set <ref>`` | ``/avatar clear`` -> ``(text, image_path_or_None)``."""
    from core.avatar import AvatarError, clear_avatar, describe, set_avatar
    from core.instance import resolve_instance_id

    instance_id = resolve_instance_id()
    sub = args[0].lower() if args else ""
    if sub == "clear":
        removed = clear_avatar(data_dir, instance_id)
        return ((f"*{instance_id}* — avatar cleared; the default mark is back." if removed
                 else f"*{instance_id}* — there was no avatar to clear (it shows the default mark)."), None)
    ref = " ".join(args[1:]).strip()
    if sub != "set" or not ref:
        return (_AVATAR_USAGE, None)
    from tools import avatar_sources
    try:
        if ref.startswith("nft:"):
            chain, contract, token_id = avatar_sources.parse_nft_ref(ref[len("nft:"):])
            data, source = await avatar_sources.image_from_nft(chain, contract, token_id)
        else:
            data, source = await avatar_sources.image_from_url(ref)
        st = set_avatar(data_dir, instance_id, data, source=source)
    except AvatarError as e:
        return (f"The avatar was NOT changed: {e}", None)
    return (f"*{instance_id}* — avatar {describe(st)}\n"
            f"_the X/Telegram account pictures keep the old image until "
            f"`polyrob avatar push` on the server_", str(st.path))


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
