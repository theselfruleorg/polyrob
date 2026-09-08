"""P4: Telegram webhook harness — wiring + decision->action dispatch.

Two layers:
- derive_webhook_path() and act_on_inbound() are deterministic and unit-tested
  (no aiogram, no network) — the route derivation (NEVER the old bot's hardcoded
  /mvpbot) and the mapping from a RouteDecision to a TaskAgent action.
- build_telegram_harness() (below) does the live aiogram Bot+Dispatcher+set_webhook
  wiring; it lazy-imports aiogram and is exercised only when TELEGRAM_SURFACE_ENABLED.
  Its round-trip needs a real bot token to verify.

act_on_inbound maps:
  STEER       -> orchestrator.submit_user_message (warm-but-dead -> rehydrate, below)
  TASK_AGENT  -> create_session(session_source=, chat_session_key=) then run_session
  COMMAND     -> /cancel /new /task /help
  CHAT_FASTPATH -> treated as TASK_AGENT for the MVP (the ChatAgent fast-path is a
                   later optimization; never drop the message).
"""
import asyncio
import logging
import os
from typing import Any, Callable, Optional

from core.surfaces.dispatcher import RouteKind
from core.surfaces import voice_guard as _core_vg
from core.surfaces.serialize import KeyedLock
from surfaces.telegram.inbound import InboundResult

logger = logging.getLogger(__name__)

# B6: per-chat inbound serialization (see act_on_inbound docstring).
_INBOUND_LOCK = KeyedLock()

# Back-off when getUpdates returns a CONFLICT (another instance polling the same
# token). Long enough that we don't spam, short enough to recover quickly once the
# other instance stops.
_CONFLICT_BACKOFF_SEC = 30

try:  # class-identity check when aiogram is present; string fallback otherwise
    from aiogram.exceptions import TelegramConflictError as _TelegramConflictError
except Exception:  # pragma: no cover - aiogram always present in prod
    _TelegramConflictError = None


async def _send_telegram_text(bot, chat_id, text: str) -> None:
    """Deliver ``text`` to ``chat_id`` through the ONE outbound seam.

    The post-run reply, the out-of-band sink and the surface's own send() all render
    and chunk identically because they all end up in ``TelegramSurface.send_text``
    (markdown -> Telegram HTML, split at the message cap, plain-text retry on a markup
    rejection). This wrapper exists only because the sink holds a raw bot, not a surface.
    """
    from surfaces.telegram.surface import TelegramSurface
    await TelegramSurface(bot).send_text(chat_id, text)


def _is_conflict_error(exc: Exception) -> bool:
    """True if *exc* is a Telegram getUpdates 'Conflict' (another instance polling)."""
    if _TelegramConflictError is not None and isinstance(exc, _TelegramConflictError):
        return True
    name = type(exc).__name__
    return "Conflict" in name or "terminated by other getUpdates" in str(exc)

_DEFAULT_WEBHOOK_PATH = "/telegram/webhook"

def _agent_name() -> str:
    """The instance's own name for user-facing help — never a hardcoded bot name."""
    try:
        from core.instance import resolve_instance_id
        return resolve_instance_id()
    except Exception:
        return "the agent"


# SSOT for /help AND the setMyCommands menu (help_commands() parses the "/"
# lines; section headers are plain lines and are skipped). 030 WS-C4: grouped —
# 28 verbs as one flat wall hid the kill-switch between /fulfill and /resume.
_HELP_BODY = (
    "— Tasks —\n"
    "/task <goal> — start a new task\n"
    "/cancel — stop the current task\n"
    "/new — start a fresh conversation\n"
    "— Control —\n"
    "/pause [scope…] [for 6h] — stop autonomous work now (all, or: trading streams planner cron social oversight pings)\n"
    "/resume [scope…] — lift the pause\n"
    "/halt — alias of /pause (everything)\n"
    "/status — health first, then session, goals, loops, delivery, posture, money\n"
    "/mode — effective autonomy posture (all axes) and how to change it\n"
    "/missed [n] — owner messages the daily cap suppressed (default 5)\n"
    "— Approvals & asks —\n"
    "/pending — proposals I've learned, awaiting your approval\n"
    "/approve <id> — activate a pending proposal\n"
    "/reject <id> — discard a pending proposal\n"
    "/asks — what I need from you to unblock work\n"
    "/fulfill <id> — mark an ask fulfilled (unblocks its goals)\n"
    "— Autonomy —\n"
    "/cron [list|show|add|cancel] — durable scheduled runs\n"
    "/goal <show|ready|pause|resume|retry|cancel> <id> — steer one goal\n"
    "/goal objective <list|pause|activate|drop> [id] — steer a whole stream\n"
    "/goals — goal board summary\n"
    "/apps [show|approve|reject|kill|logs <slug>] — durable apps: approve an address, health, kill\n"
    "/recap [window] — what I've done (default 24h, e.g. 30m/24h/7d; alias /journey)\n"
    "— Money —\n"
    "/wallet [balances] — addresses, network and spend caps\n"
    "/invoices [status] — what I've billed and who owes me\n"
    "/settle <id> [tx] — mark an invoice paid\n"
    "— Access —\n"
    "/allow <surface> <target> — allow me to message that target\n"
    "/deny <surface> <target> — revoke that permission\n"
    "/allowlist — show who I'm allowed to message\n"
    "— Settings & files —\n"
    "/prefs [all] — the preferences you have set (add 'all' for every key)\n"
    "/config — read or set preferences (safe keys write immediately; "
    "guarded keys queue for /pending review)\n"
    "/kb <query> — search my knowledge base\n"
    "/files [n] — recent files I produced (default 10)\n"
    "/dev [message] — message the dev/ops loop (Claude) directly; bare /dev = rail status\n"
    "/help [verb] — this help, or one verb's detail\n"
    "Or just send a message to talk to {name}."
)

#: G11: the slash list is not the whole control surface — the agent can act
#: through its own tools when asked in prose ("schedule a check every morning at
#: 9", "add a goal to …"). That path predates the slash verbs and is strictly
#: wider than them, but nothing ever told the owner it existed, so /help read as
#: an exhaustive list of what was possible.
_HELP_PROSE_NOTE = (
    "\n\nYou don't have to use a command. Ask me in plain words — "
    "\"schedule a check every morning at 9\", \"add a goal to …\", "
    "\"what did you spend this week\" — and I'll use my own tools."
)


def _help_text() -> str:
    name = _agent_name()
    return f"{name} commands:\n" + _HELP_BODY.format(name=name) + _HELP_PROSE_NOTE


def _help_for(verb: str) -> str:
    """`/help <verb>` (030 WS-C4): the verb's line(s) from the SSOT — including
    subverb lines like `/goal objective …` — without triggering the verb."""
    v = "/" + verb.strip().lstrip("/").lower()
    lines = [ln for ln in _HELP_BODY.splitlines()
             if ln.startswith(v + " ") or ln.startswith(v + "\n") or
             ln.split(" ")[0] == v]
    if not lines:
        return _unknown_command_text(v)
    return "\n".join(lines)


def _welcome_text() -> str:
    """First-contact reply for /start (030 L9) — short, not the verb catalog."""
    name = _agent_name()
    return (
        f"👋 I'm {name}. Send a message in plain words to give me a task, "
        "or /help for the command list."
    )


def _unknown_command_text(cmd: str) -> str:
    """Cheap teaching reply for a command-shaped token no handler owns (030 L9).

    Before this, an unknown/typo verb fell through to the LLM as chat text —
    a spawned session, typing indicator and tokens for `/statsu`. Suggest the
    closest real verbs from the `_HELP_BODY` SSOT and point at /help.
    """
    import difflib
    known = [f"/{name}" for name, _ in help_commands()]
    matches = difflib.get_close_matches(cmd, known, n=2, cutoff=0.6)
    hint = f" Did you mean {' or '.join(matches)}?" if matches else ""
    return f"Unknown command {cmd}.{hint} Send /help for the full list."

_OWNER_ADMIN_COMMANDS = ("/pending", "/approve", "/reject", "/asks", "/fulfill",
                         "/allow", "/deny", "/allowlist",
                         "/halt", "/resume", "/pause",
                         "/cron", "/goal", "/wallet", "/invoices", "/settle",
                         "/status", "/mode", "/recap", "/journey", "/goals", "/prefs", "/config",
                         "/missed", "/apps",
                         "/kb", "/files", "/dev")


def help_commands() -> list:
    """``(command, description)`` pairs parsed from the ``_HELP_BODY`` SSOT.

    Feeds Telegram's ``setMyCommands`` so the phone gets a real "/" menu with
    autocomplete instead of the owner having to remember 21 verbs. Sourced from
    the help text rather than a second list, so the menu can never drift from it
    (chat-first review 2026-08-22, G12).
    """
    out = []
    seen = set()
    for line in _HELP_BODY.splitlines():
        if not line.startswith("/"):
            continue
        head, _, desc = line.partition(" — ")
        name = head.split()[0].lstrip("/").strip()
        desc = desc.strip()
        if not name or not desc:
            continue
        # Telegram: lowercase a-z/0-9/_ only, and a 256-char description cap.
        if not name.replace("_", "").isalnum() or not name.islower():
            continue
        # A help line for a SUBVERB of an existing top-level command (e.g.
        # "/goal objective ..." under "/goal ...") parses to the same command
        # name. Telegram's setMyCommands rejects a duplicate name in the whole
        # call, and _publish_command_menu wraps that call in a broad
        # `except Exception: logger.debug(...)` — so one duplicate would
        # silently stop the ENTIRE menu from refreshing, not just this entry.
        # Keep the first (top-level) occurrence; do not "simplify" this away.
        if name in seen:
            continue
        seen.add(name)
        out.append((name, desc[:256]))
    return out


def owner_allowed(tg_user_id) -> Optional[bool]:
    """Owner-allowlist gate over raw Telegram numeric user IDs.

    Reads ALLOWED_TELEGRAM_USER_IDS (comma list). Returns:
      None  -> no allowlist set (bootstrap mode: the handler replies with the
               sender's id so the operator can lock the bot, and does NOT run the agent)
      True  -> the id is on the allowlist (proceed)
      False -> an allowlist exists but this id isn't on it (ignore)
    """
    raw = (os.getenv("ALLOWED_TELEGRAM_USER_IDS") or "").strip()
    if not raw:
        return None
    allowed = {p.strip() for p in raw.split(",") if p.strip()}
    if not allowed:
        return None
    return str(tg_user_id) in allowed


