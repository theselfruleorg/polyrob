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


# Telegram's hard per-message text cap. A reply longer than this makes
# ``bot.send_message`` raise TelegramBadRequest("message is too long") — with no
# chunking, that exception was swallowed fail-open and the owner got NOTHING.
TELEGRAM_MAX_MESSAGE_LEN = 4096


def _split_for_telegram(text: str, limit: Optional[int] = None) -> list:
    """Split ``text`` into chunks Telegram will accept as separate messages.

    Splits on line boundaries where possible so chunks don't cut mid-sentence; a
    single line longer than ``limit`` is hard-cut. ``limit`` defaults to
    ``TELEGRAM_MAX_MESSAGE_LEN``, read at call time (not def time) so tests can
    monkeypatch the module-level cap.
    """
    if limit is None:
        limit = TELEGRAM_MAX_MESSAGE_LEN
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


async def _send_telegram_text(bot, chat_id, text: str, **kwargs) -> None:
    """Send ``text`` to ``chat_id``, splitting across multiple messages if it
    exceeds Telegram's per-message length cap (see ``TELEGRAM_MAX_MESSAGE_LEN``)."""
    for chunk in _split_for_telegram(text):
        await bot.send_message(chat_id, chunk, **kwargs)


def _is_conflict_error(exc: Exception) -> bool:
    """True if *exc* is a Telegram getUpdates 'Conflict' (another instance polling)."""
    if _TelegramConflictError is not None and isinstance(exc, _TelegramConflictError):
        return True
    name = type(exc).__name__
    return "Conflict" in name or "terminated by other getUpdates" in str(exc)

_DEFAULT_WEBHOOK_PATH = "/telegram/webhook"

_HELP = (
    "ROB commands:\n"
    "/task <goal> — start a new task\n"
    "/cancel — stop the current task\n"
    "/new — start a fresh conversation\n"
    "/pending — proposals I've learned, awaiting your approval\n"
    "/approve <id> — activate a pending proposal\n"
    "/reject <id> — discard a pending proposal\n"
    "/asks — what I need from you to unblock work\n"
    "/fulfill <id> — mark an ask fulfilled (unblocks its goals)\n"
    "/allow <surface> <target> — allow me to message that target\n"
    "/deny <surface> <target> — revoke that permission\n"
    "/allowlist — show who I'm allowed to message\n"
    "/status — session + autonomy snapshot\n"
    "/recap [window] — what I've done (default 24h, e.g. 30m/24h/7d; alias /journey)\n"
    "/goals — goal board summary\n"
    "/prefs — your effective preferences (read-only)\n"
    "/config — read or set preferences (safe keys write immediately; "
    "guarded keys queue for /pending review)\n"
    "/kb <query> — search my knowledge base\n"
    "/files [n] — recent files I produced (default 10)\n"
    "/help — show this help\n"
    "Or just send a message to talk to ROB."
)

