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

from core.surfaces.access_log import record_pre_route_drop
from core.surfaces.poll_health import record_poll_error
from core.surfaces.dispatcher import RouteKind
from core.surfaces import voice_guard as _core_vg
from core.surfaces.serialize import KeyedLock
from core.surfaces.tappable import parse_tappable as _parse_tappable
from surfaces.telegram.inbound import InboundResult
from surfaces.telegram.media import (
    absorb_for_session, queue_attachments_for_new_session,
)

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
# a flat wall of verbs hid the kill-switch between /fulfill and /resume.
_HELP_BODY = (
    "— Tasks —\n"
    "/task <goal> — start a new task\n"
    "/cancel — stop the current task\n"
    "/new — start a fresh conversation\n"
    "— Control —\n"
    "/pause [word…] [for 6h] — stop autonomous work now (everything, or: "
    "trading, background, messages, deploying — or a scope)\n"
    "/resume [word…] — lift the pause\n"
    "/halt — alias of /pause (everything)\n"
    "/status — health first, then session, goals, loops, delivery, posture, made, wallet, money\n"
    "/mode — effective autonomy posture (all axes) and how to change it\n"
    "/avatar — this instance's face, traits and voice signature (read-only)\n"
    "/missed [n] — owner messages the daily cap suppressed (default 5)\n"
    "— Approvals & asks —\n"
    "/inbox [n] — everything waiting on a decision from you, blocking first\n"
    "/pending — proposals I've learned, awaiting your approval (each one tappable)\n"
    "/approve — activate what's waiting; tap the token in /pending for a specific one\n"
    "/reject — discard it; /approve_all and /reject_all decide the whole queue\n"
    "/asks — what I need from you to unblock work\n"
    "/fulfill <id> — mark an ask fulfilled (unblocks its goals)\n"
    "— Autonomy —\n"
    "/cron [list|show|add|cancel] — durable scheduled runs\n"
    "/goal <show|ready|pause|resume|retry|cancel> <id> — steer one goal\n"
    "/goal objective <list|pause|activate|drop> [id] — steer a whole stream\n"
    "/goals — goal board summary\n"
    "/apps [list|show|approve|reject|kill|logs <slug>] — durable apps: approve an address, health, kill\n"
    "/mcp [add <id> <url>|remove <id>|test <id>] — MCP servers I can use "
    "(https only; loads in every new session)\n"
    "/recap [window] — what I've done (default 24h, e.g. 30m/24h/7d; alias /journey)\n"
    "/journey [window] — alias of /recap\n"
    "— Money —\n"
    "/book — my ledger against every money chain: one verdict, then what disagrees\n"
    "/wallet [balances] — addresses, network and spend caps\n"
    "/invoices [status] — what I've billed and who owes me\n"
    "/settle <id> [tx] — mark an invoice paid\n"
    "/trade <what to do> — launch a run that carries the money verb\n"
    "/bridge <from> <to> <amount> [go] — moves native value across chains "
    "within your caps; above the line it asks you first (quote only without 'go')\n"
    "/lp positions|pool|quote|add|remove|collect — liquidity (dry-run by default)\n"
    "/launch <SYMBOL> <name…> [logo <url>] [desc <text>] [buy <amount>] [go] — "
    "launch a token on the Pons launchpad (quote only without 'go')\n"
    "/deploy <SYMBOL> <supply> <name…> [on <chain>|solana] [vanity <hex>] "
    "[uri <url>] [go] — deploy a fixed-supply token (no mint function, no owner)\n"
    "/wallet autonomous <usd> — how much runs without asking you\n"
    "— Access —\n"
    "/allow <surface> <target> — allow me to message that target\n"
    "/deny <surface> <target> — revoke that permission\n"
    "/allowlist — show who I'm allowed to message\n"
    "— Rooms —\n"
    "/groups <allow|deny|list|mode|set|role|tail|service|admins> here|<surface> "
    "<chat_id> [...] — manage this instance's group-chat presence\n"
    "/mute here|<surface> <chat_id> <duration> — silence a room for a while "
    "(e.g. 2h); owner or that room's admin\n"
    "/mute <duration> IN REPLY to a message — mute that MEMBER; free for you "
    "and room admins, a paid action for a member (046)\n"
    "/ban <duration> IN REPLY to a message — ban that MEMBER for a while; free "
    "for you and room admins, a paid action for a member (046)\n"
    "/unmute IN REPLY to a message — end that member's mute early; the muted "
    "member may COUNTER-PAY their own\n"
    "/unban IN REPLY to a message — lift that member's ban; the banned member "
    "may COUNTER-PAY their own\n"
    "/paid <status|enable|disable|price <verb> <usd>|asset <id>|offers|"
    "cancel <id>> — configure this room's paid actions; owner or room admin\n"
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

_OWNER_ADMIN_COMMANDS = ("/inbox", "/book",
                         "/pending", "/approve", "/reject", "/asks", "/fulfill",
                         "/allow", "/deny", "/allowlist",
                         "/groups", "/mute",  # 044 T18: room presence admin
                         "/paid",             # 046: paid room actions
                         "/unmute", "/ban", "/unban",   # 046 phase 2
                         "/halt", "/resume", "/pause",
                         "/cron", "/goal", "/wallet", "/invoices", "/settle",
                         "/trade", "/bridge", "/launch", "/deploy", "/lp",
                         "/status", "/mode", "/recap", "/journey", "/goals", "/prefs", "/config",
                         "/avatar",
                         "/missed", "/apps", "/mcp",
                         "/kb", "/files", "/dev")

#: 044 I10 (spec §6.1 P9/P10): owner verbs that may NOT execute from inside a
#: room, whoever is watching. Money (`/trade /wallet /bridge /deploy /launch
#: /invoices /settle`), host and self-modification (`/dev`), autonomy control
#: (`/pause /halt /resume /approve /reject`), and access/configuration (`/allow
#: /deny /config /prefs /mcp /apps`). Redirecting the REPLY to the owner's DM was
#: never enough: the ACTION ran, so a member who talked the owner into typing one
#: got it, and the room is the one place a shoulder-surfer is guaranteed.
#:
#: What STAYS reachable from a room — read-only or room-scoped, and answered in
#: the owner's DM: `/status /help /groups /mute /paid /cancel /new /goals
#: /recap /journey /missed`. A verb in NEITHER list is unchanged (it was never
#: a room hazard; adding one here is a deliberate act).
#:
#: ⚠️ `/paid` (046) is deliberately ABSENT: it configures THIS room and nothing
#: else, its writes are the room's own overlay, and it names no money the agent
#: can move. Refusing it from the room would send an admin to a DM to configure
#: the room he is standing in.
_ROOM_REFUSED_COMMANDS = frozenset({
    "/trade", "/wallet", "/deploy", "/lp", "/launch", "/bridge", "/dev",
    "/pause", "/halt", "/resume", "/approve", "/reject",
    "/allow", "/deny", "/config", "/prefs", "/mcp", "/apps",
    "/invoices", "/settle",
})

#: 044 T1: the owner stop gate runs ONLY on a routed turn. DENIED (a silent
#: group denial), COMMAND (its own path) and CORRESPONDENT_DATA never qualify.
#: GROUP_TURN does (044 T14): the owner's steer in a ROOM routes GROUP_TURN
#: instead of STEER, and the deterministic stop gate must still run before any
#: model call. It is owner-gated at the call site, so a member's GROUP_TURN
#: never reaches it.
_INTENT_GATE_KINDS = frozenset({RouteKind.STEER, RouteKind.TASK_AGENT,
                                RouteKind.GROUP_TURN})

_TURN_KINDS = frozenset({RouteKind.STEER, RouteKind.TASK_AGENT,
                         RouteKind.CHAT_FASTPATH, RouteKind.CORRESPONDENT_DATA,
                         RouteKind.GROUP_TURN})


def _route_is_turn(decision) -> bool:
    """044 T3: only a routed TURN may produce a visible side effect (bubble, echo)."""
    return getattr(decision, "kind", None) in _TURN_KINDS


def help_commands() -> list:
    """``(command, description)`` pairs parsed from the ``_HELP_BODY`` SSOT.

    Feeds Telegram's ``setMyCommands`` so the phone gets a real "/" menu with
    autocomplete instead of the owner having to remember every verb. Sourced from
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


#: How many chats the "/" menu is published to at start. An allowlist is a
#: handful of ids; a pathological one must not turn startup into a Bot API
#: hammering loop.
_MENU_MAX_CHATS = 16


def _menu_chat_ids() -> list:
    """Chat ids allowed to SEE the "/" command menu — the owner seats, only.

    Every verb in the menu is owner-admin and gated by
    :func:`owner_allowed`, so the menu's audience is exactly the
    ``ALLOWED_TELEGRAM_USER_IDS`` allowlist plus an explicit
    ``POLYROB_OWNER_TELEGRAM_ID``. This is deliberately WIDER than
    ``core.instance.resolve_owner_telegram_id`` (which answers None on a
    two-entry allowlist because it must name ONE human): here a second
    allowlisted seat can run the verbs, so hiding the menu from it would be
    wrong. With no allowlist at all the bot is in bootstrap mode and runs
    nothing, so nobody gets a menu.
    """
    out = []
    explicit = (os.getenv("POLYROB_OWNER_TELEGRAM_ID") or "").strip()
    if explicit.isdigit():
        out.append(int(explicit))
    raw = (os.getenv("ALLOWED_TELEGRAM_USER_IDS") or "").strip()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in out:
            out.append(int(part))
    return out[:_MENU_MAX_CHATS]


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


def raw_allowlist_applies(update: dict) -> bool:
    """044 T7: the raw-id allowlist is the DM lock. A room message is governed by
    the group model in route_inbound (GROUP_CHAT_ENABLED + GroupAllowlist + role)."""
    return _tg_chat_type(update) == "private"


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


async def _deliver_owner_aware(task_agent: Any, notice_key: Optional[str], deliver,
                               text: str) -> None:
    """044 T2: route system/diagnostic text (LLM-outage notice, run-budget halt)
    through ``deliver`` normally; for a ROOM ``notice_key`` this is owner-only
    output, so it is redirected straight to the owner's Telegram DM (bypassing
    ``deliver``, which is bound to the room's own chat id) via the
    ``telegram_sink`` container service — never posted into the room. A DM key
    (or no key) is unchanged legacy behaviour. Fail-open throughout: an
    unresolvable owner or a missing sink drops the notice (logged), never
    raises into the caller."""
    if deliver is None:
        return
    from core.surfaces.room_keys import is_group_session_key, owner_only_reply_target
    if not (notice_key and is_group_session_key(notice_key)):
        await deliver(text)
        return
    from surfaces.telegram.surface import chat_id_from_session_key
    target = owner_only_reply_target(notice_key, chat_id_from_session_key(notice_key))
    if target is None:
        logger.warning("owner-only notice dropped: room key %s and no owner telegram id",
                       notice_key)
        return
    sink = None
    try:
        container = getattr(task_agent, "container", None)
        sink = container.get_service("telegram_sink") if container is not None else None
    except Exception:
        sink = None
    if sink is None:
        logger.warning("owner-only notice dropped: no telegram_sink service for room key %s",
                       notice_key)
        return
    try:
        await sink.send_message(target, text)
    except Exception as e:
        logger.error("telegram owner-only notice delivery failed: %s", e, exc_info=True)


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
    # A session freezes its toolset at CREATION, so a grant the owner makes later
    # never reaches the chat he is already sitting in — and a money tool can never
    # close that gap itself, because `load_tool` refuses the money set by design.
    # Live 2026-09-12: the owner armed the trading rail and his own chat still
    # could not bridge, with `/new` the undocumented only way out. Runs on EVERY
    # owner turn, fresh or continued; a non-owner is never touched. Fail-open.
    from core.surfaces.room_keys import is_group_session_key
    if not is_group_session_key(notice_key or ""):
        from surfaces.telegram.interactive_tools import reconcile_owner_toolset
        await reconcile_owner_toolset(task_agent, user_id, session_id)

    # 056 WS3: a headless owner/room turn HOLDS the shared workspace for its whole
    # run (busy depth + turn.active marker + cross-process lock, never refusing
    # the human). Until 2026-09-19 nothing marked these turns, so cron/goal runs
    # could start under the owner's live turn on the same project root.
    from core.interactive_gate import owner_turn
    _kind = "room_turn" if is_group_session_key(notice_key or "") else "owner_chat"
    with owner_turn(kind=_kind, session_id=session_id):
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
                await _deliver_owner_aware(task_agent, notice_key, deliver, f"⚠️ {status}")
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
        await _deliver_owner_aware(task_agent, notice_key, deliver, reply)
    except Exception as e:
        logger.error("telegram reply delivery failed: %s", e, exc_info=True)


def _room_reply_anchor(result: InboundResult) -> Optional[str]:
    """044 T10 fix round 1: the inbound message id a room turn should thread its
    reply to, or None outside a room chat. Shared by the STEER and fresh-session
    call sites so the extraction can't drift between them (finding #4 of the
    task-10/11 review). ``_tg_message`` is defined further down this module —
    a plain forward reference, resolved at call time."""
    from core.surfaces.room_keys import is_group_session_key
    if not is_group_session_key(result.decision.session_key):
        return None
    mid = (_tg_message(result.inbound.raw or {}) or {}).get("message_id")
    return str(mid) if mid is not None else None


#: 044 T14 (fix round 1, minor 7): the room-turn resolvers live in their own
#: module — this harness is already one of the tree's largest and new behaviour
#: belongs in a new file, not in it. Imported by NAME so the call sites read the
#: same as before the extraction.
from surfaces.telegram.room_turn import (  # noqa: E402
    attachment_description as _with_attachment,
    cold_start_request as _cold_start_request,
    mark_turn_answered as _mark_turn_answered,
    room_absorbs_media as _room_absorbs_media,
    room_session_owner as _room_session_owner,
    room_turn_text as _room_turn_text,
    session_owner_uid as _session_owner_uid,
)


async def _start_task_session(task_agent: Any, result: InboundResult, spawn, deliver=None,
                              fetch_media=None) -> None:
    """create_session with the binding kwargs, then run it AND deliver its reply."""
    inbound = result.inbound
    session_user_id = inbound.identity.user_id
    request_text = inbound.text
    room_turn = None
    if (inbound.identity.source.chat_type or "dm") != "dm":
        from core.surfaces.room_policy import room_tool_ids
        tool_ids = room_tool_ids()   # 044 T5: the audience bounds the power
        # 044 T14: a ROOM session belongs to the OWNER tenant whoever opened it.
        session_user_id = _room_session_owner(inbound.identity.user_id)
        # The session TASK is the framed addressed line ONLY. The context block
        # is pushed as an EPHEMERAL message once the session exists (below) —
        # folding it into `request` would store a room's recent chatter for the
        # life of the session, which is precisely what "the context block is
        # API-only" forbids (044 §4.4).
        room_turn, _role = _room_turn_text(task_agent, result)
        request_text = _cold_start_request(room_turn, _role)
    else:
        # OWNER interactive sessions get the introspection + mission toolset
        # (goal/twitter/web_fetch) so "review your goals" uses goal_list instead of
        # guessing from the sandbox filesystem. None for a non-owner -> the
        # conservative default stands.
        from surfaces.telegram.interactive_tools import owner_interactive_tool_ids
        tool_ids = owner_interactive_tool_ids(inbound.identity.user_id)
    info = await task_agent.create_session(
        session_user_id,
        request=request_text,
        session_source=inbound.identity.source,
        chat_session_key=result.decision.session_key,
        tool_ids=tool_ids,
    )
    session_id = info.get("id") if isinstance(info, dict) else getattr(info, "id", None)
    if session_id:
        if room_turn is not None:
            # The SAME rail the warm turn uses, so turn 1 is neither unframed nor
            # durably stored. ⚠️ `create_session` builds and initializes the
            # ORCHESTRATOR, not the agent — `create_agent` runs inside
            # `run_session` — so on a cold start this BUFFERS the block
            # (`_pending_room_context`) and `create_agent` flushes it as the
            # ephemeral. Fail-open: a dropped block costs the room's recent
            # lines, never the turn.
            # 044 I8: "presented = handled" needs the block to have been
            # PRESENTED. `push_room_context` is fail-open and returns False when
            # it could not place the block (no agent, no pending buffer) — and the
            # marking ran anyway, so a dropped block permanently retired lines the
            # model never saw. Mark only what was really shown; an unpushed block
            # leaves its lines unanswered for the next turn or the service run.
            if task_agent.push_room_context(session_id, room_turn.context):
                # Presented = handled: every line this turn was shown, plus the
                # line it answers. At dispatch, so a crashed turn cannot leave the
                # room re-asking the same lines on every later mention.
                _mark_turn_answered(task_agent, room_turn, session_id)
            else:
                logger.warning("room context not presented for %s — leaving its "
                               "lines unanswered (the addressed line still lands)",
                               session_id)
                _mark_turn_answered(task_agent, room_turn, session_id,
                                    include_shown=False)
        # 044 T10: a fresh room session also threads its reply to the message that
        # started it. No message has been drained yet for turn 1 — the seed task
        # bypasses the HITL queue entirely — so this is a direct, once-at-creation
        # set (create_session just registered the orchestrator, so it's resident by
        # now); the drain-based recompute (agent/core/user_ingress.py::
        # _drain_user_messages) takes over from the very next queued message
        # (attachment below, a later STEER, a self-wake, ...) and refreshes/clears
        # it from there — this is not a lingering direct poke.
        _anchor = _room_reply_anchor(result)
        if _anchor is not None:
            try:
                task_agent.set_turn_reply_to(session_id, _anchor)
            except Exception as e:
                _ctx = f"session={session_id} anchor={_anchor}"
                logger.warning("telegram set_turn_reply_to failed (%s): %s — "
                               "the queued-message metadata anchor below is "
                               "the fallback", _ctx, e)
        # Attachments are queued BEFORE the run starts — the same ordering the
        # console's upload path uses (api/task_http_api.py "queue message before
        # starting agent"), so the first step already sees the files. The anchor
        # rides on this message's metadata too (it's the FIRST thing the loop's
        # own initial drain will see, and without it that drain would clear the
        # anchor set above right back to None).
        # 044 §4.4 + the 2026-09-13 media rule: in a ROOM, bytes are absorbed for
        # an owner/admin turn only. A member's file is NAMED in the ledger line and
        # is never written into the owner tenant's workspace (which is what this
        # session's workspace now IS, whoever opened the room session).
        if _room_absorbs_media(result):
            await queue_attachments_for_new_session(
                task_agent, result, session_id, fetch_media,
                extra_metadata=({"reply_to": _anchor} if _anchor is not None else None),
            )
        _spawn(_run_and_deliver(task_agent, session_user_id, session_id, deliver,
                                notice_key=result.decision.session_key), spawn)


async def _send_photo_best_effort(task_agent: Any, result: Any, path: str) -> bool:
    """Send one image to the chat this command came from. True if it went.

    Reuses the SAME media rail the invoice card rides (``MessageRouter``) rather
    than reaching for the bot object — one outbound path, and the router already
    owns the parse-mode / caption / chunking rules.

    ⚠️ Best-effort by design. A command's answer is its TEXT; if no router is
    registered (a bare test rig, a surface without media_out) the verb must
    still answer, naming the file, instead of failing. Never raises.
    """
    try:
        container = getattr(task_agent, "container", None)
        router = container.get_service("message_router") if container else None
        if router is None:
            return False
        chat_id = getattr(getattr(result.inbound.identity, "source", None),
                          "chat_id", None)
        if not chat_id:
            return False
        await router.send_message(str(chat_id), "", surface_id="telegram",
                                  media=[{"kind": "image", "path": str(path),
                                          "caption": None}])
        return True
    except Exception:
        logger.debug("telegram: /avatar photo send failed (text still sent)",
                     exc_info=True)
        return False


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


#: The ONE denial string for "you may not act here" — shared by the DENIED
#: routing branch (`_act_on_inbound_locked`) and the lifecycle gate below, so
#: the wording can never drift between the two.
_UNAUTHORIZED_TEXT = "🔒 You're not authorized to use this bot."


def _lifecycle_permitted(result: InboundResult) -> bool:
    """Gate for the lifecycle verbs `/cancel` and `/new` (043 A10).

    A lifecycle verb is NOT owner-only — a permitted non-owner DM user must
    still be able to cancel or restart THEIR OWN session. Allow when the
    sender is the owner, OR the sender is in a DM (`chat_type == "dm"`) whose
    `decision.session_key` was built from the sender's OWN chat.

    A Telegram private chat is 1:1 with its sender, and
    `core/surfaces/session_chat_registry.py::build_session_key` embeds that in
    the DM shape `agent:main:{surface}:dm:{chat_id}:{user_id}` — so the key
    naming THIS sender's own chat AND this sender confirms the session is
    theirs to act on. See `_owns_dm_session_key`.

    044 T16: a room's session is shared by every member, so a member is still
    denied there — but a room ADMIN the owner promoted may cancel or restart the
    room's own session. The role is the one resolved at the routing boundary
    (`identity.chat_role`), never re-derived here.
    """
    identity = result.inbound.identity
    if _is_admin_owner(identity.user_id):
        return True
    chat_type = getattr(identity.source, "chat_type", "dm") or "dm"
    if chat_type != "dm":
        return getattr(identity, "chat_role", None) == "admin"
    return _owns_dm_session_key(result.decision.session_key,
                                getattr(identity.source, "chat_id", None),
                                identity.user_id,
                                getattr(identity.source, "surface_id", None))


def _owns_dm_session_key(session_key: Optional[str], chat_id: Any,
                         user_id: Any, surface_id: Any = None) -> bool:
    """Is *session_key* exactly the DM key for this (surface, chat, sender)? (043 T4)

    ⚠️ This was `f":dm:{chat_id}:" in key` — a SUBSTRING search, which asks
    whether the key CONTAINS a shape rather than whether the key IS this
    sender's session. A session key is an ADDRESS, not a haystack: its writer
    is `core/surfaces/session_chat_registry.py::build_session_key`, which
    joins `agent:main:{surface}:{chat_type}:{chat_id}[:{user_id}]
    [:thread:{thread_id}]` on `:`. So `:dm:12:` also matches a group key whose
    own payload carries those bytes in a later segment (e.g. a chat id or
    thread id), and nothing at all checked WHO the key belonged to.

    Parsed instead: the surface/chat_type/chat_id through the builder's own
    inverse (`row_from_session_key`), then the DM's user segment positionally —
    the inverse does not expose it, and that segment is the half that answers
    "is this sender's session".

    ⚠️ The SURFACE is checked against THIS INBOUND's surface, never against the
    literal `"telegram"`: this module is the SHARED inbound actor (registered
    via `core/surfaces/act.py::register_inbound_actor`), so discord, slack,
    signal, x — every `surfaces/_shared.py::route_and_act` caller — and the
    email/webhook path reach these lifecycle verbs too. Pinning the string
    would deny `/cancel` to every non-telegram DM user. Comparing the two
    closes the same hole (an `agent:main:email:dm:555:u` key cannot satisfy a
    telegram sender) and is right on all of them.

    Fail-CLOSED: anything that is not exactly a DM key for this triple (a
    missing user segment, a foreign surface, an unparseable key) is a denial. A
    lifecycle verb cancels a running task; a maybe is a no.
    """
    row = None
    try:
        from core.surfaces.session_chat_registry import row_from_session_key
        row = row_from_session_key(str(session_key or ""))
    except Exception:
        logger.debug("lifecycle: session key parse failed", exc_info=True)
        return False
    if not row or row.get("chat_type") != "dm":
        return False
    if not surface_id or str(row.get("surface_id") or "") != str(surface_id):
        return False
    if not chat_id or str(row.get("chat_id") or "") != str(chat_id):
        return False
    # Segment 5 is the DM's user_id (`build_session_key` appends it only for a
    # dm with a user_id). Its absence means the key does not name an owner, so
    # it cannot name THIS one.
    parts = str(session_key or "").split(":")
    return bool(user_id) and len(parts) >= 6 and parts[5] == str(user_id)


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
    """`/missed [n]` — the owner notices the delivery rail could not send
    live (durable ``owner_notice`` rows the rail writes instead of dropping
    the text): suppressed by the daily cap, held by an owner pause, or
    undelivered (no live sink / send failed) — newest first. 2026-08-28: 91
    of 92 notices in 24h were capped and nothing let the owner read them; A7
    (2026-09-14) widened this to every marker (it used to match the cap
    marker only, so a paused/undelivered notice was unreadable). A thin
    renderer over ``core.surfaces.missed.missed_notices`` — CLI/REPL reuse
    the same query."""
    try:
        n = max(1, min(20, int(args[0]))) if args else 5
    except (TypeError, ValueError):
        return "Usage: /missed [n] (1-20, default 5)"
    try:
        from core.surfaces.missed import missed_notices
        rows = missed_notices(user_id, data_dir, n)
    except Exception as e:
        return f"Missed messages: unavailable ({type(e).__name__}: {str(e)[:120]})"
    if not rows:
        return "No suppressed owner messages on record."
    import time as _time
    lines = [f"Last {len(rows)} suppressed owner message(s) (newest first):"]
    for r in rows:
        text = str(r.get("text") or "").replace("\n", " ").strip()
        if len(text) > 240:
            text = text[:237] + "…"
        stamp = _time.strftime("%m-%d %H:%M", _time.gmtime(float(r.get("ts") or 0)))
        kind = r.get("kind") or "capped"
        lines.append(f"• {stamp}Z — [{kind}] {text}")
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
    # The DAG is the answer to "why is nothing moving": a `waiting` row is
    # parked behind a prerequisite, and goal_edges has held that fact all
    # along while the surface rendered the bare word "waiting" (2026-09-08 —
    # the owner asked "so the trading goal is running?" and could not tell).
    # core.goal_board_render also leads with running/blocked, because those are
    # the only rows that need a human.
    from core.goal_board_render import render_board

    return render_board(gb, user_id=user_id)


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
    return (f"Failed to approve {target} — it is no longer pending. "
            f"See /pending for what is.")


#: The one-token command grammar lives in ``core.surfaces.tappable`` — the
#: harness PARSES a tapped token and ``core.owner_remedy`` must RECOGNISE one,
#: and two tiers cannot each own the same grammar. This name is kept because it
#: is the harness's published seam (tests and the two dispatch sites use it).
def normalize_tappable_command(text: str) -> tuple:
    """``("/approve", "tap-abc")`` for ``/approve_tap_abc``, else ``(None, None)``.

    Also resolves ``/approve_p_<hex>`` (a pending-item alias) and
    ``/approve_all`` (the whole queue). See ``core.surfaces.tappable``.
    """
    return _parse_tappable(text)


def _pending_set(user_id: str, data_dir: str, instance_id: str, board: Any):
    """This seat's read of the ONE pending union (`approval_queue.all_pending`).

    Reuses the board the handler already built, so `/pending` and `/approve` do
    not open `goals.db` twice for one message.
    """
    from core.surfaces.correspondents import CorrespondentRegistry
    from tools.controller.approval_queue import all_pending
    try:
        registry = CorrespondentRegistry(os.path.join(data_dir, "correspondents.db"))
    except Exception:
        logger.warning("telegram: correspondent registry unavailable", exc_info=True)
        registry = None
    return all_pending(user_id=user_id, home_dir=data_dir, instance_id=instance_id,
                       board=board, correspondent_registry=registry)


async def _decide_pending_item(task_agent: Any, item: dict, *, approve: bool,
                               user_id: str, data_dir: str, instance_id: str,
                               board: Any) -> tuple:
    """Record the owner's decision on ONE item of the union. ``(ok, message)``.

    The union holds three kinds of thing and each has its own decider; routing
    lives here so the single-item path and `/approve_all` can never disagree
    about what a decision means.
    """
    from tools.controller.approval_queue import decide_pending
    kind = item.get("kind", "")
    if kind == "correspondent":
        # This seat keeps its own owner-facing sentence for a contact — it names
        # what changes about THEIR replies, which the generic decider does not.
        # A successful decision is marked with a leading glyph; read that rather
        # than the prose, so a reworded failure is never counted as a win.
        reply = _correspondent_decision(data_dir, str(item.get("id", "")),
                                        user_id, approve=approve)
        if reply is not None:
            return reply.startswith(("✅", "🚫")), reply
        return False, f"could not decide correspondent {item.get('id')}"
    return decide_pending(kind, item.get("id"), approve=approve, user_id=user_id,
                          home_dir=data_dir, instance_id=instance_id,
                          board=board, task_agent=task_agent)


async def _handle_plain_pending_decision(task_agent: Any,
                                         result: InboundResult) -> Optional[str]:
    """The owner replied "approve" / "reject" in words. Decide, or return None.

    Returns ``None`` — the message goes to the agent untouched — whenever the
    text is not unambiguously a decision, the chat is a room, or nothing is
    waiting. A decision word with an empty queue is just a word.
    """
    from core.surfaces.owner_admin import parse_pending_decision
    from core.surfaces.room_keys import is_group_session_key

    if is_group_session_key(getattr(result.decision, "session_key", "")):
        return None
    parsed = parse_pending_decision(getattr(result.inbound, "text", "") or "")
    if parsed is None:
        return None
    verb, target = parsed
    user_id = result.inbound.identity.user_id
    data_dir = _admin_data_dir(task_agent)
    from core.instance import resolve_instance_id
    from agents.task.goals.board import GoalBoard
    board = GoalBoard(os.path.join(data_dir, "goals.db"))
    pending = _pending_set(user_id, data_dir, resolve_instance_id(), board)
    if not pending.items:
        # Nothing to decide, so this was conversation. Say nothing and let the
        # agent answer — a bare "approve" over an empty queue must not become a
        # confident "nothing is waiting on you" that ends the turn.
        return None
    # Reuse the ONE handler, so the word and the tap take the identical path.
    # On a COPY: the owner's real words stay intact for the session record and
    # for anything downstream that reads them.
    import copy as _copy
    as_command = _copy.copy(result)
    as_command.inbound = _copy.copy(result.inbound)
    as_command.inbound.text = f"{verb} {target}" if target else verb
    return await _handle_owner_admin(task_agent, as_command, verb)


async def _handle_owner_admin(task_agent: Any, result: InboundResult, cmd: str) -> str:
    """§7.1 missing hop + §7.2b: /pending /approve /reject /asks /fulfill.
    owner-UX P4 T2 adds the read-only /status /recap /goals /prefs verbs.

    Thin plumbing over the SAME primitives the `polyrob owner` CLI uses
    (core.self_evolution + GoalBoard.asks/fulfill_ask) so a phone-only headless
    owner can close the approve loop.
    """
    user_id = result.inbound.identity.user_id
    # 044 T18: `/groups mode|tail` and `/mute` are reachable by a ROOM ADMIN
    # too — dispatched BEFORE the owner-only gate below. `group_ops` does its
    # own role check (owner OR that room's admin), so a plain member is still
    # refused; every other `/groups` verb (allow/deny/list/set/role/service/
    # admins) stays behind the owner-only gate.
    if cmd == "/groups":
        _groups_args = result.inbound.text.strip().split()[1:]
        _groups_verb = _groups_args[0].lower() if _groups_args else "list"
        if _groups_verb in ("mode", "tail"):
            from surfaces.telegram import group_ops
            return await group_ops.groups_reply(task_agent, result, _groups_args)
    elif cmd == "/mute":
        from surfaces.telegram import group_ops
        return await group_ops.mute_reply(task_agent, result,
                                    result.inbound.text.strip().split()[1:])
    elif cmd in ("/unmute", "/ban", "/unban"):
        # 046 phase 2: the rest of the catalog, same two meanings as `/mute` —
        # in REPLY it names a person (free for the owner and room admins, priced
        # for a member); with no reply it has no room meaning and says so.
        from surfaces.telegram import group_ops
        handler = {"/unmute": group_ops.unmute_reply,
                   "/ban": group_ops.ban_reply,
                   "/unban": group_ops.unban_reply}[cmd]
        return await handler(task_agent, result,
                       result.inbound.text.strip().split()[1:])
    elif cmd == "/paid":
        # 046: owner OR that room's admin, checked inside `group_ops` like
        # `/groups mode|tail` — dispatched BEFORE the owner-only gate below.
        from surfaces.telegram import group_ops
        return await group_ops.paid_reply(task_agent, result,
                                    result.inbound.text.strip().split()[1:])
    if not _is_admin_owner(user_id):
        return "🔒 Owner only."
    from core import self_evolution
    from core.instance import resolve_instance_id
    from agents.task.goals.board import ASK_OPEN, GoalBoard
    data_dir = _admin_data_dir(task_agent)
    instance_id = resolve_instance_id()
    args = result.inbound.text.strip().split()[1:]
    _verb, _tap = normalize_tappable_command(result.inbound.text)
    if _verb == cmd and _tap:
        args = [_tap]
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

    if cmd == "/avatar":
        # Read-only. The photo rides the SAME media rail the invoice card uses
        # (MessageRouter), best-effort: if no router is reachable the text still
        # answers, naming the file, rather than failing the verb.
        # ⚠️ `owner_ops` is imported LAZILY further down this function, after
        # this branch, so it must be imported here rather than assumed.
        from surfaces.telegram import owner_ops as _owner_ops
        text, png = _owner_ops.avatar_reply(data_dir, args)
        if png:
            await _send_photo_best_effort(task_agent, result, png)
        return text

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
    if cmd in ("/cron", "/goal", "/wallet", "/invoices", "/settle", "/trade",
               "/bridge", "/mcp", "/launch", "/deploy", "/lp"):
        from surfaces.telegram import owner_ops
        if cmd == "/lp":
            from surfaces.telegram.lp_ops import lp_reply
            return lp_reply(user_id, args)
        if cmd in ("/launch", "/deploy"):
            # 042: token creation shipped CLI-only, which means the one person
            # allowed to run it has to SSH to the box. Same lesson as /bridge.
            from surfaces.telegram import token_ops
            return (token_ops.launch_reply(user_id, args) if cmd == "/launch"
                    else token_ops.deploy_reply(user_id, args))
        if cmd == "/mcp":
            # Giving the agent a new MCP server used to be a box-side file edit,
            # which the owner (on a phone) could not do.
            return owner_ops.mcp_reply(user_id, args)
        if cmd == "/bridge":
            # 037: the bridge shipped CLI-only, so the one person allowed to run
            # it had to SSH to the box. He is usually on a phone.
            return owner_ops.bridge_reply(user_id, data_dir, args)
        if cmd == "/trade":
            # The owner asking IS the authorization a stream leg cannot express.
            return owner_ops.trade_reply(user_id, data_dir, args, board=board)
        if cmd == "/cron":
            return owner_ops.cron_reply(user_id, data_dir, args)
        if cmd == "/goal":
            return owner_ops.goal_reply(user_id, data_dir, args, board=board)
        if cmd == "/wallet":
            return owner_ops.wallet_reply(args, user_id=user_id, data_dir=data_dir)
        if cmd == "/invoices":
            return await owner_ops.invoices_reply(user_id, args)
        return await owner_ops.settle_reply(user_id, args)

    if cmd == "/inbox":
        # 043 D1: the ONE list of what is waiting on you — self-evolution
        # proposals, spend approvals, correspondents, blocked goals and apps,
        # composed once and rendered the same way on every seat. /pending stays
        # as the narrower, older view of the first three.
        from surfaces.telegram import owner_ops
        return owner_ops.inbox_reply(user_id, data_dir)

    if cmd == "/book":
        from surfaces.telegram import owner_ops
        return await owner_ops.book_reply(user_id, data_dir)

    if cmd == "/pending":
        # ONE union — the same one `/approve` decides over (2026-09-15). Chat
        # used to build this join by hand while the bare `/approve` shortcut read
        # only the first of the three sources; see `approval_queue.all_pending`.
        pending = _pending_set(user_id, data_dir, instance_id, board)
        if not pending.items:
            return (pending.degraded_line() or "No pending proposals.")
        lines = [f"{len(pending.items)} pending proposal(s):"]
        for it in pending.items:
            preview = (it.get("preview") or "").strip()
            if len(preview) > 160:
                preview = preview[:157] + "…"
            lines.append(f"• {it['kind']}:{it['id']} — {preview}")
            lines.append(f"   {self_evolution.pending_tap_token('approve', it)}   "
                         f"{self_evolution.pending_tap_token('reject', it)}")
        lines.append("Everything at once: /approve_all")
        if pending.degraded_line():
            lines.append(pending.degraded_line())
        return "\n".join(lines)

    if cmd in ("/approve", "/reject"):
        approve = (cmd == "/approve")
        pending = _pending_set(user_id, data_dir, instance_id, board)
        if not args:
            # Owner complaint, 2026-09-12: "the whole command should be
            # highlighted so I could tap on it. now I need to copy and type in
            # the id". Telegram auto-links the VERB `/approve` but not its
            # argument, so the one tappable thing on screen was the half that
            # does nothing. When exactly ONE thing is waiting there is no id to
            # disambiguate — so the bare verb decides it, and the tap is the
            # whole interaction.
            if not pending.items:
                return (pending.degraded_line()
                        or "Nothing pending — there is nothing waiting on you.")
            if len(pending.items) > 1:
                _lines = [f"{len(pending.items)} pending — tap the one you mean:"]
                for _it in pending.items:
                    _lines.append(
                        f"  {self_evolution.pending_tap_token(cmd.lstrip('/'), _it)}"
                        f" — {_it['kind']}:{_it['id']}")
                _lines.append(f"All of them: {cmd}_all")
                if pending.degraded_line():
                    _lines.append(pending.degraded_line())
                return "\n".join(_lines)
            target = str(pending.items[0]["id"])
            match = pending.items[0]
        else:
            target = args[0]
            match = None
        # 035 P1-10: `/approve all` — decide the whole queue from the phone. The
        # owner surface where friction costs most is the one where typing an id
        # is hardest. 2026-09-15: "the whole queue" now means the UNION it is
        # advertised as, not the self-evolution third of it.
        if target.lower() == "all":
            if not pending.items:
                return (pending.degraded_line() or "No pending proposals.")
            msgs, ok_n, fail_n = [], 0, 0
            for it in list(pending.items):
                ok, msg = await _decide_pending_item(
                    task_agent, it, approve=approve, user_id=user_id,
                    data_dir=data_dir, instance_id=instance_id, board=board)
                ok_n, fail_n = (ok_n + 1, fail_n) if ok else (ok_n, fail_n + 1)
                msgs.append(f"{'✓' if ok else '✗'} {it['kind']}:{it['id']} — {msg}")
            verb = "approved" if approve else "rejected"
            msgs.append(f"{ok_n} {verb}, {fail_n} failed")
            if pending.degraded_line():
                msgs.append(pending.degraded_line())
            return "\n".join(msgs)
        if match is None:
            from tools.controller.approval_queue import resolve_pending_target
            match = resolve_pending_target(target, pending)
        if match is None:
            # A `tap-`-shaped target that is not in the OPEN queue is still the
            # tool-approval lane's business: only it can tell the owner whether
            # the ask was already decided, expired, or never existed. A generic
            # "no pending proposal" would flatten those three into one.
            from tools.controller.approval_queue import strip_tap_prefix
            if strip_tap_prefix(target) is not None:
                ok, msg = await _decide_pending_item(
                    task_agent, {"kind": "tool_approval", "id": target},
                    approve=approve, user_id=user_id, data_dir=data_dir,
                    instance_id=instance_id, board=board)
                return msg if ok else f"Failed: {msg}"
            return (f"No pending proposal '{target}' — see /pending."
                    + (f"\n{pending.degraded_line()}" if pending.degraded_line() else ""))
        ok, msg = await _decide_pending_item(
            task_agent, match, approve=approve, user_id=user_id,
            data_dir=data_dir, instance_id=instance_id, board=board)
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

    if cmd == "/groups":
        # Reached here only for the owner-only verbs (mode/tail already
        # returned above, before the owner gate). The sender IS the owner at
        # this point, so `group_ops` grants every verb.
        from surfaces.telegram import group_ops
        return await group_ops.groups_reply(task_agent, result, args)

    return _help_text()


def _room_member_help(task_agent: Any, result: InboundResult):
    """A room MEMBER's `/help`, rendered for the ROOM — or None for everyone else.

    None means "not this case", so the owner and a room admin fall through to the
    unchanged owner help. The role comes from ``room_turn.room_role``, the ONE
    resolver: it reads the role the router already stamped and falls back to the
    owner check, so this can never disagree with the tier that admitted the line.
    """
    from core.surfaces.room_keys import is_group_session_key
    if not is_group_session_key(result.decision.session_key):
        return None
    from surfaces.telegram.room_turn import room_role
    if room_role(result) in ("owner", "admin"):
        return None
    from core.surfaces.command_reply import CommandReply
    from core.surfaces.room_actions import render_member_help
    src = getattr(result.inbound.identity, "source", None)
    surface = str(getattr(src, "surface_id", "") or "telegram")
    chat_id = str(getattr(src, "chat_id", "") or "")
    try:
        text = render_member_help(getattr(task_agent, "container", None),
                                  surface=surface, chat_id=chat_id,
                                  agent_name=_agent_name())
    except Exception as e:
        # Fail-open to a true sentence, never to the owner catalog: a member
        # must not be handed the admin verb list because a store was unreadable.
        logger.warning("telegram: member help render failed (%s)", e)
        text = ("I could not read this room's settings just now — try again in "
                "a moment.")
    return CommandReply(text, to_room=True)


async def _handle_command(task_agent: Any, result: InboundResult, spawn, deliver=None,
                          fetch_media=None) -> Optional[str]:
    cmd = (result.decision.command or "").lower()
    if cmd == "/help":
        args = (result.inbound.text or "").split()[1:]
        # 046: a plain MEMBER of a room asked what HE can do. Answering with the
        # owner's whole verb catalog — every admin, money and control verb — and
        # sending it to the OWNER's DM (the 044 owner-only redirect) meant the
        # member who asked saw nothing at all. The owner's and a room admin's
        # `/help` is byte-identical to before.
        _member_help = _room_member_help(task_agent, result)
        if _member_help is not None:
            return _member_help
        if args:
            return _help_for(args[0])
        return _help_text()
    if cmd == "/start":
        return _welcome_text()
    if cmd not in _OWNER_ADMIN_COMMANDS:
        # `/approve_tap_<hex>` arrives as its own command token, not as
        # `/approve` + an argument, so it must be mapped back before dispatch.
        _verb, _tap = normalize_tappable_command(result.inbound.text)
        if _verb and _tap:
            cmd = _verb
    # 044 I10 (spec §6.1 P9/P10): the owner's money, host and control verbs are
    # not room verbs. They EXECUTED from inside a room — the reply was redirected
    # to his DM, but the ACTION ran, so a member who talked the owner into typing
    # `/trade …` got it. The room is also the one place a shoulder-surfer or a
    # screen-share is guaranteed. One sentence, to the DM, naming where to do it.
    from core.surfaces.room_keys import is_group_session_key
    if is_group_session_key(result.decision.session_key) and cmd in _ROOM_REFUSED_COMMANDS:
        logger.info("telegram: %s refused from room %s", cmd,
                    result.decision.session_key)
        return (f"🔒 `{cmd}` is not available from a group chat — do this in our "
                f"private chat.")
    if cmd in _OWNER_ADMIN_COMMANDS:
        try:
            return await _handle_owner_admin(task_agent, result, cmd)
        except Exception as e:
            logger.error("owner admin command %s failed: %s", cmd, e, exc_info=True)
            # 030 C-14: cap + soften — raw exception text can carry absolute
            # paths/DB errors/provider bodies, and it is unbounded.
            return f"Command failed: {str(e)[:200]} (details in the server log)"
    if cmd == "/cancel":
        if not _lifecycle_permitted(result):
            return _UNAUTHORIZED_TEXT
        sid = result.decision.session_id
        if sid:
            try:
                await task_agent.cancel_session_by_id(sid, force=True)
            except Exception as e:
                logger.debug("telegram /cancel failed: %s", e)
            return "Task cancelled."
        return "No active task to cancel."
    if cmd == "/new":
        if not _lifecycle_permitted(result):
            return _UNAUTHORIZED_TEXT
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
        await _start_task_session(task_agent, result, spawn, deliver, fetch_media)
        return None
    return _unknown_command_text(cmd)  # unknown command -> suggestion + /help (030 L9)


async def act_on_inbound(
    task_agent: Any,
    result: InboundResult,
    *,
    spawn: Optional[Callable[[Any], Any]] = None,
    deliver: Optional[Callable[[str], Any]] = None,
    fetch_media: Optional[Callable[[Any], Any]] = None,
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
                                            deliver=deliver, fetch_media=fetch_media)


async def _act_on_inbound_locked(
    task_agent: Any,
    result: InboundResult,
    *,
    spawn: Optional[Callable[[Any], Any]] = None,
    deliver: Optional[Callable[[str], Any]] = None,
    fetch_media: Optional[Callable[[Any], Any]] = None,
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
        return _UNAUTHORIZED_TEXT

    if kind == RouteKind.CORRESPONDENT_DATA:
        # WS-A: a third party the agent contacted replied. Their text is DATA delivered
        # ONLY to the originating session (never a steer/command/new-session). Use the
        # sender's external address as the untrusted source label.
        src = result.inbound.identity.raw_user_id or result.inbound.identity.user_id
        try:
            # message_id (email: RFC Message-ID = idempotency key) feeds the durable
            # conversation log so OUR reply can set In-Reply-To (E1/A3).
            from core.surfaces.room_keys import is_group_session_key
            await task_agent.deliver_correspondent_data(
                decision.session_id, src, result.inbound.text,
                metadata={"message_id": result.inbound.idempotency_key or ""},
                surface=getattr(result.inbound.identity.source, "surface_id", None),
                group=is_group_session_key(decision.session_key),
            )
        except Exception as e:
            logger.debug("correspondent delivery failed: %s", e)
        return None

    if kind == RouteKind.COMMAND:
        return await _handle_command(task_agent, result, spawn, deliver, fetch_media)

    if kind == RouteKind.GROUP_TURN:
        # 044 T14: a line in an allowed ROOM, answered by the bound PUBLIC session.
        # The <group-context> block rides ONE call as an ephemeral control message;
        # the <addressed> line is a steer for the owner/an admin and untrusted DATA
        # for a member. Mirrors the STEER branch from here on (touch the binding,
        # then spawn the ONE place that runs the session and delivers its reply).
        turn, role = _room_turn_text(task_agent, result)
        _metadata = None
        if role in ("owner", "admin"):
            # 044 §4.4 + the 2026-09-13 media rule: bytes are absorbed for an
            # owner/admin turn only. A member's file is NAMED in the ledger line
            # and never written into the owner tenant's workspace. The
            # description goes INSIDE <addressed> — after the closing fence it
            # reads as a separate, unattributed instruction.
            _extra, _metadata = await absorb_for_session(
                task_agent, result, decision.session_id, fetch_media, base_text="")
            turn = _with_attachment(turn, _extra)
        status = "gone"
        try:
            status = await task_agent.deliver_group_turn(
                decision.session_id,
                user_id=result.inbound.identity.user_id,
                context_block=turn.context, addressed_block=turn.addressed, role=role,
                reply_to=_room_reply_anchor(result), metadata=_metadata,
                surface=turn.surface, chat_id=turn.chat_id,
                # The lines it was SHOWN; the addressed one is marked from
                # `reply_to` inside, so a dropped context block still records
                # that THIS message was handled.
                shown_message_ids=turn.shown_message_ids,
            )
        except Exception as e:
            _ctx = f"session={decision.session_id} chat={turn.chat_id}"
            logger.warning("telegram GROUP_TURN deliver failed (%s): %s — the "
                           "room got no reply this turn", _ctx, e)
        if status in ("delivered", "busy"):
            try:
                task_agent.touch_chat_binding(decision.session_key)
            except Exception as e:
                _ctx = f"key={decision.session_key}"
                logger.warning("telegram GROUP_TURN touch_chat_binding failed "
                               "(%s): %s", _ctx, e)
        if status == "delivered":
            # The OWNER uid comes from the SESSION, never from the speaker: a
            # member's uid must not become the tenant a room turn runs as.
            _spawn(_run_and_deliver(task_agent,
                                    _session_owner_uid(task_agent, decision.session_id,
                                                       result.inbound.identity.user_id),
                                    decision.session_id, deliver,
                                    notice_key=decision.session_key), spawn)
            return None
        if status == "gone":
            # The bound session is unrecoverable — open a fresh one for the room
            # rather than dropping the line (the STEER branch's own fallback).
            await _start_task_session(task_agent, result, spawn, deliver, fetch_media)
        # "busy"/"held": silent in a room. A cap/queue notice posted publicly is
        # noise for every other member, and the owner has the DM for diagnostics.
        return None

    if kind == RouteKind.STEER:
        # Deliver into the BOUND session — resident OR recreated-from-disk (which
        # restores the full message_history.json). This unifies the resident and
        # warm-but-dead cases: BOTH resume `decision.session_id`, never minting a new
        # amnesiac session. Then re-run it so the queued message is processed
        # (run_session is concurrent-resume safe: a no-op if a loop is already running).
        status = "gone"
        # Attachments land in THIS session's workspace and ride the same turn as
        # the caption, so the agent sees one coherent message (2026-09-13).
        _text, _metadata = await absorb_for_session(
            task_agent, result, decision.session_id, fetch_media,
            base_text=result.inbound.text or "")
        # 044 T10 fix round 1: the reply-anchor rides ON THIS MESSAGE's metadata,
        # not a post-hoc poke of the orchestrator — a message the queue REJECTS
        # (status == "busy" below) never carries its anchor anywhere, so a
        # still-in-flight EARLIER turn can never be re-anchored to a newer
        # message. `_drain_user_messages` reads it back off the batch that
        # actually drains (agent/core/user_ingress.py).
        _anchor = _room_reply_anchor(result)
        if _anchor is not None:
            _metadata = {**(_metadata or {}), "reply_to": _anchor}
        try:
            status = await task_agent.ensure_session_and_deliver(
                result.inbound.identity.user_id, decision.session_id,
                _text, kind="comment", metadata=_metadata,
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
        await _start_task_session(task_agent, result, spawn, deliver, fetch_media)
        return None

    # TASK_AGENT and CHAT_FASTPATH (MVP) -> start/continue a task session.
    await _start_task_session(task_agent, result, spawn, deliver, fetch_media)
    return None


# --- live harness (aiogram Bot; webhook OR local polling) --------------------


def _tg_message(update: dict) -> dict:
    """044 T9: include channel_post/edited_channel_post so _tg_user_id/_tg_chat_id/
    _tg_chat_type resolve correctly for a channel update (which carries no `from`)."""
    return (update.get("message") or update.get("edited_message")
            or update.get("channel_post") or update.get("edited_channel_post") or {})


def _is_owner_groups_line(update: dict) -> bool:
    """044 C6: is this the OWNER's `/groups …` line? (raw update, pre-routing.)

    The one thing `_room_is_allowed` admits from a room the owner has NOT
    allowlisted yet — because `/groups allow here` is what CREATES that row.
    Both halves must hold:

    * the sender's raw Telegram id is the configured owner's. Resolved through
      `owner_surface_alias`, the SAME seam `surfaces/telegram/inbound.py` uses to
      map an authenticated owner onto the principal, so this gate and the
      dispatcher's own `is_room_owner` check can never disagree about who the
      owner is. A non-owner (or an unbound owner, or an unconfigured owner
      telegram id) is None -> False.
    * the first token, `@botname` suffix stripped, is exactly `/groups`. Read
      from `text` only: a caption, a voice note and a forwarded body are not the
      documented path and must keep costing nothing in an unlisted room.

    Never raises — a probe fault reads as "not the owner" (the room stays shut).
    """
    try:
        msg = _tg_message(update)
        text = str(msg.get("text") or "").strip()
        if not text:
            return False
        if text.split(" ", 1)[0].lower().split("@", 1)[0] != "/groups":
            return False
        from core.instance import owner_surface_alias
        return owner_surface_alias(_tg_user_id(update), "telegram") is not None
    except Exception as e:
        logger.debug("telegram owner-/groups probe failed (not the owner): %s", e)
        return False


def _tg_user_id(update: dict) -> Optional[str]:
    """Raw Telegram numeric sender id (str) from an update, or None."""
    frm = (_tg_message(update).get("from") or {})
    uid = frm.get("id")
    return str(uid) if uid is not None else None


def _tg_chat_id(update: dict) -> Optional[str]:
    chat = (_tg_message(update).get("chat") or {})
    cid = chat.get("id")
    return str(cid) if cid is not None else None


def _tg_chat_type(update: dict) -> str:
    """044 T2/T3: the raw Telegram chat type (private/group/supergroup/channel)."""
    chat = (_tg_message(update).get("chat") or {})
    return str(chat.get("type") or "private")


def _is_channel_post(update: dict) -> bool:
    """044 fix round 1 (finding 1): a channel_post/edited_channel_post carries
    neither `from` nor `sender_chat` — its sender IS the channel itself (raw id
    == chat id, which build_inbound_message's own fallback already handles).
    Exempting it from the anonymous-sender gate lets it reach process_update; a
    real group/supergroup message with sender_chat and no `from` (the
    anonymous-admin case the gate exists for) still gets denied."""
    return "channel_post" in update or "edited_channel_post" in update


def _record_drop(update: dict, tg_id, reason: str) -> None:
    """045 lane 1: a pre-route refusal still leaves a trace. Fail-open."""
    try:
        msg = update.get("message") or update.get("channel_post") or {}
        record_pre_route_drop(
            surface="telegram",
            chat_id=str(_tg_chat_id(update) or "?"),
            chat_type=_tg_chat_type(update) or "?",
            sender=str(tg_id if tg_id is not None else "anonymous"),
            reason=reason,
            body_len=len(msg.get("text") or msg.get("caption") or ""),
        )
    except Exception:
        logger.debug("telegram: pre-route drop record skipped", exc_info=True)


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
        self.bot_id: Optional[int] = None
        from surfaces.telegram.surface import TelegramSurface
        self.surface = TelegramSurface(bot)

    async def _fetch_media_bytes(self, media):
        """Download ONE inbound attachment's bytes (the transport half of the
        2026-09-13 media rail). Fail-open -> None; the core rail turns a None into
        an honest "could not be downloaded" line on the turn."""
        ref = getattr(media, "ref", None)
        if not ref:
            return None
        from surfaces.telegram.voice import download_file_bytes
        return await download_file_bytes(self.bot, ref)

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

    async def _refresh_identity(self) -> None:
        """044 T8: getMe() retry, scheduled once from start()'s failure path — a
        transient startup network hiccup must not leave mention detection
        (and the own-handle owner-alias) permanently blind for the process
        lifetime."""
        try:
            me = await self.bot.get_me()
            username = getattr(me, "username", None)
            self.bot_id = getattr(me, "id", None)
            if username:
                self.bot_username = username
                self.surface.bot_username = username
        except Exception as e:
            logger.warning("telegram get_me retry (bot_username/bot_id resolve) failed: %s", e)

    async def start(self) -> None:
        from core.surfaces.registry import register_surface
        from core.surfaces.transcription import log_transcription_readiness
        register_surface(self.container, self.surface)
        # 046 T1: the ONE verb -> Telegram call adapter, registered as the
        # container service `room_actions.apply` resolves. Without it a settled
        # paid action reaches no transport and is CREDITED instead of applied.
        from surfaces.telegram.room_moderator import install_room_moderator
        install_room_moderator(self.container, self.surface)
        log_transcription_readiness(self.container)
        # 044 T8: an explicit TELEGRAM_BOT_USERNAME seeds mention detection before
        # (or in place of, if it never succeeds) getMe(); getMe still wins when it
        # succeeds, since it's also the only source of bot_id.
        env_username = (os.getenv("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@")
        if env_username:
            self.bot_username = env_username
            self.surface.bot_username = env_username
        try:
            me = await self.bot.get_me()
            username = getattr(me, "username", None)
            self.bot_id = getattr(me, "id", None)
            if username:
                self.bot_username = username
                self.surface.bot_username = username
        except Exception as e:  # fail-open: group-mention detection and the
            # own-handle owner-alias (message_send.py) just stay inert, same
            # as today, if getMe() is unavailable (e.g. a test double Bot).
            logger.warning("telegram get_me (bot_username/bot_id resolve) failed: %s", e)
            asyncio.get_running_loop().call_later(
                60, lambda: asyncio.ensure_future(self._refresh_identity())
            )
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
        """Register the verb list with Telegram — in the OWNER's chat ONLY.

        Without a menu there is no autocomplete and no descriptions on mobile —
        the owner has to remember the verbs or type /help and scroll (chat-first
        review 2026-08-22, G12). Sourced from the `_HELP_BODY` SSOT, so the menu
        cannot drift from the help text.

        ⚠️ NEVER publish to a broad scope. `set_my_commands` with no `scope=`
        writes Telegram's DEFAULT scope, which every user who opens the bot is
        served — so an owner-locked deploy showed a stranger 36 admin verbs
        (/wallet, /trade, /deploy, /halt …) that only the owner can run. Worse,
        Telegram KEEPS serving a published list until something overwrites it,
        so a retired verb outlives the build that published it. Hence the three
        `delete_my_commands` calls below: they are what actually removes an
        obsolete global menu, and they must run on every start, not once.

        Fail-open: a bot without setMyCommands (or a test double) is unaffected.
        """
        try:
            from aiogram.types import (BotCommand, BotCommandScopeAllGroupChats,
                                       BotCommandScopeAllPrivateChats,
                                       BotCommandScopeChat, BotCommandScopeDefault)
        except Exception:
            return  # aiogram absent (tests inject a fake bot) — nothing to publish

        # 1. Clear every scope that is not one owner's own chat. One call per
        #    scope so a failure on one cannot hide the others.
        deleter = getattr(self.bot, "delete_my_commands", None)
        if deleter is not None:
            for scope in (BotCommandScopeDefault(),
                          BotCommandScopeAllPrivateChats(),
                          BotCommandScopeAllGroupChats()):
                await self._menu_api_call(
                    f"delete_my_commands({getattr(scope, 'type', scope)})",
                    deleter, scope=scope)

        # 2. Publish to the owner's private chat only. No resolved owner ->
        #    no menu anywhere (the clear above still ran), which is the honest
        #    outcome: we cannot name a chat that is allowed to see the verbs.
        setter = getattr(self.bot, "set_my_commands", None)
        if setter is None:
            return
        owner_ids = _menu_chat_ids()
        if not owner_ids:
            logger.info("telegram command menu cleared; not published "
                        "(no owner chat id — set POLYROB_OWNER_TELEGRAM_ID)")
            return
        # Unguarded on purpose: help_commands() already filters to Telegram's
        # own name/description rules, and building these objects is pinned by
        # tests/unit/surfaces/telegram/test_command_menu_scope.py. A swallow
        # here would only hide a menu the owner then cannot find.
        commands = [BotCommand(command=name, description=desc)
                    for name, desc in help_commands()]
        if not commands:
            return
        for chat_id in owner_ids:
            if await self._menu_api_call(f"set_my_commands(chat {chat_id})", setter,
                                         commands,
                                         scope=BotCommandScopeChat(chat_id=chat_id)):
                logger.info("telegram command menu published to chat %s (%d verbs)",
                            chat_id, len(commands))

    async def _menu_api_call(self, what: str, method, *args, **kw) -> bool:
        """Run ONE menu Bot API call fail-open; True if it went through.

        Deliberately the only handler the menu has: the silence ratchet
        (`tests/test_status_silence_ratchet.py`) counts log-only excepts, and a
        per-call try/except in the loops above would add three of them. A
        failure here is owner-visible (a stale global menu, or no menu at all),
        so it reports at WARNING — never debug.
        """
        try:
            await method(*args, **kw)
            return True
        except Exception as e:
            logger.warning("telegram %s failed: %s", what, e)
            return False

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
                    record_poll_error("telegram", e)
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

    async def _send_reply_media(self, chat_id, entry: dict) -> None:
        """One media entry alongside a command reply (046 T12).

        The same shape `TelegramBotSink._send_media` renders — a payment card is
        a picture of facts the TEXT already carries, so a missing or unreadable
        path is a skipped picture, never a failed reply.
        """
        if not isinstance(entry, dict):
            return
        path = entry.get("path")
        if not path or not (os.path.isfile(path) and os.access(path, os.R_OK)):
            logger.warning("telegram: reply media missing/unreadable: %s", path)
            return
        try:
            from aiogram.types import FSInputFile
            file = FSInputFile(path, filename=os.path.basename(path))
        except Exception:
            file = path
        caption = entry.get("caption") or None
        if caption:
            caption = str(caption)[:1024]
        if entry.get("kind") == "image":
            await self.bot.send_photo(chat_id, file, caption=caption)
        else:
            await self.bot.send_document(chat_id, file, caption=caption)

    async def _send_owner_only(self, session_key: Optional[str], chat_id: Optional[str],
                               text: str) -> None:
        """044 T2 (fix-1, round 1 review): send diagnostic/owner-facing text (error
        breadcrumbs). In a room this is redirected to the owner's DM via
        ``owner_only_reply_target``; dropped (logged WARN) when no owner id resolves.
        Folds what used to be two duplicated breadcrumb blocks into one site."""
        from core.surfaces.room_keys import owner_only_reply_target
        target = owner_only_reply_target(session_key, chat_id)
        if target is None:
            logger.warning("owner-only breadcrumb dropped: room key %s and no owner "
                           "telegram id", session_key)
            return
        # 044 I7: bound it. This fires on EVERY failed turn, and a room in a
        # crash loop (or two bots mentioning each other) turned into one raw DM
        # per failure with no ceiling. Same 30-minute per-(surface+chat) window,
        # keyed the same way, as the LLM-outage notice — its own bucket, so a
        # breadcrumb never eats the window the real outage notice needs.
        from core.surfaces.llm_outage_notice import should_send_owner_breadcrumb
        if not should_send_owner_breadcrumb(session_key or str(chat_id or "")):
            logger.info("owner-only breadcrumb suppressed (cooldown) for %s",
                        session_key)
            return
        try:
            await self.bot.send_message(target, text)
        except Exception as e:
            logger.debug("telegram error-breadcrumb send failed: %s", e)

    async def suggest_admins(self, chat_id: str) -> list:
        """044 T19: Telegram's own admin list as SUGGESTIONS for `/groups
        admins here` — never written. The owner still confirms each one
        explicitly via `/groups role here <id> admin`; this never grants a
        role itself (Telegram's admin list is a suggestion the owner confirms,
        never an authority `GroupRoles` reads — see that module's docstring)."""
        try:
            admins = await self.bot.get_chat_administrators(chat_id)
        except Exception as e:
            logger.warning("get_chat_administrators failed for %s: %s", chat_id, e)
            return []
        out = []
        for a in admins or []:
            u = getattr(a, "user", None)
            if u is None or getattr(u, "is_bot", False):
                continue
            name = f"@{u.username}" if getattr(u, "username", None) else str(getattr(u, "first_name", "") or u.id)
            out.append({"id": str(u.id), "name": name})
        return out

    def _room_is_allowed(self, update: dict) -> bool:
        """044 I2: is this non-DM chat one the owner allowlisted?

        True for a DM (this gate is not about DMs) and for an allowed room. False
        for a room the owner never allowed — the caller then drops the update
        BEFORE any side-effecting or paid step. Fail-CLOSED for a room, exactly
        as `resolve_access_tier` is: an unreadable allowlist denies.

        ⚠️ ONE carve-out (044 C6, re-broken by this very gate): the OWNER's
        `/groups` line in a not-yet-allowlisted room. `/groups allow here` is the
        verb that CREATES the allowlist row and the documented first step
        (`docs/guide/groups.md`), so dropping it here made C6's dispatcher fix
        unreachable on Telegram and left the documented path a silent no-op. The
        carve-out is deliberately the narrowest thing that works: the OWNER
        principal (by raw Telegram id, via the same `owner_surface_alias` the
        inbound rail uses) AND a first token of exactly `/groups`. Everything
        else in an unlisted room is still dropped before `process_update` — a
        stranger's `/groups`, and the owner's own voice note, photo or chatter.
        """
        try:
            if _tg_chat_type(update) == "private":
                return True
            chat_id = _tg_chat_id(update)
            if not chat_id:
                return False
            if _is_owner_groups_line(update):
                logger.info("telegram: owner `/groups` admitted from unlisted room %s",
                            chat_id)
                return True
            from core.surfaces.group_admin import is_room_allowed
            # The harness's OWN container, not the task agent's: this runs before
            # anything has resolved a session, and `self.task_agent` is wired
            # after construction on some seats.
            container = getattr(self, "container", None) or getattr(
                getattr(self, "task_agent", None), "container", None)
            if is_room_allowed(container, "telegram", chat_id):
                return True
            logger.debug("telegram: dropping a line from unlisted room %s", chat_id)
            _record_drop(update, _tg_user_id(update), "room_not_allowed")
            return False
        except Exception as e:
            logger.warning("telegram room allowlist pre-check failed (denying): %s", e)
            return False

    async def handle_update(self, update: dict) -> dict:
        """Process one raw Telegram update. Always returns {"ok": True} so a webhook
        gets a fast 200; errors are swallowed (fail-open)."""
        try:
            # Owner-allowlist gate (raw Telegram id), BEFORE any side-effecting step.
            tg_id = _tg_user_id(update)
            if tg_id is None and _tg_chat_type(update) != "private" and not _is_channel_post(update):
                # 044 T3: an anonymous-admin / sender_chat line has no principal.
                # It is never a command; Phase 2 stores it in the ledger as a member
                # line. Until then: drop silently, never fall through the allowlist.
                _record_drop(update, None, "anonymous_sender")
                return {"ok": True}
            if tg_id is not None and raw_allowlist_applies(update):
                gate = owner_allowed(tg_id)
                if gate is False:
                    _record_drop(update, tg_id, "raw_allowlist")
                    return {"ok": True}  # not on the allowlist -> silently ignore
                if gate is None:
                    # No allowlist set: reveal the sender's id so the operator can lock
                    # the bot, and do NOT run the agent (bootstrap mode).
                    # 030 C-9: at most ONE reply per sender per process — this ran
                    # pre-dedup, so a Telegram redelivery (or any stranger's every
                    # message) re-sent it: unbounded reply amplification.
                    chat_id = _tg_chat_id(update)
                    if (chat_id is not None and _tg_chat_type(update) == "private"
                            and tg_id not in self._bootstrap_replied):
                        self._bootstrap_replied.add(tg_id)
                        if len(self._bootstrap_replied) > 1000:  # bound memory
                            self._bootstrap_replied.clear()
                        await self.bot.send_message(
                            chat_id,
                            "🔓 This bot has no allowlist set, so it is locked by default.\n"
                            f"Your Telegram user ID is: {tg_id}\n"
                            f"Set ALLOWED_TELEGRAM_USER_IDS={tg_id} and restart to use it.",
                        )
                    _record_drop(update, tg_id, "no_allowlist")
                    return {"ok": True}

            # 044 I2: a NON-DM chat is governed by the group model, which lives
            # downstream in route_inbound — so every line from every group the bot
            # has ever been added to reached `process_update` FIRST and paid for
            # unauthenticated work on the way: a voice note was DOWNLOADED and
            # transcribed (Whisper, per message) and a user-directory row was
            # written for the sender. Anyone who can add a bot to a channel could
            # spend the owner's money. This probes the SAME default-DENY store
            # `resolve_access_tier` consults — a cheap pre-check, never a second
            # authority. DMs are untouched.
            if not self._room_is_allowed(update):
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
            # 044 T3: routing hasn't happened yet here, so this can't check
            # _route_is_turn(result.decision) — a room voice note is transcribed
            # silently (the bubble returns in Phase 2 once the turn kind is known).
            update_id = update.get("update_id")
            if extract_voice_file_id(update) is not None and not (
                update_id is not None and self.dedup.peek(update_id)
            ) and _tg_chat_type(update) == "private":
                await reporter.stage(ProgressStage.TRANSCRIBING)

            result = await process_update(
                self.container, update,
                dedup=self.dedup, user_directory=self.user_directory,
                transcribe_voice=self._transcribe_voice,
                bot_username=getattr(self, "bot_username", None),
                bot_id=getattr(self, "bot_id", None),
            )
            if result is None:
                await reporter.finish()   # clear a TRANSCRIBING that slipped through
                return {"ok": True}

            # 044 T13: the room log receives every allowed-room line before any gate.
            try:
                from core.surfaces.ledger_ingest import record_inbound_to_ledger
                record_inbound_to_ledger(
                    self.container, result.inbound,
                    # 044 T16: the role the routing boundary already resolved —
                    # a second derivation here could disagree with the tier the
                    # router acted on, and the ledger is the record of WHAT
                    # HAPPENED, not of a second opinion.
                    role=(result.inbound.identity.chat_role or "member"),
                    is_owner=_is_admin_owner(result.inbound.identity.user_id))
            except Exception as e:
                _ctx = f"chat={getattr(result.inbound.identity.source, 'chat_id', '?')}"
                logger.warning("telegram ledger ingest skipped (%s): %s — this "
                               "line will be missing from room context", _ctx, e)

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

            # 044 T3 (fix-1, round 1 review): only a routed TURN may produce a visible
            # side effect from here on — computed once, as early as `result` (the
            # routing decision) is available, and reused by the voice guard below, the
            # transcript echo and the WORKING bubble further down.
            _is_turn = _route_is_turn(result.decision)

            # Voice guard: a voice/audio note that produced no transcript (transcription
            # off, or faster-whisper not installed) would otherwise route an EMPTY turn —
            # which reads as a confused generic reply. Tell the user instead and DON'T run
            # the agent. Clear the status bubble first.
            # Uses the core seam (Task 1.6): inbound.media carries the voice Media set by
            # build_inbound_message, so the guard no longer inspects the raw update dict.
            # fix-1: a DENIED/COMMAND decision never gets this reply either (it was
            # previously sent unconditionally to the raw chat id).
            if _core_vg.voice_needs_guard(result.inbound.media, result.inbound.text):
                await reporter.finish()
                from core.surfaces.config import SurfaceConfig
                guard = _core_vg.voice_unavailable_message(SurfaceConfig.voice_transcription_enabled())
                if chat_id and _is_turn:
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
            # 2026-09-13: an attachment is CONTENT. Before the media rail this guard
            # dropped the owner's photo (prod 08:00:00Z: text='' -> "dropping (no
            # dispatch)") because a photo/document arrives with no `text` at all.
            # Media-bearing updates route; only genuinely contentless ones are noise.
            _has_media = bool(getattr(result.inbound, "media", None))
            if not (result.inbound.text or "").strip() and not _has_media:
                logger.info("telegram inbound: empty non-voice content — dropping (no dispatch)")
                await reporter.finish()
                return {"ok": True}

            # 031: deterministic owner stop/resume — BEFORE any model call, any
            # queue, any tool. A full stop/resume is applied here from the text
            # alone (voice included) and confirmed from the read-back state.
            if (_is_admin_owner(result.inbound.identity.user_id)
                    and getattr(result.decision, "kind", None) in _INTENT_GATE_KINDS):
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
                    # fix-1 (round 1 review): the gate's own confirmation/failure text
                    # is owner-only output too — a room redirects it to the owner DM
                    # instead of posting it publicly (this fires only for STEER /
                    # TASK_AGENT / GROUP_TURN per _INTENT_GATE_KINDS — 044 T14 made
                    # GROUP_TURN the owner's warm room kind — so it is never DENIED
                    # here).
                    from core.surfaces.room_keys import owner_only_reply_target
                    _gate_target = owner_only_reply_target(result.decision.session_key, chat_id)
                    if _gate_target is None:
                        logger.warning("owner-only reply dropped: room key %s and no owner "
                                       "telegram id", result.decision.session_key)
                    else:
                        await _send_telegram_text(self.bot, _gate_target, _gate_reply)
                    return {"ok": True}

            # 2026-09-15: the plain-word pending decision, on the same rail and
            # for the same reason as the stop gate above — the pending notice
            # asks the owner to "reply approve", and until now nothing read the
            # reply. Owner-only, private chat only, and only while something is
            # actually waiting; otherwise the word is ordinary chat and goes to
            # the agent untouched.
            if (_is_admin_owner(result.inbound.identity.user_id)
                    and getattr(result.decision, "kind", None) in _INTENT_GATE_KINDS):
                try:
                    _decision_reply = await _handle_plain_pending_decision(
                        self.task_agent, result)
                except Exception as e:
                    logger.error("telegram pending-decision gate failed: %s", e,
                                 exc_info=True)
                    _decision_reply = None
                if _decision_reply:
                    await reporter.finish()
                    if chat_id:
                        await _send_telegram_text(self.bot, chat_id, _decision_reply)
                    return {"ok": True}

            # Persistent transcript echo (voice only): post '🎙️ Transcript: …' quoting the
            # voice note so the user sees what ROB heard, BEFORE the answer. Fail-open —
            # never blocks the turn. Gated VOICE_TRANSCRIPT_ECHO (default ON).
            from core.surfaces.voice_echo import voice_transcript, voice_echo_message
            from core.surfaces.config import SurfaceConfig
            if SurfaceConfig.voice_transcript_echo_enabled() and _is_turn:
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

            if _is_turn:
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
                        # 044 T2: this is diagnostic/owner-facing text — for a room
                        # session it goes to the owner's DM, never the room.
                        await self._send_owner_only(
                            result.decision.session_key, chat_id,
                            "⚠️ Something went wrong handling that — please try again.")

                asyncio.create_task(_wrapped())

            # 004: deliver an agent turn's final reply to the chat. The spawned run
            # (_run_and_deliver) extracts the real answer after run_session and calls this;
            # without it the interactive reply was discarded (owner saw silence).
            from core.surfaces.room_keys import is_group_session_key, owner_only_reply_target
            _deliver_chat_id = chat_id_from_session_key(result.decision.session_key)
            _room_chat_id = None
            _reply_kind = getattr(result.decision, "kind", None)
            if (_reply_kind == RouteKind.DENIED
                    and not getattr(result.decision, "silent", False)
                    and is_group_session_key(result.decision.session_key)):
                # fix-1 (round 1 review): a non-silent DENIED reply (pairing code /
                # unauthorized text) is NEVER sent in a room, and never redirected to
                # the owner either — pairing is a DM concept; the group access model
                # governs rooms. Suppress entirely.
                logger.info("telegram: DENIED reply suppressed in room %s",
                           result.decision.session_key)
                _deliver_chat_id = None
            elif _reply_kind in (RouteKind.COMMAND, RouteKind.STEER):
                # 044 T2: a command reply, or a STEER busy-branch reply (which can
                # carry an owner pause confirmation), is owner-only output. In a room
                # it goes to the owner's DM. NOT GROUP_TURN: since 044 T14 that is
                # the ROOM's own turn and its reply belongs in the room — its
                # busy/held outcomes are silent precisely so nothing owner-only can
                # arrive here.
                # 044 T18: a ROOM ADMIN'S own command reply (e.g. `/groups mode
                # here listen`, `/mute here 2h`, or a refusal from a verb he
                # isn't allowed) goes to THAT ADMIN's own DM — the owner would
                # never see it if it went to the owner's DM instead.
                if getattr(result.inbound.identity, "chat_role", None) == "admin":
                    _deliver_chat_id = (result.inbound.identity.raw_user_id
                                        or result.inbound.identity.user_id)
                else:
                    _deliver_chat_id = owner_only_reply_target(
                        result.decision.session_key, _deliver_chat_id)
                    if _deliver_chat_id is None:
                        logger.warning("owner-only reply dropped: room key %s and no owner "
                                       "telegram id", result.decision.session_key)
                # 046 T2: a handler may say its reply belongs in the ROOM. The
                # owner-only redirect above is right for `/groups` and `/paid`,
                # whose output is configuration — and WRONG for a purchase: a
                # member's paid offer is a quote addressed to the member who
                # asked, with a price and a payable address in it. Sent to the
                # owner's DM (which is what happened), the payer saw silence and
                # the whole rail was dead on arrival. A member may also have no
                # DM with the bot at all.
                _room_chat_id = chat_id_from_session_key(
                    result.decision.session_key)

            async def _deliver(text):
                if _deliver_chat_id:
                    await _send_telegram_text(self.bot, _deliver_chat_id, text)

            try:
                reply = await act_on_inbound(
                    self.task_agent, result, spawn=_spawn_with_typing, deliver=_deliver,
                    fetch_media=self._fetch_media_bytes,
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
                    # 044 T2: same owner-only redirect as the other breadcrumb.
                    await self._send_owner_only(
                        result.decision.session_key, chat_id,
                        "⚠️ Something went wrong handling that — please try again.")
                return {"ok": True}
            if reply:
                # Immediate-reply branches (DENIED / COMMAND / busy) never spawn, so the
                # _wrapped finally never runs -> delete the status bubble here, BEFORE the
                # reply (so the user never sees status + answer stacked).
                if tracker is not None:
                    tracker.close()
                await reporter.finish()
                # 044 T2/fix-1: reuse the same owner-aware target computed above
                # (COMMAND/STEER redirect, DENIED-in-room suppression) instead of
                # re-deriving the raw room chat id — otherwise these replies would
                # still leak into (or be silently dropped from) the room incorrectly.
                from core.surfaces.command_reply import (reply_media, reply_text,
                                                          reply_to_room)
                reply_chat_id = _deliver_chat_id
                if reply_to_room(reply) and _room_chat_id:
                    reply_chat_id = _room_chat_id
                if reply_chat_id:
                    await _send_telegram_text(self.bot, reply_chat_id,
                                              reply_text(reply))
                    _media_failed = 0
                    for entry in reply_media(reply):
                        # Fail-open per entry, exactly like every other media
                        # send here: a card that will not render must never take
                        # the offer text down with it.
                        try:
                            await self._send_reply_media(reply_chat_id, entry)
                        except Exception as e:
                            _media_failed += 1
                            logger.warning("telegram: reply media failed (%s)", e)
                    if _media_failed:
                        # ⚠️ Said, not just logged. The text above carries every
                        # payable fact, so the reader needs to know the picture
                        # is MISSING rather than wonder whether they missed it.
                        await _send_telegram_text(
                            self.bot, reply_chat_id,
                            "(I could not attach the image — everything you "
                            "need is in the message above.)")
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

    harness = TelegramHarness(
        bot, container, task_agent,
        webhook_base=webhook_base, dedup=dedup, user_directory=user_directory,
        poll_timeout=poll_timeout,
    )
    # 044 T19: register the harness itself so a seat that isn't Telegram-shaped
    # (group_ops, cron delivery) can reach `suggest_admins` without importing
    # this module — mirrors the `user_directory` registration above.
    if container.get_service("telegram_harness") is None:
        container.register_service("telegram_harness", harness)
    return harness


# R-4: register THE shared inbound dispatch with the core-owned contract so
# core.surfaces.inbound_webhook can delegate without importing the surface tier.
from core.surfaces.act import register_inbound_actor  # noqa: E402

register_inbound_actor(act_on_inbound)