def derive_webhook_path() -> str:
    """Webhook route from WEBHOOK_PATH env (normalized to a leading slash).

    Defaults to /telegram/webhook — explicitly NOT the old bot's hardcoded /mvpbot.
    """
    raw = (os.getenv("WEBHOOK_PATH") or _DEFAULT_WEBHOOK_PATH).strip()
    if not raw:
        raw = _DEFAULT_WEBHOOK_PATH
    return raw if raw.startswith("/") else "/" + raw


def _spawn(coro, spawn: Optional[Callable[[Any], Any]]) -> None:
    if spawn is not None:
        spawn(coro)
    else:
        asyncio.create_task(coro)


def _last_error_text(task_agent: Any, session_id: str) -> str:
    """Best-effort terminal ActionResult error for a finished session.

    The in-loop halt path (error_recovery sets ``stopped`` + returns an error
    ActionResult) surfaces through run_session as a bare "Session failed:
    Unknown error" — the agent-result dict carries no 'error' key — so LLM-outage
    classification needs the ledger's terminal error text ("PERMANENT ERROR: …
    402 …" / "All LLM providers failed. Tried: …"). Fail-open: any shape
    mismatch returns ""."""
    try:
        get_orch = getattr(task_agent, "get_orchestrator", None)
        orch = get_orch(session_id) if get_orch is not None else None
        agents = getattr(orch, "agents", None) if orch is not None else None
        agent = next(iter(agents.values()), None) if agents else None
        items = getattr(getattr(agent, "history", None), "history", None)
        if items:
            results = getattr(items[-1], "result", None) or []
            if results:
                return str(getattr(results[-1], "error", "") or "")
    except Exception:  # pragma: no cover - fail-open probe
        pass
    return ""


async def _run_and_deliver(task_agent: Any, user_id: str, session_id: str, deliver,
                           notice_key: Optional[str] = None) -> None:
    """Run a session to completion, then deliver the agent's REAL reply to the chat.

    ``run_session`` returns a generic STATUS string ('Session completed successfully'),
    NOT the agent's answer — so sending its return would deliver a useless status line.
    The actual reply lives in the agent's history; we extract it exactly the way
    ``TaskAgent.chat_once`` does (``_extract_chat_reply`` — which strips brain-state
    telemetry and prefers the clean ``done()`` output) and send THAT.

    This closes the interactive-chat outage (proposal 004): the STEER/fresh paths
    fire-and-forgot ``run_session`` and discarded the reply, so the owner got silence.
    Delivery happens in exactly ONE place (here), and the immediate-reply branch in
    ``handle_update`` only fires for COMMAND/DENIED/busy (which never spawn) — so there
    is no double-send. Fail-open: a delivery error never escapes the spawned turn.

    Two silence bugs this closes (2026-07-16): the C10 bound-session skip below
    assumed the router "already delivered live" UNCONDITIONALLY — false for an
    errored run, where the agent never reaches send_message/done, so nothing was
    delivered live AND the fallback was skipped. And an empty reply on a failed
    run returned silently. A failed run now always says something.
    """
    status = await task_agent.run_session(user_id, session_id)
    if deliver is None:
        return
    # run_session returns a human-readable string, not a status enum. The
    # "Session failed: ..." prefix (generic exception) and "Session suspended: ..."
    # prefix (agents/task_agent_lite.py's `except InsufficientCreditsError` path —
    # the OTHER credit-death shape, distinct from a mid-step LLM 402) are both real
    # failures; the canonical list lives in run_as_session._RUN_REFUSALS
    # (comment "live-test F7"). "Session is already executing" and "No new
    # input; ..." are no-ops that must stay silent — do NOT swap in
    # run_as_session.is_refusal() here, it also matches those and would break
    # the deliberate busy-session silence.
    failed = str(status or "").startswith(("Session failed:", "Session suspended:"))

    # T1.1 enable-blocker (validated 2026-07-23): a RUN_BUDGET_USD halt on a
    # CONTINUED session ends the turn before any new step, so the extraction
    # below would surface the PREVIOUS turn's reply as if it answered this
    # message. run_session's return IS the honest halt text for exactly this
    # case (mirrors chat_once's marker branch) — deliver it and stop. Scoped
    # to the budget marker; every other failure keeps the notice/extraction rail.
    if failed:
        try:
            from agents.task.agent.core.run_budget import RUN_BUDGET_MARKER
        except ImportError:  # pragma: no cover - core-only install
            RUN_BUDGET_MARKER = "run_budget_exhausted"
        if RUN_BUDGET_MARKER in str(status):
            try:
                await deliver(f"⚠️ {status}")
            except Exception as e:
                logger.error("telegram reply delivery failed: %s", e, exc_info=True)
            return

    if not failed:
        # C10: when Singular Chat is bound, the send_message / done router mirror
        # already delivered the reply LIVE (MarkdownV2) for a SUCCESSFUL run.
        # Delivering again here would double-send (raw plain text). Skip for bound
        # sessions; keep the post-run deliver for unbound/legacy paths (cron,
        # `polyrob run`, raw API) that have no router.
        try:
            get_orch = getattr(task_agent, "get_orchestrator", None)
            orch = get_orch(session_id) if get_orch is not None else None
            if orch is not None and getattr(orch, "_message_router", None) is not None \
                    and getattr(orch, "_chat_session_key", None):
                logger.debug(
                    "telegram: session %s bound to chat router; skipping post-run deliver "
                    "(already delivered live)", session_id,
                )
                return
        except Exception as e:  # pragma: no cover - fail-open
            logger.debug("telegram bound-session check failed: %s", e)

    reply = ""
    try:
        extract = getattr(task_agent, "_extract_chat_reply", None)
        if extract is not None:
            reply = extract(session_id) or ""
    except Exception as e:  # pragma: no cover - fail-open
        logger.debug("telegram extract chat reply failed: %s", e)
    reply = (reply or "").strip()

    if not reply:
        if not failed:
            # A successful run with nothing to say is legitimately silent.
            logger.info("telegram: session %s produced no deliverable reply", session_id)
            return
        # A FAILED run must never be silent — that is the outage (2026-07-16: the
        # owner asked a question, OpenRouter 402'd, and got absolute silence). The
        # raw error_msg is deliberately NOT forwarded here — it's provider/stack
        # text (a 402 body can be ~2KB of JSON); it goes to the log. The credit
        # sentinel is what delivers the actionable "credits are out" notice.
        logger.warning(
            "telegram: session %s failed with no reply; sending error notice (status=%s)",
            session_id, status,
        )
        # Proposal 015 #2: an LLM-provider outage (ALL providers exhausted /
        # 402 cascade) gets a specific, static, LLM-independent notice —
        # kill-switch LLM_OUTAGE_NOTICE (default ON) + a 30-min per-surface+chat
        # cooldown so a 402 storm can't spam the chat. This runs ONLY here, on
        # the chat-surface post-run deliver seam (act_on_inbound → spawned
        # _run_and_deliver), so goal/cron/self-wake runs can never trigger it.
        # Fail-open: any error in classification falls through to the legacy
        # generic notice below, never into the error path.
        try:
            from core.surfaces.error_notices import notice_for_texts
            from core.surfaces.llm_outage_notice import (
                OUTAGE_NOTICE_TEXT,
                looks_like_llm_outage,
                should_send_llm_outage_notice,
            )
            last_err = _last_error_text(task_agent, session_id)
            if looks_like_llm_outage(status, last_err):
                if not should_send_llm_outage_notice(notice_key or session_id):
                    # Flag off, or within the cooldown window: deliberately
                    # silent (the failure itself is logged above; the first
                    # notice of the window already told this chat).
                    logger.info(
                        "telegram: LLM-outage notice suppressed for session %s "
                        "(flag off or cooldown)", session_id,
                    )
                    return
                # T1.2: append a reason-specific phrase (from the SAME
                # classify_text SSOT looks_like_llm_outage just used) on top
                # of the generic notice — never raw provider/model text, only
                # one of the fixed sentences in core.surfaces.error_notices.
                notice = OUTAGE_NOTICE_TEXT
                phrase = notice_for_texts(status, last_err)
                if phrase:
                    notice = f"{OUTAGE_NOTICE_TEXT}\n{phrase}"
                reply = notice
        except Exception:
            logger.debug("llm outage notice classification failed (fail-open)",
                         exc_info=True)
        if not reply:
            reply = ("⚠️ I couldn't answer that — my run failed before I could reply. "
                     "If this keeps happening, my API credits may be out.")

    try:
        await deliver(reply)
    except Exception as e:
        logger.error("telegram reply delivery failed: %s", e, exc_info=True)


async def _start_task_session(task_agent: Any, result: InboundResult, spawn, deliver=None) -> None:
    """create_session with the binding kwargs, then run it AND deliver its reply."""
    inbound = result.inbound
    # OWNER interactive sessions get the introspection + mission toolset (goal/twitter/
    # web_fetch) so "review your goals" uses goal_list instead of guessing from the
    # sandbox filesystem. None for a non-owner -> the conservative default stands.
    from surfaces.telegram.interactive_tools import owner_interactive_tool_ids
    tool_ids = owner_interactive_tool_ids(inbound.identity.user_id)
    info = await task_agent.create_session(
        inbound.identity.user_id,
        request=inbound.text,
        session_source=inbound.identity.source,
        chat_session_key=result.decision.session_key,
        tool_ids=tool_ids,
    )
    session_id = info.get("id") if isinstance(info, dict) else getattr(info, "id", None)
    if session_id:
        _spawn(_run_and_deliver(task_agent, inbound.identity.user_id, session_id, deliver,
                                notice_key=result.decision.session_key), spawn)


def _admin_data_dir(task_agent: Any) -> str:
    """The daemon's data home — the SAME dir the pending writers/goal board use."""
    cfg = getattr(getattr(task_agent, "container", None), "config", None)
    from core.runtime_paths import data_dir_or_home
    return data_dir_or_home(getattr(cfg, "data_dir", None))