_OWNER_ADMIN_COMMANDS = ("/pending", "/approve", "/reject", "/asks", "/fulfill",
                         "/allow", "/deny", "/allowlist",
                         "/status", "/recap", "/journey", "/goals", "/prefs", "/config",
                         "/kb", "/files")


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
            from core.surfaces.llm_outage_notice import (
                OUTAGE_NOTICE_TEXT,
                looks_like_llm_outage,
                should_send_llm_outage_notice,
            )
            if looks_like_llm_outage(status, _last_error_text(task_agent, session_id)):
                if not should_send_llm_outage_notice(notice_key or session_id):
                    # Flag off, or within the cooldown window: deliberately
                    # silent (the failure itself is logged above; the first
                    # notice of the window already told this chat).
                    logger.info(
                        "telegram: LLM-outage notice suppressed for session %s "
                        "(flag off or cooldown)", session_id,
                    )
                    return
                reply = OUTAGE_NOTICE_TEXT
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
    """owner-UX P4 T2: one-message snapshot — bound-session state + an autonomy
    one-liner. Degrades gracefully: model/ctx% are only shown for a RESIDENT
    session (no new plumbing — reused from what `orch.agents`/`message_manager`
    already expose); a missing goal board / cron store / ledger never raises,
    it's just omitted from the line.

    ``board`` lets a caller that already opened a ``GoalBoard`` for this
    request (e.g. ``_handle_owner_admin``) share that connection instead of
    this function opening a second one against the same ``goals.db`` (owner-UX
    P4 T4 review hardening); a bare call without one still opens its own.
    """
    lines = ["Status:"]
    if not session_id:
        lines.append("• Session: no active session.")
    else:
        short = session_id[:12] + ("…" if len(session_id) > 12 else "")
        session_line = f"• Session: bound ({short})"
        try:
            get_orch = getattr(task_agent, "get_orchestrator", None)
            orch = get_orch(session_id) if get_orch is not None else None
            if orch is None:
                session_line += " — idle (not resident)."
            else:
                has_pending = getattr(task_agent, "_session_has_pending_input", None)
                busy = bool(has_pending(session_id)) if has_pending is not None else False
                session_line += " — running." if busy else " — idle."
                try:
                    agent_obj = next(iter((getattr(orch, "agents", None) or {}).values()), None)
                    if agent_obj is not None:
                        model = getattr(agent_obj, "model_name", None)
                        mm = getattr(agent_obj, "message_manager", None)
                        ctx_pct = mm.get_context_usage_percent() if mm is not None else None
                        extra = []
                        if model:
                            extra.append(f"model={model}")
                        if ctx_pct is not None:
                            extra.append(f"ctx={ctx_pct:.0f}%")
                        if extra:
                            session_line += " " + " ".join(extra)
                except Exception as e:
                    logger.debug("telegram /status agent detail skipped: %s", e)
        except Exception as e:
            logger.debug("telegram /status session check failed: %s", e)
            session_line += " — unknown."
        lines.append(session_line)

    try:
        from agents.task.goals.board import GoalBoard, STATUS_READY, STATUS_RUNNING
        gb = board if board is not None else GoalBoard(os.path.join(data_dir, "goals.db"))
        open_n = len(gb.list(user_id=user_id, status=STATUS_READY, limit=1000))
        running_n = len(gb.list(user_id=user_id, status=STATUS_RUNNING, limit=1000))
        autonomy = f"• Goals: {open_n} open, {running_n} running."
    except Exception as e:
        logger.debug("telegram /status goal counts failed: %s", e)
        autonomy = "• Goals: unavailable."
    try:
        from cron.jobs import CronJobStore
        from cron.service import CronService
        svc = CronService(CronJobStore(os.path.join(data_dir, "cron.db")))
        upcoming = [j.next_run_at for j in svc.list_jobs(user_id=user_id)
                   if j.enabled and j.next_run_at]
        if upcoming:
            autonomy += f" Next cron: {min(upcoming).isoformat(timespec='minutes')}."
    except Exception as e:
        # Not cheaply reachable (or no cron for this tenant) — omit, no error.
        logger.debug("telegram /status cron lookup omitted: %s", e)
    lines.append(autonomy)

    try:
        from agents.task.constants import autonomy_mode_display
        lines.append(f"• Autonomy mode: {autonomy_mode_display()}")
    except Exception as e:
        logger.debug("telegram /status autonomy mode line failed: %s", e)

    try:
        from modules.credits.unified_ledger import build_ledger
        ledger = await build_ledger(user_id, days=1, include_balances=True)
        r = ledger.get("runtime") or {}
        t = ledger.get("treasury") or {}
        spend = float(r.get("spend_window_usd") or 0.0)
        total = float(r.get("spend_total_usd") or 0.0)
        lines.append(f"• Runtime cost (24h): ${spend:.2f} · ${total:.2f} total.")
        lines.append(f"• Treasury: net ${float(t.get('net_usd') or 0.0):.2f}.")
    except Exception as e:
        logger.debug("telegram /status ledger lookup failed: %s", e)
    return "\n".join(lines)


def _recap_reply(user_id: str, data_dir: str, args: list) -> str:
    """owner-UX P4 T2: `/recap [window]` (default 24h) over core.recap (T1)."""
    from core.recap import build_recap, format_recap_markdown
    window = args[0] if args else "24h"
    try:
        entries = build_recap(user_id, data_dir, window=window)
    except ValueError:
        return (f"Invalid recap window {window!r} — expected e.g. '30m' / '24h' / '7d' "
                "(a bare number of seconds also works).")
    return format_recap_markdown(entries, window)


def _goals_reply(user_id: str, data_dir: str, board: Optional[Any] = None) -> str:
    """owner-UX P4 T2: goal board summary — counts by status + up to 5 most
    recent open/running goals.

    ``board`` lets a caller share an already-open ``GoalBoard`` (see
    ``_status_reply``'s docstring); a bare call without one opens its own.
    """
    from agents.task.goals.board import (
        KIND_GOAL, STATUS_READY, STATUS_RUNNING, STATUS_TRIAGE, GoalBoard)
    gb = board if board is not None else GoalBoard(os.path.join(data_dir, "goals.db"))
    goals = [g for g in gb.list(user_id=user_id, limit=1000) if g.kind == KIND_GOAL]
    if not goals:
        return "No goals yet."
    counts: dict = {}
    for g in goals:
        counts[g.status] = counts.get(g.status, 0) + 1
    lines = [f"{len(goals)} goal(s): " +
            ", ".join(f"{status}={n}" for status, n in sorted(counts.items()))]
    open_states = (STATUS_TRIAGE, STATUS_READY, STATUS_RUNNING)
    recent = sorted((g for g in goals if g.status in open_states),
                    key=lambda g: g.created_at, reverse=True)[:5]
    if recent:
        lines.append("Recent open/running:")
        for g in recent:
            lines.append(f"• {g.id[:8]} [{g.status}] {g.title}")
    return "\n".join(lines)


