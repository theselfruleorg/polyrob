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
from surfaces.telegram.inbound import InboundResult

logger = logging.getLogger(__name__)

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
    "/help — show this help\n"
    "Or just send a message to talk to ROB."
)


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


async def _run_and_deliver(task_agent: Any, user_id: str, session_id: str, deliver) -> None:
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
    """
    await task_agent.run_session(user_id, session_id)
    if deliver is None:
        return
    # C10: when Singular Chat is bound, the send_message / done router mirror already
    # delivered the reply LIVE (MarkdownV2). Delivering again here would double-send (raw
    # plain text). Skip for bound sessions; keep the post-run deliver for unbound/legacy
    # paths (cron, `polyrob run`, raw API) that have no router.
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
        logger.info("telegram: session %s produced no deliverable reply", session_id)
        return
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
        _spawn(_run_and_deliver(task_agent, inbound.identity.user_id, session_id, deliver), spawn)


async def _handle_command(task_agent: Any, result: InboundResult, spawn, deliver=None) -> Optional[str]:
    cmd = (result.decision.command or "").lower()
    if cmd == "/help":
        return _HELP
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
    """
    decision = result.decision
    kind = decision.kind

    if kind == RouteKind.DENIED:
        # Ingress blocked (pairing required / not paired). NEVER run the agent on a
        # denied message — return a user-facing message (with the pairing code, if any)
        # instead of falling through to _start_task_session.
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
            await task_agent.deliver_correspondent_data(
                decision.session_id, src, result.inbound.text,
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
                                    decision.session_id, deliver), spawn)
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

            reply = await act_on_inbound(
                self.task_agent, result, spawn=_spawn_with_typing, deliver=_deliver,
            )
            if reply:
                # Immediate-reply branches (DENIED / COMMAND / busy) never spawn, so the
                # _wrapped finally never runs -> delete the status bubble here, BEFORE the
                # reply (so the user never sees status + answer stacked).
                await reporter.finish()
                reply_chat_id = chat_id_from_session_key(result.decision.session_key)
                await _send_telegram_text(self.bot, reply_chat_id, reply)
            elif not spawned["v"]:
                # No reply AND nothing spawned (e.g. create_session yielded no id) -> the
                # finally will never run; clear the status bubble so it never orphans.
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
    """

    def __init__(self, bot):
        self._bot = bot

    async def send_message(self, chat_id, text) -> bool:
        try:
            cid = int(chat_id) if str(chat_id).isdigit() else chat_id
            await _send_telegram_text(self._bot, cid, text)
            return True
        except Exception:  # fail-open: delivery must never crash the caller
            logger.warning("TelegramBotSink.send_message failed for chat %s", chat_id,
                           exc_info=True)
            return False


def build_telegram_harness(container, task_agent, *, token, webhook_base=None, bot=None,
                           data_dir="data", poll_timeout: int = 30):
    """Assemble a TelegramHarness. Lazy-imports aiogram for the real Bot (tests inject
    a fake). Ensures a UserDirectory + UpdateDedup exist on the shared data dir.

    webhook_base=None -> polling mode (local). Pass a base URL for webhook mode.
    """
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