def _is_admin_owner(user_id: str) -> bool:
    """Owner gate for the admin verbs. Telegram is a NETWORK surface, so the
    single-user local bypass is never honored here — only the bound principal
    (the owner alias maps the owner's telegram id onto it) qualifies."""
    from core.instance import is_owner_local_safe, resolve_owner_principal
    return is_owner_local_safe(user_id, owner_principal=resolve_owner_principal(),
                               local_enabled=False)


async def _status_reply(task_agent: Any, user_id: str, session_id: Optional[str],
                        data_dir: str, board: Optional[Any] = None) -> str:
    """`/status` — rendered from the ONE status snapshot (core/status_snapshot.py).

    2026-08-28: this used to assemble its own view (ready/running counts + the
    posture card + two money lines) and drop any section whose read failed, so
    it rendered "Goals: 0 open, 0 running · kill switch: clear" over a
    credit-dead provider, 91 suppressed owner notices, two open asks and a
    blocked goal. Now: health first (never empty), every section typed, an
    unreadable source renders ``unavailable (<reason>)`` instead of vanishing.

    ``board`` is accepted for call-site compatibility; the snapshot reads the
    same ``goals.db`` read-only. Runs in a worker thread so the network balance
    probe (a display surface opts in) never blocks the polling loop.
    """
    from core.status_snapshot import build_status_snapshot
    from core.status_render import render_status_text
    snap = await asyncio.to_thread(
        build_status_snapshot, user_id, data_dir=data_dir, task_agent=task_agent,
        session_id=session_id, include_balances=True)
    return render_status_text(snap, title="Status:", prefix="• ", health_limit=_STATUS_HEALTH_LIMIT)


#: Health lines one chat `/status` renders before rolling the rest into a count
#: (a phone screen; the console/CLI keep the full list).
_STATUS_HEALTH_LIMIT = 8


async def _health_header(user_id: str, data_dir: str, task_agent: Any = None,
                         limit: int = 4) -> list:
    """The mandatory health block other read verbs (/mode, /recap, /goals)
    prepend — a degraded instance renders first on EVERY seat. Cheap path (no
    money, no balances). Never raises: a builder failure renders as its own
    'unavailable' line rather than silently producing no header."""
    try:
        from core.status_snapshot import build_status_snapshot
        from core.status_render import render_health_lines
        from core.status_render import pause_headline
        snap = await asyncio.to_thread(
            build_status_snapshot, user_id, data_dir=data_dir, task_agent=task_agent,
            include_money=False)
        # 031: the pause state leads every seat, before health.
        return [pause_headline(snap)] + render_health_lines(snap, prefix="• ", limit=limit)
    except Exception as e:
        logger.warning("telegram health header failed: %s", e, exc_info=True)
        return [f"Health: unavailable ({type(e).__name__}: {str(e)[:120]})"]


def _pause_line(data_dir: str) -> str:
    """The cheap (no snapshot) pause line for verbs that build no header (/goals)."""
    try:
        from core.status_render import pause_headline_from
        from core.surfaces.owner_admin import pause_state
        return pause_headline_from(pause_state(data_dir).to_dict())
    except Exception as e:
        logger.warning("telegram pause line failed: %s", e, exc_info=True)
        return f"autonomy: unavailable ({type(e).__name__}: {str(e)[:120]})"


def _missed_reply(user_id: str, data_dir: str, args: list) -> str:
    """`/missed [n]` — the owner notices the daily cap SUPPRESSED (durable
    ``owner_notice`` rows the rail writes instead of dropping the text), newest
    first. 2026-08-28: 91 of 92 notices in 24h were capped and nothing let the
    owner read them."""
    try:
        n = max(1, min(20, int(args[0]))) if args else 5
    except (TypeError, ValueError):
        return "Usage: /missed [n] (1-20, default 5)"
    try:
        from core.runtime_paths import sidecar_db_path
        from core.sqlite_util import execute_retry
        path = (os.getenv("TELEMETRY_EVENT_LOG_PATH") or "").strip()
        if not path:
            local = os.path.join(data_dir, "telemetry_events.db")
            path = local if os.path.exists(local) else str(sidecar_db_path("telemetry_events.db"))
        if not os.path.exists(path):
            return f"Missed messages: unavailable (telemetry log not found at {path})"
        rows = execute_retry(
            path,
            "SELECT ts, attrs FROM telemetry_events WHERE kind='owner_notice' AND user_id=? "
            "AND attrs LIKE '%[suppressed by daily proactive-message cap%' "
            "ORDER BY ts DESC LIMIT ?", (user_id, n), fetch="all") or []
    except Exception as e:
        return f"Missed messages: unavailable ({type(e).__name__}: {str(e)[:120]})"
    if not rows:
        return "No suppressed owner messages on record."
    import json as _json
    import time as _time
    lines = [f"Last {len(rows)} suppressed owner message(s) (newest first):"]
    for r in rows:
        try:
            text = str((_json.loads(r["attrs"]) or {}).get("text") or "")
        except Exception:
            text = ""
        # strip the rail's marker prefix "[suppressed by …; source=x] "
        if text.startswith("[") and "] " in text:
            text = text.split("] ", 1)[1]
        text = text.strip().replace("\n", " ")
        if len(text) > 240:
            text = text[:237] + "…"
        stamp = _time.strftime("%m-%d %H:%M", _time.gmtime(float(r["ts"])))
        lines.append(f"• {stamp}Z — {text}")
    lines.append("Raise the cap: /config set delivery.daily_cap N")
    return "\n".join(lines)


#: Entry lines one chat `/recap` may render before rolling the rest into a count.
_RECAP_CHAT_LIMIT = 15


def _recap_reply(user_id: str, data_dir: str, args: list) -> str:
    """owner-UX P4 T2: `/recap [window]` (default 24h) over core.recap (T1).

    Bounded for chat (G14): an unbounded `/recap 7d` renders every event,
    episode and authored skill one line each — several phone messages of
    scrollback. The terminal keeps the full listing.
    """
    from core.recap import build_recap, format_recap_markdown
    window = args[0] if args else "24h"
    try:
        entries = build_recap(user_id, data_dir, window=window)
    except ValueError:
        return (f"Invalid recap window {window!r} — expected e.g. '30m' / '24h' / '7d' "
                "(a bare number of seconds also works).")
    return format_recap_markdown(entries, window, limit=_RECAP_CHAT_LIMIT)


def _goals_reply(user_id: str, data_dir: str, board: Optional[Any] = None) -> str:
    """owner-UX P4 T2: goal board summary — counts by status + up to 5 most
    recent open/running goals.

    ``board`` lets a caller share an already-open ``GoalBoard`` (see
    ``_status_reply``'s docstring); a bare call without one opens its own.
    """
    from agents.task.goals.board import (
        STATUS_READY, STATUS_RUNNING, STATUS_TRIAGE, GoalBoard)
    gb = board if board is not None else GoalBoard(os.path.join(data_dir, "goals.db"))
    # Counts over EVERY row and a newest-first open list, both in SQL. The old
    # ``gb.list(limit=1000)`` scan is a ``priority DESC`` window that evicts the
    # newest low-priority rows (the manifest stream legs) first once the board
    # outgrows the limit — the exact eviction the agent's goal_list hit at 100
    # rows on 2026-08-29.
    counts = gb.status_counts(user_id=user_id)
    total = sum(counts.values())
    if not total:
        return "No goals yet."
    lines = [f"{total} goal(s): " +
            ", ".join(f"{status}={n}" for status, n in sorted(counts.items()))]
    open_states = (STATUS_TRIAGE, STATUS_READY, STATUS_RUNNING)
    recent = gb.list_recent(user_id=user_id, statuses=open_states, limit=5)
    if recent:
        lines.append("Recent open/running:")
        for g in recent:
            lines.append(f"• {g.id[:8]} [{g.status}] {g.title}")
    return "\n".join(lines)


def _is_configured_source(source: str) -> bool:
    """True when a preference's effective value came from real configuration.

    ``built-in`` / ``default(...)`` mean nobody set it — those rows are the bulk
    of the listing and carry no information the owner asked for.
    """
    s = str(source or "")
    return not (s == "built-in" or s.startswith("default"))


def _prefs_reply(user_id: str, data_dir: str, instance_id: str,
                 full: bool = False) -> str:
    """owner-UX P4 T2: read-only resolved-preferences summary via the display
    SSOT (`core.prefs.display_effective`) — never the raw file, always the
    effective (pref/env/merged) value + source.

    Bare `/prefs` shows only what is actually SET (G15): the full schema is 26
    keys across group headers, which is ~35 lines on a phone in answer to what
    is usually a one-key question. `/prefs all` (and `/config list`) keep the
    complete listing.
    """
    from core.prefs import PREF_SCHEMA, display_effective
    rows = []
    for key in sorted(PREF_SCHEMA):
        value, source = display_effective(key, user_id, data_dir, instance_id=instance_id)
        rows.append((key, value, source))
    if not full:
        configured = [r for r in rows if _is_configured_source(r[2])]
        if not configured:
            return ("No preferences set — everything is on its built-in default.\n"
                    "See them all with /prefs all; change one with "
                    "/config set <key> <value>.")
        lines = [f"Set preferences ({len(configured)} of {len(rows)}):"]
        lines += [f"  {key} = {value} ({source})" for key, value, source in configured]
        lines.append("")
        lines.append(f"The other {len(rows) - len(configured)} are on built-in defaults "
                     "— /prefs all shows everything.")
        return "\n".join(lines)
    by_group: dict = {}
    for key, value, source in rows:
        by_group.setdefault(key.split(".", 1)[0], []).append((key, value, source))
    lines = ["Your preferences (read-only):"]
    for group in sorted(by_group):
        lines.append(f"[{group}]")
        for key, value, source in by_group[group]:
            lines.append(f"  {key} = {value} ({source})")
    lines.append("")
    lines.append("tell me what to change — guarded changes arrive as /pending proposals")
    return "\n".join(lines)