def _prefs_reply(user_id: str, data_dir: str, instance_id: str) -> str:
    """owner-UX P4 T2: read-only resolved-preferences summary via the display
    SSOT (`core.prefs.display_effective`) — never the raw file, always the
    effective (pref/env/merged) value + source."""
    from core.prefs import PREF_SCHEMA, display_effective
    by_group: dict = {}
    for key in sorted(PREF_SCHEMA):
        by_group.setdefault(key.split(".", 1)[0], []).append(key)
    lines = ["Your preferences (read-only):"]
    for group in sorted(by_group):
        lines.append(f"[{group}]")
        for key in by_group[group]:
            value, source = display_effective(key, user_id, data_dir, instance_id=instance_id)
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
        return _prefs_reply(user_id, data_dir, instance_id)
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
            lines.append(f"• {path}{size_s}"
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

    if cmd in ("/recap", "/journey"):  # one recap vocabulary across surfaces
        return _recap_reply(user_id, data_dir, args)

    if cmd == "/goals":
        return _goals_reply(user_id, data_dir, board=board)

    if cmd == "/prefs":
        return _prefs_reply(user_id, data_dir, instance_id)

    if cmd == "/config":
        return _config_reply(user_id, data_dir, instance_id, args)

    if cmd == "/kb":
        if not args:
            return "Usage: /kb <query> — search the knowledge base"
        return await _kb_reply(user_id, " ".join(args))

    if cmd == "/files":
        return await _files_reply(user_id, args)

    if cmd == "/pending":
        from tools.controller.approval_queue import list_pending_tool_approvals
        items = self_evolution.list_pending(user_id, home_dir=data_dir,
                                            instance_id=instance_id)
        items = items + list_pending_tool_approvals(board, user_id)
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
            ok, msg = decide_tool_approval(board, target, user_id=user_id,
                                           approved=(cmd == "/approve"))
            return msg if ok else f"Failed: {msg}"
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

    return _HELP


async def _handle_command(task_agent: Any, result: InboundResult, spawn, deliver=None) -> Optional[str]:
    cmd = (result.decision.command or "").lower()
    if cmd == "/help":
        return _HELP
    if cmd in _OWNER_ADMIN_COMMANDS:
        try:
            return await _handle_owner_admin(task_agent, result, cmd)
        except Exception as e:
            logger.error("owner admin command %s failed: %s", cmd, e, exc_info=True)
            return f"Command failed: {e}"
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
    return _HELP  # unknown command -> help


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
    key = getattr(result.decision, "session_key", "") or ""
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

    Two transports:
      - webhook (webhook_base set): start() sets the Telegram webhook; the FastAPI
        route body is handle_update.
      - polling (webhook_base None): run_polling() long-polls getUpdates and feeds
        each update to handle_update. This is the local battle-test path — no public
        URL / SSL needed.
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
        self.bot_username: Optional[str] = None
        from surfaces.telegram.surface import TelegramSurface
        self.surface = TelegramSurface(bot)

    async def _transcribe_voice(self, update: dict):
        """Injected into process_update so the inbound spine stays transport-free (#9):
        download a voice/audio attachment and transcribe it via the shared engine.
        No-op (returns None) unless VOICE_TRANSCRIPTION_ENABLED. Fail-open.

        The transcriber is now built via get_transcriber(container) — registered once on
        the container and shared across surfaces (Task 1.6 core-seam migration)."""
        from agents.task.surface_config import SurfaceConfig
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
                    chat_id = _tg_chat_id(update)
                    if chat_id is not None:
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
                from agents.task.surface_config import SurfaceConfig
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

            # Persistent transcript echo (voice only): post '🎙️ Transcript: …' quoting the
            # voice note so the user sees what ROB heard, BEFORE the answer. Fail-open —
            # never blocks the turn. Gated VOICE_TRANSCRIPT_ECHO (default ON).
            from core.surfaces.voice_echo import voice_transcript, voice_echo_message
            from agents.task.surface_config import SurfaceConfig
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
