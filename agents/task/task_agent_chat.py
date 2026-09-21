"""Chat runtime: ``chat_once`` (the one chat front door), the per-chat lock + model hot-swap, brain-state reply extraction, ``process_user_message``.

Split out of ``agents/task_agent_lite.py`` (S8, 2026-08-29) as a mixin over ``TaskAgent``;
every method is verbatim and relies on the host for the session registry, container,
config and chat maps. Logs under the historical ``agents.task_agent_lite`` logger name.
"""
import logging
from typing import Optional, Dict, Any, Union, List
import asyncio
import json
import re
from agents.task.task_agent_support import (
    SessionRequest,
    _resolve_chat_runtime,
    _spawn_detached,
)

logger = logging.getLogger("agents.task_agent_lite")


class TaskAgentChatMixin:
    async def process_user_message(
        self,
        user_id: str,
        input_text: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Process user input - supports continuous chat.

        Flow:
        1. Check for existing session (even if completed)
        2. If exists: Queue message and resume that session
        3. If not: Create new session

        Args:
            user_id: User ID
            input_text: User's message
            metadata: Optional metadata

        Returns:
            Response message
        """
        if not self._initialized:
            await self._initialize()

        if not self.task_available:
            return "Task automation not available"

        text = input_text.strip()

        # Handle commands
        if text.startswith('/v2_status'):
            status = await self.get_session_status(user_id)
            if status['found']:
                return f"Session: {status['id']}\nStatus: {status['status']}\nTask: {status['task']}"
            return "No active session"

        if text.startswith('/v2_cancel'):
            if await self.cancel_session(user_id, force=True):
                return "Session cancelled"
            return "No session to cancel"

        if not text:
            return "Please provide a task description"

        try:
            # CONTINUOUS CHAT: Check for existing session first
            # User might be continuing a conversation from seconds/hours/months ago
            existing_session_id = None
            # Set when an existing session could not be resumed, so the new-session
            # path can tell the user instead of silently pretending to continue (B2).
            resume_failed_id = None

            # Try to get user's latest session (even if completed/suspended)
            if user_id in self.user_sessions:
                existing_session_id = self.user_sessions[user_id]
            else:
                # Check SessionManager for any sessions for this user
                all_sessions = self.session_manager._sessions
                user_session_ids = [sid for sid, info in all_sessions.items()
                                   if info.get('user_id') == user_id]
                if user_session_ids:
                    # Get most recent
                    existing_session_id = user_session_ids[-1]

            # If existing session found, queue message and resume
            if existing_session_id:
                session_info = self.session_manager.get_session_info(existing_session_id)
                if session_info:
                    logger.info(f"💬 Continuing session {existing_session_id} (status: {session_info.get('status')})")

                    # W1: a genuine user turn clears the self-wake re-entry budget so
                    # the agent can again earn forged continuations on its own work.
                    try:
                        from agents.task.constants import AutonomyConfig
                        if AutonomyConfig.self_wake_enabled():
                            from agents.task.agent.core.self_wake import get_reentry_budget
                            get_reentry_budget().reset(existing_session_id)
                    except Exception:
                        # Fail-open: reentry budget reset failure is non-critical
                        pass

                    # Get or recreate orchestrator (shared per-session lock, a2)
                    orchestrator = await self._resolve_or_recreate(existing_session_id, session_info)

                    if orchestrator:
                        # Queue message to agent via orchestrator
                        try:
                            await orchestrator.submit_user_message(
                                agent_id=None,  # Routes to first agent
                                text=text,
                                kind="continuation",
                                metadata=metadata or {}
                            )
                            logger.info(f"✅ Queued message to session {existing_session_id}")
                        except Exception as e:
                            logger.error(f"Failed to queue message: {e}")

                        # Resume session execution
                        _spawn_detached(self.run_session(user_id, existing_session_id))

                        return f"💬 Message sent to existing session\nSession: {existing_session_id}"
                    else:
                        # Could not resume (orchestrator missing AND recreation failed).
                        # Fresh-start-with-notice: rather than silently spawn a duplicate,
                        # retire the un-recreatable session from in-memory tracking so it
                        # stops leaking the per-user limit and isn't re-selected next
                        # message — history stays on disk (delete_files=False, recoverable
                        # by id) — then fall through to create a new session and tell the
                        # user what happened.
                        logger.warning(
                            f"Could not resume session {existing_session_id} "
                            f"(orchestrator recreation failed); starting a new session"
                        )
                        resume_failed_id = existing_session_id
                        self.session_manager.cleanup_session(existing_session_id, delete_files=False)
                        self._session_last_activity.pop(existing_session_id, None)
                        self._session_execution_locks.pop(existing_session_id, None)

            # No existing session or failed to resume - create new session
            logger.info(f"🆕 Creating new session for user {user_id}")
            session = await self.create_session(user_id, text, creator="owner")

            # Run in background
            _spawn_detached(self.run_session(user_id, session['id']))

            if resume_failed_id:
                return (
                    f"⚠️ Couldn't resume your previous session ({resume_failed_id[:8]}…), "
                    f"so I started a new one.\nSession: {session['id']}"
                )
            return f"🤖 Task started: {text[:100]}...\nSession: {session['id']}"

        except Exception as e:
            logger.error(f"Failed to process message: {e}")
            return f"Error: {str(e)}"
    # ------------------------------------------------------------------
    # S2 (chat consolidation): synchronous chat adapter
    # ------------------------------------------------------------------
    @staticmethod
    def _chat_key(user_id: str, chat_id: Optional[str]) -> str:
        """Durable conversation key. Falls back to user_id when no chat_id is
        given, mirroring the legacy ChatAgent's one-thread-per-user behavior."""
        return f"chat:{user_id}:{chat_id or user_id}"
    def _chat_tool_ids(self) -> List[str]:
        from agents.task.constants import CHAT_TOOL_IDS
        return list(CHAT_TOOL_IDS)
    async def _resolve_chat_persona(self) -> str:
        """Resolve the persona via the surface-shared resolver (T1-07): explicit
        POLYROB_PERSONA (template key or literal) > default character > "".
        Fail-open to "" (off-path byte-identical)."""
        try:
            from agents.personality.persona_resolver import resolve_persona
            return await resolve_persona(container=self.container)
        except Exception as e:
            logger.debug(f"chat persona resolve skipped: {e}")
            return ""
    @staticmethod
    def _looks_like_brain_state(text) -> bool:
        """True when *text* is agent brain-state telemetry, not a chat reply.

        Mirrors cli.ui.dialog.is_brain_state WITHOUT importing the CLI layer
        (agents must not depend on cli). Recognises the JSON shapes
        ({"current_state": {...}} and >=2 bare brain keys, tolerating a leading
        ```json fence) and the Memory:/Next: text echo. Live-caught on GLM, whose
        raw brain-JSON content can be the last AIMessage in history.
        """
        if not text:
            return False
        t = str(text).strip()
        # strip a single enclosing markdown code fence (DeepSeek JSON-fallback path)
        if t.startswith("```") or t.startswith("~~~"):
            m = re.match(r"^\s*(?:```|~~~)[^\n]*\n(.*?)\n?(?:```|~~~)\s*$", t, re.DOTALL)
            if m:
                t = m.group(1).strip()
        from modules.llm.brain_scrubber import BRAIN_KEYS as _brain_keys  # ONE set (S7)
        if t.startswith("{"):
            try:
                obj, _end = json.JSONDecoder().raw_decode(t)
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict):
                if isinstance(obj.get("current_state"), dict):
                    return True
                if sum(1 for k in obj if k in _brain_keys) >= 2:
                    return True
        # Memory:/Next: echo form (require both so prose with a stray header isn't caught)
        has_mem = re.search(r"(?im)^\s*memory\s*:", t)
        has_next = re.search(r"(?im)^\s*next(?:[_ ]?goal)?\s*:", t)
        return bool(has_mem and has_next)
    def _extract_chat_reply(self, session_id: str) -> str:
        """Return the agent's ACTUAL last assistant reply for a finished turn.

        Priority (corrected after a live GLM brain-JSON leak):
        1. The clean done() output — history.final_result() when is_done() — since
           done() adds its clean message to history BEFORE the atomic add of the
           model's raw content (which may be brain-state JSON), so "last AIMessage"
           alone would capture telemetry.
        2. The conversational/send path: the last NON-brain AIMessage (the real
           send_message text; ActionResult.extracted_content is only a placeholder).
        3. final_result() as a last resort.
        Brain-state telemetry is never returned; the generic run_session string never is.
        """
        try:
            orch = self._registry.get(session_id)
            if not orch or not getattr(orch, 'agents', None):
                return ""
            agent = next(iter(orch.agents.values()), None)
            if agent is None:
                return ""

            # The ledger lives on agent.history (AgentHistoryList) — AgentState has
            # NO history field, so the old `agent.state.history` read ALWAYS raised
            # and this path silently never ran (the goal-58a1385d18bf corruption:
            # priority-2 then returned the P2-16 placeholder AIMessage).
            hist = getattr(agent, 'history', None)
            from agents.task.runtime.run_outcome import FRAMEWORK_PLACEHOLDER_TEXTS

            # 0) C3: the reply this turn actually spoke, recorded where it was
            # published. This outranks done() — F4: preferring done's text meant
            # every unbound path (raw API, chat_once, /v1, a surface without the
            # bus) delivered the third-person bookkeeping recap INSTEAD of the
            # answer. Ordering over history cannot fix it, because done writes
            # "✅ Task Complete\n\n<text>" and so IS the last AIMessage.
            try:
                from core.surfaces.turn_reply import last_reply_text
                spoken = last_reply_text(orch)
                if spoken and not self._looks_like_brain_state(spoken) \
                        and spoken.strip() not in FRAMEWORK_PLACEHOLDER_TEXTS:
                    return spoken.strip()
            except Exception:
                # Fail-open: fall through to the legacy history scan.
                pass

            # 1) clean done() output
            try:
                if hist is not None and hist.is_done():
                    fr = hist.final_result()
                    if fr and not self._looks_like_brain_state(fr) \
                            and str(fr).strip() not in FRAMEWORK_PLACEHOLDER_TEXTS:
                        return str(fr).strip()
            except Exception:
                # Fail-open: done() output extraction failure is non-critical
                pass

            # 2) last non-brain AIMessage (conversational/send path)
            mm = getattr(agent, 'message_manager', None)
            if mm is not None:
                try:
                    from modules.llm.messages import AIMessage
                    for managed in reversed(list(mm.history.messages)):
                        msg = getattr(managed, 'message', None)
                        content = getattr(msg, 'content', None)
                        if isinstance(msg, AIMessage) and isinstance(content, str) and content.strip():
                            text = content.strip()
                            # Framework placeholders ("Processing actions", …) are
                            # display scaffolding, never the agent's reply — skip.
                            if text in FRAMEWORK_PLACEHOLDER_TEXTS:
                                continue
                            prefix = "✅ Task Complete\n\n"
                            if text.startswith(prefix):
                                text = text[len(prefix):].strip()
                            if text and not self._looks_like_brain_state(text):
                                return text
                except Exception as e:
                    logger.debug(f"chat reply scan failed: {e}")

            # 3) last-resort fallback
            try:
                fr = hist.final_result() if hist is not None else None
                if fr and not self._looks_like_brain_state(fr) \
                        and str(fr).strip() not in FRAMEWORK_PLACEHOLDER_TEXTS:
                    return str(fr).strip()
            except Exception:
                # Fail-open: history scan failure is non-critical, try next fallback
                pass
        except Exception as e:
            logger.debug(f"_extract_chat_reply failed: {e}")
        return ""
    async def chat_once(
        self,
        user_id: str,
        text: str,
        chat_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """Run ONE synchronous chat turn on the unified task agent and return the
        assistant's reply text.

        ``temperature`` (2026-09-21, interface audit B18) is an optional
        per-request sampling override from the OpenAI-compat surface: baked into
        the SessionRequest for a brand-new session, and applied to the live
        adapter's default for a reused one (``_apply_temperature``). ``None`` =
        the agent's configured default, byte-identical to before.

        This is the synchronous counterpart to the fire-and-forget
        process_user_message: it awaits run_session and returns the real reply
        (not the "Task started…" ack, not run_session's generic completion
        string). It reuses a durable session keyed by (user_id, chat_id) so a
        follow-up continues the same conversation, and runs tool-light.

        `provider`/`model` (B3) are an optional PER-REQUEST model override —
        e.g. from the OpenAI-compat `/v1/chat/completions` `body.model` field
        via `map_model()`. When set, they steer this turn's model (and, since
        chat sessions are durable, every subsequent turn of this (user_id,
        chat_id) session until overridden again). See `_chat_once_locked` for
        how the override is applied (live swap_model for a reused session;
        baked into the SessionRequest for a brand-new one).
        """
        if not self._initialized:
            await self._initialize()
        if not self.task_available:
            return "Task automation not available"

        text = (text or "").strip()
        if not text:
            return "Please provide a message"

        key = self._chat_key(user_id, chat_id)
        locks = getattr(self, "_chat_locks", None)
        if locks is None:
            locks = {}
            self._chat_locks = locks
        lock = locks.get(key) or locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                result = await self._chat_once_locked(
                    user_id, text, key, provider=provider, model=model,
                    temperature=temperature,
                )
        finally:
            # Evict the lock only if this turn left no active session mapping
            # for `key` (e.g. session creation raised or failed) -- a
            # successful turn always re-populates self._chat_sessions[key], so
            # this is a no-op on the common path. Safe without extra locking:
            # no `await` sits between the membership check and the pop, so
            # asyncio cannot interleave another task's get-or-create for the
            # same key in between (task switches only happen at await points).
            if key not in self._chat_sessions:
                locks.pop(key, None)
        return result
    async def _maybe_swap_chat_model(
        self, orch, provider: Optional[str], model: str,
    ) -> None:
        """Apply a B3 per-request model override to a REUSED session's live
        agent, idempotently.

        Uses the same accessor `_extract_chat_reply` uses (`orch.agents` is a
        dict; chat sessions run a single agent, so the first value is it). A
        no-op when no live agent exists yet (e.g. mid-creation) or when the
        requested (provider, model) already matches the agent's current
        SSOT (`model_name`/`llm_provider`) — a falsy `provider` never counts
        as "different", it means swap_model should auto-detect. This guard
        is what stops an unchanged `body.model` from rebuilding the LLM on
        every single request. A failed swap is logged and the turn proceeds
        on the agent's current (unchanged) model.
        """
        agent = next(iter(orch.agents.values()), None) if getattr(orch, "agents", None) else None
        if agent is None or not hasattr(agent, "swap_model"):
            return
        same_model = getattr(agent, "model_name", None) == model
        # A never-swapped agent never has `.llm_provider` set to the request's
        # provider label the way swap_model would — it may be None (older builds)
        # or only the Agent-level mirror. Fall back to the MessageManager-backed
        # `provider_name` SSOT so an unchanged model doesn't rebuild the LLM on
        # every request (B3 idempotence). A falsy request provider never counts
        # as "different" (auto-detect).
        current_provider = (
            getattr(agent, "llm_provider", None) or getattr(agent, "provider_name", None)
        )
        same_provider = (not provider) or current_provider == provider
        if same_model and same_provider:
            return
        # A failed swap must warn + proceed on the current model — an exception
        # from swap_model (e.g. a provider build blowing up) must never 500 the
        # /v1 request (the plan's contract: failed swap = warn + proceed).
        try:
            res = await agent.swap_model(provider, model)
        except Exception as e:
            logger.warning(
                f"per-request model swap raised: {e} — "
                f"turn continues on {getattr(agent, 'model_name', '?')}"
            )
            return
        if not res.get("ok"):
            logger.warning(
                f"per-request model swap failed: {res.get('error')} — "
                f"turn continues on {getattr(agent, 'model_name', '?')}"
            )
    @staticmethod
    def _apply_temperature(orch, temperature: Optional[float]) -> None:
        """Set the live adapter's default temperature for a REUSED chat session.

        The native adapters capture their default at construction
        (``modules/llm/adapters.py::_default_temperature``) and read it on every
        call, so writing it is the whole override — no LLM rebuild. A missing
        attribute (a non-native client) is a documented no-op, never an error.
        """
        if temperature is None:
            return
        agent = next(iter(orch.agents.values()), None) if getattr(orch, "agents", None) else None
        llm = getattr(agent, "llm", None)
        if llm is not None and hasattr(llm, "_default_temperature"):
            try:
                llm._default_temperature = float(temperature)
            except (TypeError, ValueError):
                logger.warning("per-request temperature %r ignored (not a number)", temperature)

    async def _chat_once_locked(
        self,
        user_id: str,
        text: str,
        key: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """The body of chat_once, run under the per-chat-key lock (see chat_once)."""
        session_id = self._chat_sessions.get(key)

        # Validate a remembered session still exists; else drop and recreate.
        if session_id and not self.session_manager.get_session_info(session_id):
            self._chat_sessions.pop(key, None)
            session_id = None

        # C1: expand @file/@folder/@diff/@url references (opt-in, fail-soft).
        # Use the session workspace when available; fall back to CWD for new sessions.
        try:
            from agents.task.constants import AutonomyConfig
            if AutonomyConfig.context_references_enabled():
                from agents.task.agent.messages.context_references import (
                    preprocess_context_references,
                )
                from agents.task.path import pm
                import os as _os
                _root = str(pm().get_workspace_dir(session_id, user_id)) if session_id else _os.getcwd()
                text = preprocess_context_references(
                    text, root=_root, confine_to_root=True, allow_filesystem=True
                )
        except Exception:
            pass  # fail-soft: leave text unchanged

        persona = await self._resolve_chat_persona()

        if session_id:
            # Reuse: queue a continuation and resume (await, unlike the
            # background process_user_message path).
            orch = self._registry.get(session_id)
            if not orch:
                info = self.session_manager.get_session_info(session_id)
                orch = await self._resolve_or_recreate(session_id, info) if info else None
            if orch:
                if persona:
                    orch._persona_block = persona
                if model:
                    await self._maybe_swap_chat_model(orch, provider, model)
                self._apply_temperature(orch, temperature)
                try:
                    await orch.submit_user_message(
                        agent_id=None, text=text, kind="continuation",
                    )
                except Exception as e:
                    logger.warning(f"chat_once: failed to queue continuation: {e}")
            else:
                # Un-recreatable — drop the mapping and fall through to create.
                self._chat_sessions.pop(key, None)
                session_id = None

        if not session_id:
            from agents.task.constants import CHAT_MAX_STEPS
            # Provider/model via the one shared resolver (Seam 2): CHAT_/DEFAULT_ env
            # pins win; else the historical openai/gpt-5 default if OpenAI is keyed;
            # else the first keyed provider (fixes only-one-key chat). Model always
            # matches the resolved provider.
            _provider, _model = _resolve_chat_runtime()
            # B3: a per-request model override for a BRAND-NEW session has no
            # live agent yet to swap_model() (that's created later, inside
            # run_session) — bake it into the SessionRequest instead, the
            # same knob CHAT_PROVIDER/CHAT_MODEL already use above.
            if model:
                _model = model
                _provider = provider or _provider
            req_kwargs = dict(
                task=text,
                tools=self._chat_tool_ids(),
                max_steps=CHAT_MAX_STEPS,
                use_vision=False,
                provider=_provider,
                model=_model,
            )
            if temperature is not None:
                req_kwargs["temperature"] = float(temperature)
            req = SessionRequest(**req_kwargs)
            from agents.task.constants import CHAT_SKIP_CREDIT_CHECK
            info = await self.create_session(
                user_id, req, chat_session_key=key,
                skip_credit_check=CHAT_SKIP_CREDIT_CHECK,
                creator="owner",
            )
            session_id = info['id']
            self._chat_sessions[key] = session_id
            orch = self._registry.get(session_id)
            if orch and persona:
                orch._persona_block = persona

        run_result = await self.run_session(user_id, session_id)
        # Final-review fix (T1.1): a RUN_BUDGET_USD halt ends the turn BEFORE
        # any step runs (or right after one), so `_extract_chat_reply`'s
        # fallback (last AIMessage / final_result) would return the reply to
        # the PREVIOUS turn — or "" — instead of the honest halt text.
        # run_session's return string IS that honest text
        # ("Session failed: run_budget_exhausted: ...") for exactly this
        # case; surface it directly rather than falling through to the
        # (now-stale) extraction. Scoped to the budget-halt marker only —
        # other failure strings keep their pre-existing (stale-extraction)
        # behavior, which is out of scope here.
        if isinstance(run_result, str) and run_result.startswith("Session failed:"):
            try:
                from agents.task.agent.core.run_budget import RUN_BUDGET_MARKER
            except ImportError:
                RUN_BUDGET_MARKER = "run_budget_exhausted"
            if RUN_BUDGET_MARKER in run_result:
                return run_result
        return self._extract_chat_reply(session_id) or ""