def _config_reply(user_id: str, data_dir: str, instance_id: str, args: list) -> str:
    """owner-UX T10: `/config` — the read/write control-plane counterpart to
    the read-only `/prefs`.

    No args (or `list`) renders the SAME PREF_SCHEMA listing `/prefs` does —
    reuses :func:`_prefs_reply`, never a second PREF_SCHEMA loop.

    `set <key> <value>`:
      - unknown key -> names the valid PREF_SCHEMA groups (a closest-match hint
        across every namespace is the CLI/REPL's job — this stays terse);
      - SAFE key -> writes immediately via `core.prefs.write_preference` and
        confirms;
      - GUARDED key -> NEVER written directly from a bare Telegram message —
        queues a `core.prefs.propose_pref_change` proposal and points at
        /pending -> /approve (the same trust ladder the webview `confirm:true`
        PATCH uses; mirrors `cli/ui/commands/h_config.py`'s `--confirm` gate,
        except the confirm bypass itself — Telegram has no `--confirm` here).
    """
    if not args or args[0].lower() == "list":
        # `/config` and `/config list` keep the FULL schema listing — this is the
        # control plane, where seeing every writable key is the point.
        return _prefs_reply(user_id, data_dir, instance_id, full=True)
    sub = args[0].lower()
    if sub != "set":
        return ("Usage: /config [list] | /config set <key> <value>\n"
                "See /prefs for the full read-only listing.")
    rest = args[1:]
    if len(rest) < 2:
        return "Usage: /config set <key> <value>"
    key = rest[0]
    value = " ".join(rest[1:]).strip()
    from core.prefs import PREF_SCHEMA, SENSITIVITY_GUARDED, propose_pref_change, write_preference
    spec = PREF_SCHEMA.get(key)
    if spec is None:
        groups = sorted({k.split(".", 1)[0] for k in PREF_SCHEMA})
        return (f"Unknown preference key: {key!r} — valid groups: "
                f"{', '.join(groups)}. See /config for the full key list.")
    if spec.sensitivity == SENSITIVITY_GUARDED:
        ok, msg = propose_pref_change(user_id, key, value, data_dir, instance_id=instance_id)
        if not ok:
            return f"Failed: {msg}"
        return (f"'{key}' is guarded — queued for review (not written yet).\n"
                "See /pending, approve with /approve <id> (or /reject <id>).")
    ok, err = write_preference(data_dir, user_id, key, value, instance_id=instance_id)
    if not ok:
        return f"error: {err}"
    return f"Set {key} = {value} (applies: {spec.applies})."


async def _kb_reply(user_id: str, query: str) -> str:
    """QW-4 (proposal 021): owner KB read from the phone — the same
    ``modules.memory.registry.kb_search`` primitive ``polyrob kb search`` uses.
    Before this verb, "ingested into KB" was write-only theatre from the
    owner's seat (assessment 2026-07-19 §2 touchpoint 4)."""
    from agents.task.constants import AutonomyConfig
    if not AutonomyConfig.kb_enabled():
        return "Knowledge base is disabled (KB_ENABLED=off)."
    import modules.memory.registry as _reg
    try:
        result = await _reg.kb_search(query, user_id=user_id, limit=5)
    except Exception as e:
        return f"KB search failed: {e}"
    text = str(result or "").strip()
    if not text:
        return f"No results for {query!r}."
    return text[:3500] + ("…" if len(text) > 3500 else "")


async def _files_reply(user_id: str, args) -> str:
    """QW-4: recent run artifacts from the episode registry — the owner's file
    view over what background runs actually produced. Artifact rows may be
    JSON strings (older writes) or lists — both are handled."""
    try:
        n = max(1, min(int(args[0]), 30)) if args else 10
    except (TypeError, ValueError):
        n = 10
    import json
    import modules.memory.registry as _reg
    try:
        # window scales with the ask so /files 30 can actually find 30 distinct
        # files across episodes (review Minor #8)
        rows = await _reg.memory_recall_episodes(
            user_id=user_id, limit=max(30, min(n * 5, 100)), order="newest")
    except Exception as e:
        return f"Could not read episodes: {e}"
    seen: set = set()
    lines: list = []
    for r in rows or []:
        get = (r.get if isinstance(r, dict) else lambda k, d=None: getattr(r, k, d))
        arts = get("artifacts") or []
        if isinstance(arts, str):
            try:
                arts = json.loads(arts)
            except (ValueError, TypeError):
                arts = []
        sid = str(get("session_id") or "")
        for a in arts if isinstance(arts, list) else []:
            if not isinstance(a, dict) or not a.get("path"):
                continue
            path = str(a["path"])
            if path in seen:
                continue
            seen.add(path)
            size = a.get("bytes")
            size_s = (f" ({float(size) / 1024:.1f} KB)"
                      if isinstance(size, (int, float)) else "")
            # Backticked so the renderer emits <code>: a bare `report.md` is
            # auto-linked by Telegram as a Moldovan domain (G2).
            lines.append(f"• `{path}`{size_s}"
                         + (f" — session {sid[:8]}" if sid else ""))
            if len(lines) >= n:
                break
        if len(lines) >= n:
            break
    if not lines:
        return "No file artifacts recorded yet."
    out = ["Recent artifacts (newest first):"] + lines
    try:
        from core.surfaces.deep_link import webview_public_url
        base = webview_public_url()
        if base:
            out.append(f"Console: {base}")
    except Exception:
        pass
    return "\n".join(out)


def _pause_reply(data_dir: str, cmd: str, args: list) -> str:
    """`/pause`, `/halt` (alias) and `/resume` from the phone — thin over the
    ONE owner-admin pause API (031); the reply is the VERIFIED (read-back)
    state, never a checkmark on a write."""
    from core.surfaces.owner_admin import (pause_autonomy, render_pause_result,
                                           render_resume_result, resume_autonomy_scopes)
    from core.surfaces.owner_intent import parse_pause_args
    try:
        scopes, minutes = parse_pause_args([] if cmd == "/halt" else list(args))
    except ValueError as e:
        return f"⚠️ {e}"
    if cmd == "/resume":
        if minutes is not None:
            return "⚠️ /resume takes scopes only (no duration) — e.g. /resume trading"
        res = resume_autonomy_scopes(data_dir, scopes=None if scopes == ("all",) else scopes,
                                     via="telegram")
        return render_resume_result(res, halt_hint="/pause")
    res = pause_autonomy(data_dir, scopes=scopes, duration_minutes=minutes,
                         reason=f"{cmd} {' '.join(args)}".strip(), via="telegram")
    return render_pause_result(res, resume_hint="/resume")


def _mode_reply() -> str:
    """`/mode` — JUST the effective-posture card + how to change it (030 WS-E4,
    read-only v1).

    `/status` already embeds the card inside a long snapshot; `/mode` answers
    the single question "what may this instance do right now, and why" with
    nothing else around it. Writes stay on the CLI seat: `/config set` is
    prefs-only from chat, and mode/posture are env flags.
    """
    lines = ["Effective posture (all axes):"]
    try:
        from core.config_policy.posture_card import render_posture_card
        lines.extend(render_posture_card(prefix="• "))
    except Exception:
        logger.debug("telegram /mode posture card failed", exc_info=True)
        return ("Could not read the posture card — run `polyrob autonomy status` "
                "on the box.")
    lines.append("")
    lines.append("To change: `/config set` is prefs-only; mode/posture are env "
                 "flags — `polyrob autonomy on|off [--mode supervised|autonomous]` "
                 "(restart applies). Live pause: /pause and /resume.")
    return "\n".join(lines)


def _correspondent_decision(data_dir: str, target: str, user_id: str,
                            *, approve: bool) -> Optional[str]:
    """Approve a pending `<surface>:<address>` correspondent from chat.

    Returns None when TARGET is not a pending correspondent, so the caller falls
    through to the self-evolution proposal path (an id may legitimately contain
    a colon). 030 C3: the registry now has a real ``reject()`` (pending →
    expired tombstone, blocks a silent re-seed) — chat uses it, matching the
    webview Review page.
    """
    surface, _, address = target.partition(":")
    if not surface or not address:
        return None
    from core.surfaces.correspondents import CorrespondentRegistry
    registry = CorrespondentRegistry(os.path.join(data_dir, "correspondents.db"))
    try:
        pending = [r for r in registry.list(user_id=user_id)
                   if r.get("state") == "pending"
                   and f"{r['surface']}:{r['address']}" == target]
    except Exception as e:
        logger.warning("telegram correspondent decision: registry read failed: %s", e,
                       exc_info=True)
        return None
    if not pending:
        return None
    if not approve:
        try:
            ok = registry.reject(surface=surface, address=address, user_id=user_id)
        except Exception:
            ok = False
        if ok:
            return (f"🚫 Rejected {target} — the pending binding is tombstoned "
                    "and cannot silently re-seed.")
        return (f"'{target}' stays pending — a pending contact is already denied "
                "(their replies never reach me).")
    ok = registry.approve(surface=surface, address=address, user_id=user_id)
    if ok:
        return f"✅ Approved {target} — their replies now reach me as data."
    return f"Failed to approve {target} — see `polyrob owner approve {surface} {address}`."


