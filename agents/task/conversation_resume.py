"""ConversationResumeMixin — dead-session correspondent conversation resume.

Extracted (2026-07-23, file-size ratchet fix) from ``agents/task_agent_lite.py``
where it landed as part of T1.4 (single-flight ``_try_conversation_resume``).
Pure code-motion — logic is byte-preserved, only the module/class wrapper and
imports changed. See ``docs/*`` T1.4 history for the single-flight contract:
serialized per ``dead_session_id`` via ``TaskAgent._resume_locks`` so two
concurrent correspondent replies for the same dead session can't both mint a
replacement session / double-rebind the correspondent registry.

Composed into ``TaskAgent`` (``agents/task_agent_lite.py``); relies on host
attributes/methods it does not own: ``self._resume_locks``, ``self.container``,
``self._registry``, ``self.session_manager``, ``self._resolve_or_recreate``,
``self.create_session``, ``self.run_session`` — mirrors how the
``agent/core/*`` and ``agent/session/*`` mixins reach orchestrator/agent state
via ``self``.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ConversationResumeMixin:
    """Composed into ``TaskAgent`` — see module docstring."""

    async def _try_conversation_resume(
        self,
        dead_session_id: str,
        source: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        surface: Optional[str] = None,
        store: Optional[Any] = None,
    ) -> bool:
        """E6/A6: replace a dead originating session instead of dropping the reply.

        Creates a fresh session for the binding's TENANT (never the correspondent),
        re-points the correspondent registry + conversation store at it, injects the
        reply as correspondent DATA (with the durable conversation context), and
        spawns the run. Gated ``CONVERSATION_RESUME_ENABLED`` (default ON). Returns
        True iff the reply was delivered into the replacement session.

        T1.4 (single-flight): serialized per ``dead_session_id`` via ``_resume_locks``
        so two concurrent replies for the same dead session can't both create a
        replacement session / double-rebind the correspondent registry. The loser
        re-runs the SAME registry resolution after acquiring the lock; if it now
        shows the correspondent re-pointed away from ``dead_session_id`` (or a live
        orchestrator unexpectedly exists for whatever it resolves to), it delivers
        into that session instead of minting a second one.
        """
        from core.surfaces.config import SurfaceConfig
        # Deferred import (avoids a load-time circular import with
        # agents.task_agent_lite, which imports this mixin at module scope).
        from agents.task_agent_lite import _spawn_detached
        if not SurfaceConfig.conversation_resume_enabled() or not surface:
            return False
        # 031 owner pause: minting a replacement session and running it is
        # autonomous work — held (the registry/store rows are untouched).
        from core.autonomy_control import allows as _allows
        _dec = _allows("resume_session")
        if not _dec.allowed:
            logger.warning("conversation resume HELD for %s (%s)", dead_session_id, _dec.reason)
            return False
        if not hasattr(self, "_resume_locks"):
            self._resume_locks = {}
        lock = self._resume_locks.setdefault(dead_session_id, asyncio.Lock())
        async with lock:
            try:
                container = getattr(self, "container", None)
                registry = (container.get_service("correspondent_registry")
                            if container else None)
                if registry is None:
                    return False
                row = registry.resolve(surface=surface, address=source)
                user_id = (row or {}).get("user_id")
                if not user_id:
                    return False

                # Post-acquire re-check: another flight may have already resumed this
                # correspondent while we waited on the lock. If the registry no longer
                # points at dead_session_id (already rebound by the winner) — or a live
                # orchestrator unexpectedly exists for whatever it currently resolves
                # to — deliver into THAT session via the same delivery pathway below
                # and return, instead of minting a second replacement session.
                current_sid = (row or {}).get("session_id")
                already_live = (self._registry.get(current_sid)
                                if current_sid else None)
                if current_sid and (current_sid != dead_session_id
                                     or already_live is not None):
                    orch = already_live
                    if orch is None:
                        orch = await self._resolve_or_recreate(
                            current_sid,
                            self.session_manager.get_session_info(current_sid) or {})
                    if orch is None:
                        return False
                    ctx = ""
                    if store is not None:
                        try:
                            ctx = store.format_context(user_id, surface, source)
                        except Exception:
                            ctx = ""
                    text_to_inject = f"{ctx}\n\n[new message]\n{text}" if ctx else text
                    delivered = orch.inject_correspondent_message(
                        text_to_inject, source, metadata, surface=surface,
                        address=source)
                    if not delivered:
                        return False
                    if store is not None:
                        try:
                            store.record_inbound(user_id, surface, source, text,
                                                 mid=(metadata or {}).get("message_id"),
                                                 session_id=current_sid)
                        except Exception:
                            pass
                    _spawn_detached(self.run_session(user_id, current_sid))
                    logger.info(
                        f"correspondent conversation resume race: {surface}:{source} "
                        f"already resumed to {current_sid} while {dead_session_id} "
                        "was pending — delivering there instead of minting a second "
                        "session")
                    return True

                ctx = ""
                if store is not None:
                    try:
                        ctx = store.format_context(user_id, surface, source)
                    except Exception:
                        ctx = ""
                task_text = (
                    f"[conversation-resume] {surface}:{source} sent a message to a "
                    f"conversation whose original session ({dead_session_id}) is no longer "
                    "available. Their message arrives as correspondent DATA in this "
                    "session; review the conversation context and respond appropriately.")
                info = await self.create_session(user_id, task_text)
                new_sid = (info or {}).get("session_id")
                if not new_sid:
                    return False
                orch = self._registry.get(new_sid)
                if orch is None:
                    orch = await self._resolve_or_recreate(
                        new_sid, self.session_manager.get_session_info(new_sid) or {})
                if orch is None:
                    return False
                text_to_inject = f"{ctx}\n\n[new message]\n{text}" if ctx else text
                delivered = orch.inject_correspondent_message(
                    text_to_inject, source, metadata, surface=surface, address=source)
                if not delivered:
                    return False
                try:
                    registry.rebind_session(surface=surface, address=source,
                                            user_id=user_id, new_session_id=new_sid)
                except Exception:
                    pass
                if store is not None:
                    try:
                        store.rebind_session(user_id, surface, source, new_sid)
                        store.record_inbound(user_id, surface, source, text,
                                             mid=(metadata or {}).get("message_id"),
                                             session_id=new_sid)
                    except Exception:
                        pass
                _spawn_detached(self.run_session(user_id, new_sid))
                logger.warning(
                    f"correspondent conversation resumed: {surface}:{source} re-pointed "
                    f"from dead session {dead_session_id} to new session {new_sid}")
                try:
                    from core.event_log import (event_log_enabled,
                                                                 get_event_log)
                    if event_log_enabled():
                        get_event_log().record(
                            "correspondent_resumed", user_id=user_id, session_id=new_sid,
                            source=surface,
                            attrs={"address": source, "dead_session": dead_session_id})
                except Exception:
                    pass
                return True
            except Exception as e:
                logger.error(f"conversation resume failed for {source}: {e}", exc_info=True)
                return False