async def _handle_owner_admin(task_agent: Any, result: InboundResult, cmd: str) -> str:
    """§7.1 missing hop + §7.2b: /pending /approve /reject /asks /fulfill.
    owner-UX P4 T2 adds the read-only /status /recap /goals /prefs verbs.

    Thin plumbing over the SAME primitives the `polyrob owner` CLI uses
    (core.self_evolution + GoalBoard.asks/fulfill_ask) so a phone-only headless
    owner can close the approve loop.
    """
    user_id = result.inbound.identity.user_id
    if not _is_admin_owner(user_id):
        return "🔒 Owner only."
    from core import self_evolution
    from core.instance import resolve_instance_id
    from agents.task.goals.board import ASK_OPEN, GoalBoard
    data_dir = _admin_data_dir(task_agent)
    instance_id = resolve_instance_id()
    args = result.inbound.text.strip().split()[1:]
    board = GoalBoard(os.path.join(data_dir, "goals.db"))

    if cmd == "/status":
        return await _status_reply(task_agent, user_id, result.decision.session_id,
                                   data_dir, board=board)

    if cmd == "/mode":  # 030 WS-E4: the posture card + how to change it — health first
        header = await _health_header(user_id, data_dir, task_agent)
        return "\n".join(header) + "\n" + _mode_reply()

    if cmd in ("/recap", "/journey"):  # one recap vocabulary across surfaces
        header = await _health_header(user_id, data_dir, task_agent, limit=3)
        return "\n".join(header) + "\n" + _recap_reply(user_id, data_dir, args)

    if cmd == "/missed":
        return _missed_reply(user_id, data_dir, args)

    if cmd == "/goals":
        return _pause_line(data_dir) + "\n" + _goals_reply(user_id, data_dir, board=board)

    if cmd == "/apps":
        from surfaces.telegram.apps_ops import apps_reply  # 032 durable app service
        return _pause_line(data_dir) + "\n" + apps_reply(user_id, data_dir, args)

    if cmd == "/prefs":
        return _prefs_reply(user_id, data_dir, instance_id,
                            full=bool(args) and args[0].lower() in ("all", "full"))

    if cmd == "/config":
        return _config_reply(user_id, data_dir, instance_id, args)

    if cmd == "/kb":
        if not args:
            return "Usage: /kb <query> — search the knowledge base"
        return await _kb_reply(user_id, " ".join(args))

    if cmd == "/files":
        return await _files_reply(user_id, args)

    if cmd == "/dev":
        # Owner ↔ on-host dev-loop rail (proposal 027 WS-1). Raw text, not
        # `args` — the loop should see exactly what the owner typed.
        from surfaces.telegram.dev_rail import perform_dev_command, strip_dev_prefix
        return await perform_dev_command(strip_dev_prefix(result.inbound.text))

    if cmd in ("/halt", "/resume", "/pause"):
        return _pause_reply(data_dir, cmd, args)

    # G13: the owner write verbs that used to exist only on the CLI seat. Thin
    # plumbing over the same primitives, kept in owner_ops so this file (already
    # god-file sized) does not grow another five handlers.
    if cmd in ("/cron", "/goal", "/wallet", "/invoices", "/settle"):
        from surfaces.telegram import owner_ops
        if cmd == "/cron":
            return owner_ops.cron_reply(user_id, data_dir, args)
        if cmd == "/goal":
            return owner_ops.goal_reply(user_id, data_dir, args, board=board)
        if cmd == "/wallet":
            return owner_ops.wallet_reply(args)
        if cmd == "/invoices":
            return await owner_ops.invoices_reply(user_id, args)
        return await owner_ops.settle_reply(user_id, args)

    if cmd == "/pending":
        from tools.controller.approval_queue import list_pending_tool_approvals
        from core.surfaces.correspondents import CorrespondentRegistry
        from core.surfaces.owner_admin import pending_correspondent_items
        items = self_evolution.list_pending(user_id, home_dir=data_dir,
                                            instance_id=instance_id)
        items = items + list_pending_tool_approvals(board, user_id)
        # Pending correspondent bindings belong here too. The CLI has always
        # aggregated all THREE queues; chat aggregated two, so a third party the
        # agent contacted stayed unroutable with no chat-visible trace for a
        # phone-only owner (chat-first review 2026-08-22, G10).
        items = items + pending_correspondent_items(
            CorrespondentRegistry(os.path.join(data_dir, "correspondents.db")), user_id)
        if not items:
            return "No pending proposals."
        lines = [f"{len(items)} pending proposal(s):"]
        for it in items:
            preview = (it.get("preview") or "").strip()
            if len(preview) > 160:
                preview = preview[:157] + "…"
            lines.append(f"• {it['kind']}:{it['id']} — {preview}")
        lines.append("Approve with /approve <id>, discard with /reject <id>.")
        return "\n".join(lines)

    if cmd in ("/approve", "/reject"):
        if not args:
            return f"Usage: {cmd} <id> (see /pending)"
        target = args[0]
        # Tool-approval asks (Task 9 / G-2) are namespaced `tap-<id>` so a bare
        # `/approve <id>` can dispatch WITHOUT an explicit kind — never confused
        # with a self-evolution proposal id.
        from tools.controller.approval_queue import decide_tool_approval, strip_tap_prefix
        if strip_tap_prefix(target) is not None:
            # 030 WS-E3: pass the live agent so an approval wakes the
            # originating session (resume-on-grant) instead of waiting for a
            # byte-identical retry to happen by luck.
            ok, msg = decide_tool_approval(board, target, user_id=user_id,
                                           approved=(cmd == "/approve"),
                                           task_agent=task_agent)
            return msg if ok else f"Failed: {msg}"
        # A correspondent item's id is `<surface>:<address>` — listing it in
        # /pending without a way to act on it would just move the dead end.
        if ":" in target:
            reply = _correspondent_decision(data_dir, target, user_id,
                                            approve=(cmd == "/approve"))
            if reply is not None:
                return reply
        items = self_evolution.list_pending(user_id, home_dir=data_dir,
                                            instance_id=instance_id)
        match = next((it for it in items if str(it["id"]) == target), None)
        if match is None:
            return f"No pending proposal '{target}' — see /pending."
        fn = self_evolution.promote if cmd == "/approve" else self_evolution.reject
        ok, msg = fn(match["kind"], match["id"], user_id=user_id,
                     home_dir=data_dir, instance_id=instance_id)
        return msg if ok else f"Failed: {msg}"

    if cmd == "/asks":
        # Tool-approval asks have their OWN dedicated surface (/pending +
        # /approve /reject, tap-<id>) — excluded here so a payment request
        # doesn't show twice under two different id shapes.
        rows = [a for a in board.asks(user_id=user_id, status=ASK_OPEN)
                if (a.payload or {}).get("ask_kind") != "tool_approval"]
        if not rows:
            return "No open asks — nothing is blocked on you."
        lines = [f"{len(rows)} open ask(s):"]
        for a in rows:
            blocks = (a.payload or {}).get("blocks_goal_ids", [])
            lines.append(f"• {a.id} — {a.title}"
                         + (f" (blocks {len(blocks)} goal(s))" if blocks else ""))
            if a.body:
                lines.append(f"   {a.body[:160]}")
        lines.append("Fulfilled one? /fulfill <id> unblocks its goals.")
        return "\n".join(lines)

    if cmd == "/fulfill":
        if not args:
            return "Usage: /fulfill <ask-id> (see /asks)"
        ok, unblocked = board.fulfill_ask(args[0], user_id=user_id)
        if not ok:
            return f"No open ask '{args[0]}' — see /asks."
        return f"✅ Ask fulfilled — {unblocked} goal(s) unblocked."

    from core.surfaces.outbound_allowlist import OutboundAllowlist
    allowlist = OutboundAllowlist(os.path.join(data_dir, "surfaces.db"))

    if cmd == "/allow":
        if len(args) < 2:
            return "Usage: /allow <surface> <target>"
        surface, target = args[0], args[1]
        allowlist.allow(user_id, surface, target)
        return f"✅ Allowed {surface}:{target}."

    if cmd == "/deny":
        if len(args) < 2:
            return "Usage: /deny <surface> <target>"
        surface, target = args[0], args[1]
        ok = allowlist.revoke(user_id, surface, target)
        if not ok:
            return f"No active allowlist entry {surface}:{target}."
        return f"✅ Denied {surface}:{target}."

    if cmd == "/allowlist":
        rows = allowlist.list(user_id)
        if not rows:
            return "No allowlist entries."
        lines = [f"{len(rows)} allowlist entr{'y' if len(rows) == 1 else 'ies'}:"]
        for r in rows:
            note = f" ({r['note']})" if r["note"] else ""
            lines.append(f"• {r['status']} {r['surface']}:{r['target']}{note}")
        return "\n".join(lines)

    return _help_text()


async def _handle_command(task_agent: Any, result: InboundResult, spawn, deliver=None) -> Optional[str]:
    cmd = (result.decision.command or "").lower()
    if cmd == "/help":
        args = (result.inbound.text or "").split()[1:]
        if args:
            return _help_for(args[0])
        return _help_text()
    if cmd == "/start":
        return _welcome_text()
    if cmd in _OWNER_ADMIN_COMMANDS:
        try:
            return await _handle_owner_admin(task_agent, result, cmd)
        except Exception as e:
            logger.error("owner admin command %s failed: %s", cmd, e, exc_info=True)
            # 030 C-14: cap + soften — raw exception text can carry absolute
            # paths/DB errors/provider bodies, and it is unbounded.
            return f"Command failed: {str(e)[:200]} (details in the server log)"
    if cmd == "/cancel":
        sid = result.decision.session_id
        if sid:
            try:
                await task_agent.cancel_session_by_id(sid, force=True)
            except Exception as e:
                logger.debug("telegram /cancel failed: %s", e)
            return "Task cancelled."
        return "No active task to cancel."
    if cmd == "/new":
        sid = result.decision.session_id
        if sid:
            try:
                await task_agent.cancel_session_by_id(sid, force=True)
            except Exception as e:
                logger.debug("telegram /new cancel failed: %s", e)
        # a4: drop the chat<->session binding so the NEXT message routes cold (a fresh
        # session) instead of STEERing back into the just-cancelled thread.
        try:
            task_agent.unbind_chat(result.decision.session_key)
        except Exception as e:
            logger.debug("telegram /new unbind failed: %s", e)
        return "Started fresh — send your next message to begin."
    if cmd == "/task":
        # /task <goal>: strip the verb and start a new task with the remainder.
        text = result.inbound.text.strip()
        goal = text[len("/task"):].strip()
        if not goal:
            return "Usage: /task <what you want done>"
        result.inbound.text = goal
        await _start_task_session(task_agent, result, spawn, deliver)
        return None
    return _unknown_command_text(cmd)  # unknown command -> suggestion + /help (030 L9)


async def act_on_inbound(
    task_agent: Any,
    result: InboundResult,
    *,
    spawn: Optional[Callable[[Any], Any]] = None,
    deliver: Optional[Callable[[str], Any]] = None,
) -> Optional[str]:
    """Execute a routing decision. Returns an optional immediate user-facing reply
    (e.g. command acks); streamed/discrete agent output flows out via the surface.

    ``deliver`` is an async ``send(text)`` callable that delivers an agent turn's final
    reply to the chat (proposal 004). It is threaded into the spawned run so a STEER
    resume / fresh session actually answers the owner instead of discarding the reply.

    B6 (2026-07-13 review): execution is serialized per ``decision.session_key`` —
    the polling surfaces (telegram/discord/slack/signal/x/email share this handler)
    had no per-chat lock, so two rapid messages to a cold chat both routed
    TASK_AGENT and raced create_session for the single session_chat binding,
    orphaning one session. Distinct chats still run concurrently. (The webhook
    surface holds its own KeyedLock upstream; nesting is safe — consistent order,
    per-key granularity.)
    """
    # T1.4 alias-safe lease: aliased routing keys (e.g. two thread-suffixed keys
    # for one correspondent) can resolve to ONE session; lock on the resolved
    # session so the resolve/create/inject phase serializes per session. Cold
    # paths (no resolved session yet) keep the chat routing key — byte-identical
    # to the B6 cold-create protection. "sid:" prefix keeps the two bucket
    # namespaces disjoint.
    _sid = getattr(result.decision, "session_id", None)
    key = f"sid:{_sid}" if _sid else (getattr(result.decision, "session_key", "") or "")
    async with _INBOUND_LOCK.for_key(key):
        return await _act_on_inbound_locked(task_agent, result, spawn=spawn,
                                            deliver=deliver)


async def _act_on_inbound_locked(
    task_agent: Any,
    result: InboundResult,
    *,
    spawn: Optional[Callable[[Any], Any]] = None,
    deliver: Optional[Callable[[str], Any]] = None,
) -> Optional[str]:
    decision = result.decision
    kind = decision.kind

    if kind == RouteKind.DENIED:
        # Ingress blocked (pairing required / not paired). NEVER run the agent on a
        # denied message — return a user-facing message (with the pairing code, if any)
        # instead of falling through to _start_task_session.
        if getattr(decision, "silent", False):
            # W3 group denials are silent: never spam a channel with auth notices.
            return None
        if decision.pairing_code:
            return (
                "🔒 You're not authorized to use this bot yet.\n"
                f"Pairing code: {decision.pairing_code}\n"
                "Ask the operator to approve it."
            )
        return "🔒 You're not authorized to use this bot."

    if kind == RouteKind.CORRESPONDENT_DATA:
        # WS-A: a third party the agent contacted replied. Their text is DATA delivered
        # ONLY to the originating session (never a steer/command/new-session). Use the
        # sender's external address as the untrusted source label.
        src = result.inbound.identity.raw_user_id or result.inbound.identity.user_id
        try:
            # message_id (email: RFC Message-ID = idempotency key) feeds the durable
            # conversation log so OUR reply can set In-Reply-To (E1/A3).
            await task_agent.deliver_correspondent_data(
                decision.session_id, src, result.inbound.text,
                metadata={"message_id": result.inbound.idempotency_key or ""},
                surface=getattr(result.inbound.identity.source, "surface_id", None),
            )
        except Exception as e:
            logger.debug("correspondent delivery failed: %s", e)
        return None

    if kind == RouteKind.COMMAND:
        return await _handle_command(task_agent, result, spawn, deliver)

    if kind == RouteKind.STEER:
        # Deliver into the BOUND session — resident OR recreated-from-disk (which
        # restores the full message_history.json). This unifies the resident and
        # warm-but-dead cases: BOTH resume `decision.session_id`, never minting a new
        # amnesiac session. Then re-run it so the queued message is processed
        # (run_session is concurrent-resume safe: a no-op if a loop is already running).
        status = "gone"
        try:
            status = await task_agent.ensure_session_and_deliver(
                result.inbound.identity.user_id, decision.session_id,
                result.inbound.text, kind="comment",
            )
        except Exception as e:
            logger.debug("telegram STEER deliver failed: %s", e)
        # Normalize a legacy bool return (older TaskAgent) to the status vocabulary.
        if status is True:
            status = "delivered"
        elif status is False:
            status = "gone"

        if status in ("delivered", "busy"):
            # The session is alive (a-MED2: 'busy' = queue full but processing). Bump
            # last-activity on this success path — keeps route_inbound a side-effect-free
            # decision table (a1). The user is active either way.
            try:
                task_agent.touch_chat_binding(decision.session_key)
            except Exception as e:
                logger.debug("telegram STEER touch_chat_binding failed: %s", e)

        if status == "delivered":
            _spawn(_run_and_deliver(task_agent, result.inbound.identity.user_id,
                                    decision.session_id, deliver,
                                    notice_key=decision.session_key), spawn)
            return None
        if status == "busy":
            # The queue is full, so THIS message was rejected (not enqueued) — be honest
            # rather than implying it'll be handled next. Don't double-spawn run_session
            # (one is already draining) and don't mint a fresh amnesiac session.
            # 031: a SCOPED stop ("stop trading") that could not even be queued must
            # not evaporate — pause everything as the safe default and say so.
            if _is_admin_owner(result.inbound.identity.user_id):
                from core.surfaces.owner_intent import owner_stop_intent
                from surfaces.telegram.owner_intent_gate import handle_owner_intent
                _intent = owner_stop_intent(result.inbound.text)
                if _intent is not None and _intent.kind == "scoped":
                    try:
                        _reply = await handle_owner_intent(
                            task_agent, result, _intent, _admin_data_dir(task_agent), busy=True)
                        if _reply:
                            return _reply
                    except Exception as e:
                        logger.error("telegram busy-branch pause failed: %s", e, exc_info=True)
                        return (f"⚠️ Busy, and the safe-default pause failed "
                                f"({type(e).__name__}: {str(e)[:120]}). Send /pause to force it.")
            return ("⏳ I'm still working through your earlier messages and can't take this "
                    "one yet — please send it again in a moment.")
        # status == "gone": truly gone (no on-disk metadata) -> a fresh session.
        await _start_task_session(task_agent, result, spawn, deliver)
        return None

    # TASK_AGENT and CHAT_FASTPATH (MVP) -> start/continue a task session.
    await _start_task_session(task_agent, result, spawn, deliver)
    return None


# --- live harness (aiogram Bot; webhook OR local polling) --------------------


def _tg_message(update: dict) -> dict:
    return update.get("message") or update.get("edited_message") or {}


def _tg_user_id(update: dict) -> Optional[str]:
    """Raw Telegram numeric sender id (str) from an update, or None."""
    frm = (_tg_message(update).get("from") or {})
    uid = frm.get("id")
    return str(uid) if uid is not None else None


def _tg_chat_id(update: dict) -> Optional[str]:
    chat = (_tg_message(update).get("chat") or {})
    cid = chat.get("id")
    return str(cid) if cid is not None else None


class TelegramHarness:
    """Owns the aiogram Bot + the inbound handler for one shared bot.

    Inbound does NOT use aiogram's Dispatcher routing — our own dispatcher
    (process_update -> route_inbound) decides everything; aiogram is only the
    transport for outbound send + update delivery. The Bot is injected so this is
    testable with a fake.

    Transport: polling (run_polling() long-polls getUpdates and feeds each
    update to handle_update) — the ONLY wired transport today; both production
    call sites pass webhook_base=None. 030 C-7 honesty note: the webhook branch
    (start() would call set_webhook) is RESERVED — no FastAPI route exists for
    it, so setting webhook_base would break inbound until a route body calling
    handle_update is mounted (api/webhooks.py serves WebhookSurface impls only).
    """

    def __init__(self, bot, container, task_agent, *, webhook_base, dedup, user_directory,
                 poll_timeout: int = 30, typing_interval: float = 4.0):
        self.bot = bot
        self.container = container
        self.task_agent = task_agent
        self.webhook_base = webhook_base
        self.dedup = dedup
        self.user_directory = user_directory
        self.poll_timeout = poll_timeout
        self.typing_interval = typing_interval
        self._running = False
        self._bootstrap_replied: set = set()  # 030 C-9: one bootstrap reply per sender
        self.bot_username: Optional[str] = None
        from surfaces.telegram.surface import TelegramSurface
        self.surface = TelegramSurface(bot)

    async def _transcribe_voice(self, update: dict):
        """Injected into process_update so the inbound spine stays transport-free (#9):
        download a voice/audio attachment and transcribe it via the shared engine.
        No-op (returns None) unless VOICE_TRANSCRIPTION_ENABLED. Fail-open.

        The transcriber is now built via get_transcriber(container) — registered once on
        the container and shared across surfaces (Task 1.6 core-seam migration)."""
        from core.surfaces.config import SurfaceConfig
        if not SurfaceConfig.voice_transcription_enabled():
            return None
        try:
            from surfaces.telegram.voice import extract_voice_file_id, transcribe_telegram_voice
            # Only resolve the (heavy) transcriber when the update actually carries audio —
            # a text message must not trigger a model/import load.
            if not extract_voice_file_id(update):
                return None
            from core.surfaces.transcription import get_transcriber
            transcriber = get_transcriber(self.container)
            return await transcribe_telegram_voice(self.bot, update, transcriber)
        except Exception as e:
            logger.debug("telegram _transcribe_voice failed: %s", e)
            return None

    async def _typing_keepalive(self, chat_id: str) -> None:
        """Keep Telegram's 'typing…' indicator alive while an agent turn runs.

        Telegram shows the action for ~5s, so we refresh every typing_interval.
        Runs as a background task; cancelled when the turn's run_session completes.
        """
        import asyncio
        try:
            while True:
                # Refresh AFTER the interval; the immediate first action is sent by the
                # caller so a short turn still shows 'typing…' at least once.
                await asyncio.sleep(self.typing_interval)
                try:
                    await self.bot.send_chat_action(chat_id, "typing")
                except Exception as e:
                    logger.debug("telegram send_chat_action failed: %s", e)
        except asyncio.CancelledError:
            pass

    async def start(self) -> None:
        from core.surfaces.registry import register_surface
        from core.surfaces.transcription import log_transcription_readiness
        register_surface(self.container, self.surface)
        log_transcription_readiness(self.container)
        try:
            me = await self.bot.get_me()
            username = getattr(me, "username", None)
            if username:
                self.bot_username = username
                self.surface.bot_username = username
        except Exception as e:  # fail-open: group-mention detection and the
            # own-handle owner-alias (message_send.py) just stay inert, same
            # as today, if getMe() is unavailable (e.g. a test double Bot).
            logger.debug("telegram get_me (bot_username resolve) failed: %s", e)
        await self._publish_command_menu()
        if self.webhook_base:
            url = self.webhook_base.rstrip("/") + derive_webhook_path()
            await self.bot.set_webhook(url)
            logger.info("telegram webhook set: %s", url)
        else:
            # Polling mode: clear any stale webhook so getUpdates is allowed.
            try:
                await self.bot.delete_webhook()
            except Exception as e:
                logger.debug("telegram delete_webhook (poll start) failed: %s", e)

    async def _publish_command_menu(self) -> None:
        """Register the verb list with Telegram so the phone gets a "/" menu.

        Without this there is no command menu, no autocomplete and no
        descriptions on mobile — the owner has to remember 21 verbs or type
        /help and scroll (chat-first review 2026-08-22, G12). Sourced from the
        `_HELP_BODY` SSOT, so the menu cannot drift from the help text.
        Fail-open: a bot without setMyCommands (or a test double) is unaffected.
        """
        try:
            from aiogram.types import BotCommand
        except Exception:
            return  # aiogram absent (tests inject a fake bot) — nothing to publish
        setter = getattr(self.bot, "set_my_commands", None)
        if setter is None:
            return
        try:
            commands = [BotCommand(command=name, description=desc)
                        for name, desc in help_commands()]
            if commands:
                await setter(commands)
                logger.info("telegram command menu published (%d verbs)", len(commands))
        except Exception as e:
            logger.debug("telegram set_my_commands failed: %s", e)

    async def stop(self) -> None:
        self._running = False
        try:
            await self.bot.delete_webhook()
        except Exception as e:
            logger.debug("telegram delete_webhook failed: %s", e)
        try:
            session = getattr(self.bot, "session", None)
            if session is not None and hasattr(session, "close"):
                await session.close()
        except Exception as e:
            logger.debug("telegram bot session close failed: %s", e)

    async def run_polling(self) -> None:
        """Long-poll getUpdates and dispatch each update. Exits when stop() is
        called (self._running False) or the task is cancelled."""
        import asyncio
        self._running = True
        offset = None
        while self._running:
            try:
                updates = await self.bot.get_updates(offset=offset, timeout=self.poll_timeout)
            except asyncio.CancelledError:
                break
            except Exception as e:  # fail-open: a transient getUpdates error must not kill the loop
                # A getUpdates CONFLICT means another bot instance is long-polling the
                # SAME token (only one may). Retrying every second + dumping a full
                # traceback each time floods the journal and changes nothing — so log
                # one concise warning and back off longer. Other transient errors keep
                # the fast retry + traceback.
                if _is_conflict_error(e):
                    logger.warning(
                        "telegram get_updates conflict: another instance is long-polling "
                        "this bot token (only one may) — backing off %ss",
                        _CONFLICT_BACKOFF_SEC,
                    )
                    await asyncio.sleep(_CONFLICT_BACKOFF_SEC)
                else:
                    logger.error("telegram get_updates failed: %s", e, exc_info=True)
                    await asyncio.sleep(1)
                continue
            for u in updates:
                # aiogram returns Update models; tests inject raw dicts.
                data = u.model_dump(by_alias=True, exclude_none=True) if hasattr(u, "model_dump") else u
                uid = data.get("update_id")
                if uid is not None:
                    offset = uid + 1
                await self.handle_update(data)

    def _make_progress_reporter(self, chat_id):
        """An EditingProgressReporter bound to this chat over the aiogram Bot, or a
        NullProgressReporter when there's no chat to post to. Telegram supports editing,
        so the status bubble is sent once then edited through stages and deleted."""
        from core.surfaces.progress import EditingProgressReporter, NullProgressReporter
        if not chat_id:
            return NullProgressReporter()

        async def _send(text):
            sent = await self.bot.send_message(chat_id, text)
            return getattr(sent, "message_id", None)

        async def _edit(mid, text):
            await self.bot.edit_message_text(text=text, chat_id=chat_id, message_id=mid)

        async def _delete(mid):
            await self.bot.delete_message(chat_id, mid)

        return EditingProgressReporter(_send, _edit, _delete, supports_edit=True)

    def _progress_edits_enabled(self, user_id: str) -> bool:
        """TELEGRAM_PROGRESS_EDITS env default, per-owner progress.telegram pref."""
        try:
            from core.config_policy import AutonomyConfig
            env_value = AutonomyConfig.telegram_progress_edits()
            from core import prefs
            from core.runtime_paths import data_dir_or_home
            return bool(prefs.resolve(
                "progress.telegram", user_id, data_dir_or_home(None),
                env_value=env_value, default=env_value,
            ))
        except Exception:
            return True  # fail-open to the fix (flag default is ON)

    def _maybe_start_progress_tracker(self, reporter: Any, result: Any) -> Optional[Any]:
        """019 P2: attach a live TurnProgressTracker for this turn (or None).

        The tracker drives the existing progress bubble from the session's feed
        events. For a STEER turn the session id is known up front; a fresh chat
        turn binds lazily via the session_chat_registry reverse lookup once the
        new session starts emitting. Fail-open: any error → legacy static bubble.
        """
        try:
            user_id = result.inbound.identity.user_id or ""
            if not self._progress_edits_enabled(user_id):
                return None
            from agents.task.telemetry.live_progress import (
                TurnProgressTracker,
                attach_tracker,
            )

            def _resolver(sid: str) -> Optional[str]:
                try:
                    container = getattr(self.task_agent, "container", None)
                    registry = container.get_service("session_chat_registry") if container else None
                    if registry is None or not hasattr(registry, "resolve_by_session_id"):
                        return None
                    row = registry.resolve_by_session_id(sid)
                    return (row or {}).get("session_key")
                except Exception:
                    return None

            decision = result.decision
            tracker = TurnProgressTracker(
                reporter,
                session_key=getattr(decision, "session_key", "") or "",
                session_id=getattr(decision, "session_id", None),
                key_resolver=_resolver,
            )
            attach_tracker(tracker)
            return tracker
        except Exception as e:
            logger.debug("progress tracker start failed: %s", e)
            return None

    async def handle_update(self, update: dict) -> dict:
        """Process one raw Telegram update. Always returns {"ok": True} so a webhook
        gets a fast 200; errors are swallowed (fail-open)."""
        try:
            # Owner-allowlist gate (raw Telegram id), BEFORE any side-effecting step.
            tg_id = _tg_user_id(update)
            if tg_id is not None:
                gate = owner_allowed(tg_id)
                if gate is False:
                    return {"ok": True}  # not on the allowlist -> silently ignore
                if gate is None:
                    # No allowlist set: reveal the sender's id so the operator can lock
                    # the bot, and do NOT run the agent (bootstrap mode).
                    # 030 C-9: at most ONE reply per sender per process — this ran
                    # pre-dedup, so a Telegram redelivery (or any stranger's every
                    # message) re-sent it: unbounded reply amplification.
                    chat_id = _tg_chat_id(update)
                    if chat_id is not None and tg_id not in self._bootstrap_replied:
                        self._bootstrap_replied.add(tg_id)
                        if len(self._bootstrap_replied) > 1000:  # bound memory
                            self._bootstrap_replied.clear()
                        await self.bot.send_message(
                            chat_id,
                            "🔓 This bot has no allowlist set, so it is locked by default.\n"
                            f"Your Telegram user ID is: {tg_id}\n"
                            f"Set ALLOWED_TELEGRAM_USER_IDS={tg_id} and restart to use it.",
                        )
                    return {"ok": True}

            import asyncio
            from surfaces.telegram.inbound import process_update
            from surfaces.telegram.surface import chat_id_from_session_key
            from surfaces.telegram.voice import extract_voice_file_id
            from core.surfaces.progress import ProgressStage

            chat_id = _tg_chat_id(update)
            reporter = self._make_progress_reporter(chat_id)

            # Voice: show '🎤 Transcribing…' BEFORE process_update runs the (heavy)
            # transcription. Gate on a NON-mutating dedup peek so a redelivered voice
            # update (which process_update will dedup to None) doesn't post an orphan
            # status bubble; the authoritative claim still happens inside process_update.
            update_id = update.get("update_id")
            if extract_voice_file_id(update) is not None and not (
                update_id is not None and self.dedup.peek(update_id)
            ):
                await reporter.stage(ProgressStage.TRANSCRIBING)

            result = await process_update(
                self.container, update,
                dedup=self.dedup, user_directory=self.user_directory,
                transcribe_voice=self._transcribe_voice,
                bot_username=getattr(self, "bot_username", None),
            )
            if result is None:
                await reporter.finish()   # clear a TRANSCRIBING that slipped through
                return {"ok": True}

            # Trace the routed turn (visible in the journal on the headless service) so a
            # 'voice ran on empty context' bug is diagnosable: what text actually routed?
            try:
                logger.info(
                    "telegram inbound routed: voice=%s kind=%s session=%s text=%r",
                    extract_voice_file_id(update) is not None,
                    getattr(result.decision, "kind", None),
                    getattr(result.decision, "session_id", None),
                    (result.inbound.text or "")[:120],
                )
            except Exception:
                pass

            # Voice guard: a voice/audio note that produced no transcript (transcription
            # off, or faster-whisper not installed) would otherwise route an EMPTY turn —
            # which reads as a confused generic reply. Tell the user instead and DON'T run
            # the agent. Clear the status bubble first.
            # Uses the core seam (Task 1.6): inbound.media carries the voice Media set by
            # build_inbound_message, so the guard no longer inspects the raw update dict.
            if _core_vg.voice_needs_guard(result.inbound.media, result.inbound.text):
                await reporter.finish()
                from core.surfaces.config import SurfaceConfig
                guard = _core_vg.voice_unavailable_message(SurfaceConfig.voice_transcription_enabled())
                if chat_id:
                    try:
                        await self.bot.send_message(chat_id, guard)
                    except Exception as e:
                        logger.debug("telegram voice-guard reply failed: %s", e)
                return {"ok": True}

            # Empty-content guard: an inbound with NO text and NO voice is noise — a
            # Telegram edited_message / reaction / metadata edit that build_inbound_message
            # still turns into a routable message with text="". Routing it dispatches an
            # EMPTY-context turn (create_session with task="" -> a confused 'What do you
            # need?' reply, and a junk session). Drop it. (Empty VOICE is already handled
            # by the voice guard above; this covers the non-voice empty case.)
            if not (result.inbound.text or "").strip():
                logger.info("telegram inbound: empty non-voice content — dropping (no dispatch)")
                await reporter.finish()
                return {"ok": True}

            # 031: deterministic owner stop/resume — BEFORE any model call, any
            # queue, any tool. A full stop/resume is applied here from the text
            # alone (voice included) and confirmed from the read-back state.
            if (_is_admin_owner(result.inbound.identity.user_id)
                    and getattr(result.decision, "kind", None) != RouteKind.COMMAND):
                from core.surfaces.owner_admin import owner_pause_phrases
                from core.surfaces.owner_intent import owner_stop_intent
                from surfaces.telegram.owner_intent_gate import handle_owner_intent
                _dd = _admin_data_dir(self.task_agent)
                _intent = owner_stop_intent(
                    result.inbound.text,
                    extra_phrases=owner_pause_phrases(result.inbound.identity.user_id, _dd))
                _gate_reply = None
                if _intent is not None:
                    try:
                        _gate_reply = await handle_owner_intent(self.task_agent, result, _intent, _dd)
                    except Exception as e:
                        # Never fall through to the model on a stop: say it failed.
                        logger.error("telegram owner intent gate failed: %s", e, exc_info=True)
                        _gate_reply = (f"⚠️ I could not apply that automatically "
                                       f"({type(e).__name__}: {str(e)[:120]}). Send /pause to force it.")
                if _gate_reply:
                    await reporter.finish()
                    await _send_telegram_text(self.bot, chat_id, _gate_reply)
                    return {"ok": True}

            # Persistent transcript echo (voice only): post '🎙️ Transcript: …' quoting the
            # voice note so the user sees what ROB heard, BEFORE the answer. Fail-open —
            # never blocks the turn. Gated VOICE_TRANSCRIPT_ECHO (default ON).
            from core.surfaces.voice_echo import voice_transcript, voice_echo_message
            from core.surfaces.config import SurfaceConfig
            if SurfaceConfig.voice_transcript_echo_enabled():
                _t = voice_transcript(result.inbound.media)
                if _t and chat_id:
                    _vmid = (update.get("message") or {}).get("message_id")
                    _echo = voice_echo_message(_t)
                    try:
                        await self.bot.send_message(chat_id, _echo, reply_to_message_id=_vmid)
                    except Exception as e:
                        logger.debug("telegram transcript echo (quoted) failed: %s", e)
                        try:
                            await self.bot.send_message(chat_id, _echo)
                        except Exception as e2:
                            logger.debug("telegram transcript echo (fallback) failed: %s", e2)

            # Transcript good (or a text turn) -> '⚙️ Working…' for the turn's duration.
            await reporter.stage(ProgressStage.WORKING)

            # 019 P2: upgrade the static bubble to a live feed-driven status
            # line (current tool / step / wait state, throttled edits). Gated
            # by TELEGRAM_PROGRESS_EDITS + the progress.telegram pref;
            # fail-open to the legacy static bubble.
            tracker = self._maybe_start_progress_tracker(reporter, result)

            # Spawn agent turns with a 'typing…' keep-alive AND delete the status bubble
            # when the turn completes (run_session finishes after the answer is sent).
            # Commands that return an immediate reply don't spawn (handled below).
            spawned = {"v": False}

            def _spawn_with_typing(coro):
                spawned["v"] = True

                async def _wrapped():
                    typing = None
                    if chat_id:
                        # Immediate feedback: one typing action now, then keep-alive.
                        try:
                            await self.bot.send_chat_action(chat_id, "typing")
                        except Exception as e:
                            logger.debug("telegram initial send_chat_action failed: %s", e)
                        typing = asyncio.create_task(self._typing_keepalive(chat_id))
                    errored = False
                    try:
                        await coro
                    except Exception as e:
                        errored = True
                        logger.error("telegram agent turn failed: %s", e, exc_info=True)
                    finally:
                        if typing is not None:
                            typing.cancel()
                        if tracker is not None:
                            tracker.close()
                        await reporter.finish()   # delete '⚙️ Working…' when the turn ends
                    if errored and chat_id:
                        # Don't leave the user with a silent void (status gone, no answer).
                        try:
                            await self.bot.send_message(
                                chat_id,
                                "⚠️ Something went wrong handling that — please try again.",
                            )
                        except Exception as e:
                            logger.debug("telegram error-breadcrumb send failed: %s", e)

                asyncio.create_task(_wrapped())

            # 004: deliver an agent turn's final reply to the chat. The spawned run
            # (_run_and_deliver) extracts the real answer after run_session and calls this;
            # without it the interactive reply was discarded (owner saw silence).
            _deliver_chat_id = chat_id_from_session_key(result.decision.session_key)

            async def _deliver(text):
                if _deliver_chat_id:
                    await _send_telegram_text(self.bot, _deliver_chat_id, text)

            try:
                reply = await act_on_inbound(
                    self.task_agent, result, spawn=_spawn_with_typing, deliver=_deliver,
                )
            except Exception as e:
                # 019 review fix: an act_on_inbound raise (e.g. create_session
                # on exhausted credits) previously unwound past every cleanup —
                # leaking the progress tracker in the module registry forever
                # and orphaning the '⚙️ Working…' bubble, with no user feedback.
                logger.error("telegram act_on_inbound failed: %s", e, exc_info=True)
                if tracker is not None:
                    tracker.close()
                await reporter.finish()
                if chat_id:
                    try:
                        await self.bot.send_message(
                            chat_id,
                            "⚠️ Something went wrong handling that — please try again.",
                        )
                    except Exception as send_err:
                        logger.debug("telegram error-breadcrumb send failed: %s", send_err)
                return {"ok": True}
            if reply:
                # Immediate-reply branches (DENIED / COMMAND / busy) never spawn, so the
                # _wrapped finally never runs -> delete the status bubble here, BEFORE the
                # reply (so the user never sees status + answer stacked).
                if tracker is not None:
                    tracker.close()
                await reporter.finish()
                reply_chat_id = chat_id_from_session_key(result.decision.session_key)
                await _send_telegram_text(self.bot, reply_chat_id, reply)
            elif not spawned["v"]:
                # No reply AND nothing spawned (e.g. create_session yielded no id) -> the
                # finally will never run; clear the status bubble so it never orphans.
                if tracker is not None:
                    tracker.close()
                await reporter.finish()
        except Exception as e:  # fail-open: never raise into the transport
            logger.error("telegram handle_update failed: %s", e, exc_info=True)
        return {"ok": True}


class TelegramBotSink:
    """Minimal outbound sink wrapping a bot's ``send_message`` so out-of-band
    deliverers (e.g. ``cron/delivery.py`` proactive owner outreach) can push a
    message to a raw chat id without going through the session-binding router.

    Registered as the container service ``telegram_sink``. ``send_message`` is async
    (aiogram's Bot.send_message is a coroutine); cron delivery awaits it.

    ``media`` (QW-1, 2026-07-19): optional list of pre-validated attachment
    entries (``core.surfaces.attachments`` shape: kind/path/caption) sent as
    photos/documents ON TOP of the text — per-entry fail-open, a media fault
    never takes the delivered text down with it (mirrors
    ``TelegramSurface._send_media``).
    """

    def __init__(self, bot):
        self._bot = bot

    async def send_message(self, chat_id, text, media=None) -> bool:
        try:
            cid = int(chat_id) if str(chat_id).isdigit() else chat_id
            await _send_telegram_text(self._bot, cid, text)
        except Exception:  # fail-open: delivery must never crash the caller
            logger.warning("TelegramBotSink.send_message failed for chat %s", chat_id,
                           exc_info=True)
            return False
        if media:
            await self._send_media(cid, media)
        return True

    async def _send_media(self, chat_id, media: list) -> None:
        try:
            from aiogram.types import FSInputFile
        except ImportError:
            FSInputFile = None
        for entry in media:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not path or not (os.path.isfile(path) and os.access(path, os.R_OK)):
                logger.warning("TelegramBotSink: media path missing/unreadable, "
                               "skipping: %s", path)
                continue
            try:
                file = (FSInputFile(path, filename=os.path.basename(path))
                        if FSInputFile is not None else path)
                caption = entry.get("caption") or None
                if caption:
                    caption = str(caption)[:1024]  # Telegram caption hard cap
                if entry.get("kind") == "image":
                    await self._bot.send_photo(chat_id, file, caption=caption)
                else:
                    await self._bot.send_document(chat_id, file, caption=caption)
            except Exception as e:  # per-entry fail-open
                logger.warning("TelegramBotSink: failed to send media %s: %s", path, e)


def build_telegram_harness(container, task_agent, *, token, webhook_base=None, bot=None,
                           data_dir=None, poll_timeout: int = 30):
    """Assemble a TelegramHarness. Lazy-imports aiogram for the real Bot (tests inject
    a fake). Ensures a UserDirectory + UpdateDedup exist on the shared data dir.

    webhook_base=None -> polling mode (local). Pass a base URL for webhook mode.

    WS-3: an omitted data_dir resolves to the data home, never a relative "data".
    """
    from core.runtime_paths import data_dir_or_home
    data_dir = data_dir_or_home(data_dir)
    from surfaces.telegram.dedup import UpdateDedup
    from tools.user_directory import UserDirectory

    if bot is None:
        from aiogram import Bot  # lazy: only needed when actually starting the surface
        bot = Bot(token)

    dedup = UpdateDedup(os.path.join(data_dir, "tg_dedup.db"))
    user_directory = container.get_service("user_directory")
    if user_directory is None:
        user_directory = UserDirectory(os.path.join(data_dir, "users.db"))
        container.register_service("user_directory", user_directory)

    # Register the outbound sink so cron/delivery proactive outreach can find the bot.
    try:
        if container.get_service("telegram_sink") is None:
            container.register_service("telegram_sink", TelegramBotSink(bot))
    except Exception:
        pass

    return TelegramHarness(
        bot, container, task_agent,
        webhook_base=webhook_base, dedup=dedup, user_directory=user_directory,
        poll_timeout=poll_timeout,
    )


# R-4: register THE shared inbound dispatch with the core-owned contract so
# core.surfaces.inbound_webhook can delegate without importing the surface tier.
from core.surfaces.act import register_inbound_actor  # noqa: E402

register_inbound_actor(act_on_inbound)
