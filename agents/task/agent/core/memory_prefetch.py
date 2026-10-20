"""Memory prefetch mixin (roadmap P7 / B-T2 — Reference-parity).

Routes the agent's current query through the active memory provider
(``modules.memory.registry``) at the start of a step and injects any recalled
context as a one-shot ``RECALL``-origin message. Inert by default: the registry's
default provider is ``NullMemoryProvider`` (returns ""), so nothing is injected and
production behaviour is unchanged until an external provider is registered.

Fail-open throughout — a memory backend hiccup never breaks the agent loop.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from modules.llm.messages import BaseMessage
from core.config_policy import memory_prefetch_cadence

logger = logging.getLogger(__name__)

# B1 (2026-10-11): one recall injected ~25,000 chars (5 rows of up to 4,000 + 8 KB
# chunks) on step 1 and every cadence step, and chat context sat above its
# compaction target. The injection is bounded HERE, for every provider.
#: Max chars of one recalled entry (a memory row or a KB chunk).
RECALL_ENTRY_MAX_CHARS = 1500
#: Max chars of the cross-session memory section.
RECALL_MEMORY_MAX_CHARS = 6000
#: Max chars of the knowledge-base section, and how many chunks it asks for.
RECALL_KB_MAX_CHARS = 3000
RECALL_KB_LIMIT = 4

_MEM_ENTRY = re.compile(r"\n(?=- )")                 # "- [day] content" bullets
_KB_ENTRY = re.compile(r"\n(?=\[[^\]\n]+ #\d+\] )")  # "[source #idx] content"


_ENTRY_HEAD = re.compile(r"^(?:- (?:\[\d{4}-\d{2}-\d{2}\] )?|\[[^\]\n]+ #\d+\] )")


_UNVERIFIED_MARK = "[unverified] "


def _clip_entry(entry: str, cap: int, terms: Optional[list]) -> str:
    """Clip one long entry to ``cap`` chars AROUND the part that matched.

    D10 (review 2026-10-13): a long multi-finding row wins the OR/bm25 rank
    because it matches many terms, and the old head-only clip then cut the
    sentence that matched. Keep the entry's head marker (``- [date] `` /
    ``[source #i] ``) and show the window with the most query terms."""
    if len(entry) <= cap:
        return entry
    m = _ENTRY_HEAD.match(entry)
    head = m.group(0) if m else ""
    body = entry[len(head):]
    room = max(cap - len(head) - 2, 1)
    start = 0
    words = [t.lower() for t in (terms or []) if t]
    if words:
        low = body.lower()
        hits = sorted(i for w in words for i in _find_all(low, w))
        best, best_n = 0, 0
        for h in hits:
            s0 = max(0, h - room // 5)
            n = len({w for w in words if w in low[s0:s0 + room]})
            if n > best_n:
                best, best_n = s0, n
        start = best
    window = body[start:start + room]
    # D6: a finding's "[unverified] " marker leads its LINE; a window that
    # starts inside that line must keep it, or a guess reads as a fact.
    line_start = body.rfind("\n", 0, start) + 1 if start > 0 else 0
    if start > line_start and body.startswith(_UNVERIFIED_MARK, line_start):
        window = _UNVERIFIED_MARK + window[:max(room - len(_UNVERIFIED_MARK), 1)]
    return (head + ("…" if start > 0 else "") + window
            + ("…" if start + room < len(body) else ""))


def _find_all(hay: str, needle: str, limit: int = 20):
    i, n = hay.find(needle), 0
    while i != -1 and n < limit:
        yield i
        i, n = hay.find(needle, i + 1), n + 1


def _budget(text: str, splitter, cap: int, seen: Optional[set],
            terms: Optional[list] = None) -> str:
    """Clip each entry, drop entries ``seen`` already holds, stop at ``cap``.

    ``seen`` (the run's injected entries) is updated with what is kept, so a
    later cadence step never re-injects a line the run already got. ``terms``
    (the query's informative terms) centre the clip on the matched part."""
    out, used = [], 0
    for entry in splitter.split(text):
        entry = entry.strip()
        if not entry:
            continue
        key = " ".join(entry.split())
        if seen is not None and key in seen:
            continue
        if len(entry) > RECALL_ENTRY_MAX_CHARS:
            entry = _clip_entry(entry, RECALL_ENTRY_MAX_CHARS, terms)
        if out and used + len(entry) + 1 > cap:
            break
        if not out and len(entry) > cap:
            entry = entry[:cap - 1] + "…"
        out.append(entry)
        used += len(entry) + 1
        if seen is not None:
            seen.add(key)
    return "\n".join(out)


#: D3 / E-A9: the one-line RECALL note when the store could not be read.
RECALL_READ_ERROR_NOTE = (
    "Memory recall is unavailable this turn ({error_class}) — the store could not "
    "be read. Do not state that something was never recorded or never happened; "
    "say that memory could not be checked.")
#: D5: owner-saved console notes injected with recall (owner-memory regimes only).
OWNER_NOTES_MAX = 10
OWNER_NOTES_MAX_CHARS = 2000


def memory_read_error(exc: BaseException, what: str = "Memory") -> str:
    """The agent-facing text for a recall READ failure (D3 / D14): names the
    error class and forbids the "never recorded" reading. Mirrors the KB branch
    of ``session_search`` ("…failed; nothing was searched")."""
    err = getattr(exc, "error_class", None) or type(exc).__name__
    return (f"{what} could not be read ({err}); nothing was searched. Do not state that "
            "something was never recorded or is empty — say memory could not be checked.")


def recall_reader(agent=None, *, orchestrator=None, session_id: Optional[str] = None,
                  is_sub_agent: bool = False) -> dict:
    """The reader description the recall routers forward to the provider.

    From the session regime (``agents.task.session_class``): an owner chat
    demotes cron/goal rows; a cron / goal run does not, and names its own job /
    goal so its past runs rank first (review D2 / A7). ``modules`` cannot ask
    for the regime itself (layering), so the agents tier passes plain values.
    Fail-open to {} (the provider's legacy default)."""
    try:
        from agents.task.session_class import CHILD, REGIME_POLICY, session_regime
        regime = CHILD if is_sub_agent else session_regime(
            agent, orchestrator=orchestrator, session_id=session_id)
        pol = REGIME_POLICY[regime]
        reader = {"demote_automation": bool(pol.demote_automation_recall)}
        sid = session_id or getattr(agent, "session_id", None)
        if pol.recall_own_runs_first and sid:
            from core.autonomy_marker import cron_job_for_session, goal_for_session
            reader["prefer_job_id"] = cron_job_for_session(sid)
            reader["prefer_goal_id"] = goal_for_session(sid)
        return reader
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("recall reader unresolved: %s", e)
        return {}


async def _owner_notes(user_id: Optional[str], seen: Optional[set]) -> str:
    """D5 (review 2026-10-13): the notes the owner saved from the console
    ("I saved that to my notes.") had no path into the agent's context — only
    the default-OFF ``memory`` tool read ``curated_memory``. Return the active
    owner-authored notes (``created_by='user'``), dated, bounded; '' on none or
    error. Owner-authored, so not framed as untrusted data."""
    try:
        from modules.memory.registry import get_memory_registry
        provider = get_memory_registry().active()
        if provider is None or not hasattr(provider, "note_list"):
            return ""
        rows = await provider.note_list(user_id, status="active", limit=50)
    except Exception as e:
        logger.debug("owner notes skipped: %s", e)
        return ""
    import time as _time
    out, used = [], 0
    for n in rows or []:
        if (n.get("created_by") or "") != "user":
            continue
        body = " ".join(str(n.get("content") or "").split())
        if not body:
            continue
        title = str(n.get("title") or "").strip()
        ts = n.get("updated_ts") or n.get("created_ts")
        day = ""
        try:
            day = f"[{_time.strftime('%Y-%m-%d', _time.localtime(int(ts)))}] " if ts else ""
        except Exception:
            day = ""
        line = f"- {day}{title + ': ' if title else ''}{body}"
        key = " ".join(line.split())
        if seen is not None and key in seen:
            continue
        if len(line) > RECALL_ENTRY_MAX_CHARS:
            line = line[:RECALL_ENTRY_MAX_CHARS - 1] + "…"
        if out and used + len(line) + 1 > OWNER_NOTES_MAX_CHARS:
            break
        out.append(line)
        used += len(line) + 1
        if seen is not None:
            seen.add(key)
        if len(out) >= OWNER_NOTES_MAX:
            break
    return "\n".join(out)


_KB_HEAD = re.compile(r"^\[([^\]\n]+) #(\d+)\] ", re.M)


async def _date_kb_snapshots(text: str, *, user_id: Optional[str], collection: str) -> str:
    """A8 (review 2026-10-13): a KB chunk is a COPY of a file as of its ingest;
    prefetch served a 10-02 copy of a ledger rewritten daily as if current.
    Tag each ``[source #i]`` with its snapshot date, and say so when the source
    is a file on disk modified after that snapshot. Fail-open: text unchanged."""
    try:
        from modules.memory.registry import kb_list_sources
        rows = await kb_list_sources(user_id=user_id, collection=collection)
    except Exception:
        return text
    import os
    from datetime import datetime
    when = {}
    for r in rows or []:
        src, created = r.get("source_path"), str(r.get("created_at") or "")
        if not src or not created:
            continue
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        changed = False
        try:
            changed = os.path.isabs(src) and os.path.getmtime(src) > ts + 1
        except OSError:
            changed = False
        when[src] = (created[:10], changed)

    def _tag(m):
        src, idx = m.group(1), m.group(2)
        if src not in when:
            return m.group(0)
        day, changed = when[src]
        note = f"snapshot {day}" + ("; the live file changed since — read the file" if changed else "")
        return f"[{src} #{idx}, {note}] "
    return _KB_HEAD.sub(_tag, text)


async def build_prefetch_message(query: str, *, session_id: str,
                                 user_id: Optional[str] = None,
                                 seen: Optional[set] = None,
                                 reader: Optional[dict] = None,
                                 owner_notes: bool = False) -> Optional[BaseMessage]:
    """Prefetch memory for ``query`` and wrap it as a MEMORY control message.

    Returns None when there is nothing to inject (Null provider, empty recall, or any
    error). Otherwise a ``HumanMessage`` enveloped with ``MessageOrigin.RECALL``.
    ``user_id`` scopes recall to the tenant (P0-0).

    When ``AutonomyConfig.kb_auto_prefetch()`` is True, ALSO queries the active KB
    collection and appends the result under a distinct section header. The KB branch
    is fully independent and fail-open: a KB error never breaks the memory branch.
    When ``kb_auto_prefetch()`` is False the function is byte-identical to today.
    """
    if not query or not query.strip():
        return None
    read_error = None
    try:
        from modules.memory.registry import MemoryReadError, memory_prefetch
        try:
            recalled = await memory_prefetch(query, session_id=session_id, user_id=user_id,
                                             raise_errors=True, **(reader or {}))
        except MemoryReadError as e:
            # D3 / E-A9: an unreadable store is not an empty one — say so.
            read_error = e.error_class
            recalled = ""
    except Exception as e:  # registry/provider failure must never break the step
        logger.debug("memory prefetch skipped: %s", e)
        read_error = type(e).__name__
        recalled = ""

    try:
        from modules.memory.fts_terms import informative_terms
        terms = informative_terms(query)
    except Exception:
        terms = None
    recalled = _budget((recalled or "").strip(), _MEM_ENTRY,
                       RECALL_MEMORY_MAX_CHARS, seen, terms)

    # T4-02: keep the RAW recall for the observability preview — the event must
    # show what was recalled, not the untrusted-wrap envelope around it.
    raw_recalled = recalled

    # Cross-session recall replays prior user/assistant turns verbatim, so an
    # instruction injected into one session could be recalled into another. Frame
    # it as DATA (same delimiter the untrusted tool-result path uses) so indirect
    # prompt-injection via recalled memory is read, not obeyed. Gated by the shared
    # UNTRUSTED_TOOL_RESULT_WRAP flag (default ON).
    if recalled:
        try:
            from agents.task.constants import UNTRUSTED_TOOL_RESULT_WRAP
            if UNTRUSTED_TOOL_RESULT_WRAP:
                from core.security.untrusted_wrap import wrap_untrusted
                recalled = wrap_untrusted("cross_session_memory", recalled)
        except Exception as e:  # framing must never break prefetch
            logger.debug("memory recall wrap skipped: %s", e)

    # --- T13: KB auto-prefetch (additive, fail-open, fully independent) -------
    kb_recalled = ""
    raw_kb = ""
    try:
        from core.config_policy import AutonomyConfig
        if AutonomyConfig.kb_auto_prefetch():
            try:
                from modules.memory.registry import kb_search
                # Auto-prefetch collection is configurable — a KB ingested into a
                # non-"default" collection was previously never auto-recalled because
                # this was hardcoded. Operators point it at their collection via env.
                import os
                _kb_collection = os.getenv("KB_PREFETCH_COLLECTION", "default").strip() or "default"
                kb_result = await kb_search(query, user_id=user_id, collection=_kb_collection,
                                            limit=RECALL_KB_LIMIT)
                kb_result = _budget((kb_result or "").strip(), _KB_ENTRY,
                                    RECALL_KB_MAX_CHARS, seen, terms)
                if kb_result:
                    kb_result = await _date_kb_snapshots(
                        kb_result, user_id=user_id, collection=_kb_collection)
                    kb_recalled = kb_result
                    raw_kb = kb_recalled
                    try:
                        from agents.task.constants import UNTRUSTED_TOOL_RESULT_WRAP
                        if UNTRUSTED_TOOL_RESULT_WRAP:
                            from core.security.untrusted_wrap import wrap_untrusted
                            kb_recalled = wrap_untrusted("knowledge_base", kb_recalled)
                    except Exception as e:  # wrap failure must never break KB branch
                        logger.debug("kb recall wrap skipped: %s", e)
            except Exception as e:  # KB error must never break memory branch
                logger.debug("kb auto-prefetch skipped: %s", e)
    except Exception as e:  # flag-read failure is non-fatal
        logger.debug("kb_auto_prefetch flag check skipped: %s", e)
    # --------------------------------------------------------------------------

    notes = await _owner_notes(user_id, seen) if owner_notes else ""

    # Combine: sections when two or more are present; a single section bare;
    # None if nothing (and no read error to report).
    sections = [(h, b) for h, b in (("Notes the owner saved", notes),
                                    ("Recalled from memory", recalled),
                                    ("Recalled from knowledge base", kb_recalled)) if b]
    if read_error:
        note = RECALL_READ_ERROR_NOTE.format(error_class=read_error)
        combined = "\n\n".join([note] + [f"## {h}\n\n{b}" for h, b in sections])
    elif len(sections) > 1:
        combined = "\n\n".join(f"## {h}\n\n{b}" for h, b in sections)
    elif sections:
        h, b = sections[0]
        combined = f"## {h}\n\n{b}" if h == "Notes the owner saved" else b
    else:
        return None

    # T4-02: recall was invisible on every surface (ephemeral LLM message only) —
    # record a first-class memory_recall event so the owner can reconstruct what
    # was recalled and acted on. Raw (pre-wrap) content; fail-open.
    _event_attrs = None
    try:
        from agents.task.telemetry.memory_events import emit_memory_event, scrubbed_preview
        _scope = ("cross_session+kb" if (raw_recalled and raw_kb)
                  else ("kb" if raw_kb else "cross_session"))
        _raw = "\n".join(p for p in (notes, raw_recalled, raw_kb) if p)
        _extra = {"read_error": read_error} if read_error else {}
        _event_attrs = emit_memory_event(
            "memory_recall", user_id=user_id or "", session_id=session_id,
            source="prefetch", scope=_scope, content=_raw,
            query=scrubbed_preview(query), **_extra)
    except Exception as e:
        logger.debug("memory recall event skipped: %s", e)

    from modules.llm.messages import make_control_message, MessageOrigin

    msg = make_control_message(combined, MessageOrigin.RECALL)
    if _event_attrs:
        try:
            # Ride the attrs on the message so the caller (the prefetch mixin,
            # which holds the orchestrator) can mirror the event to the live feed.
            msg._memory_event = _event_attrs
        except Exception:
            pass
    return msg


def _owner_memory(agent) -> bool:
    """The regime's ``owner_memory`` column (fail-closed to False)."""
    try:
        from agents.task.session_class import regime_policy
        return bool(regime_policy(agent).owner_memory)
    except Exception:
        return False


class MemoryPrefetchMixin:
    """Compose into Agent to inject recalled memory at step start (B-T2)."""

    def _build_recall_query(self) -> str:
        """Build the memory-recall query from the live turn, not just the static task.

        Combines the turn's subject with the current brain-state ``Next`` (the
        agent's stated next intent) so recall tracks what the agent is actually
        doing now (P0-1). Fail-open: falls back to the task string alone.

        B1 (2026-10-11) — the subject, first that is set:
        - ``_recall_turn_text``: the latest genuine owner message (set at the
          drain). A long-lived chat session keeps ``self.task`` = its FIRST
          message, so every later turn recalled for that first message.
        - ``orchestrator._recall_seed``: the run's own text, minted by the cron
          runner / goal dispatcher. A cron task OPENS with the owner-rules
          overlay, so 85% of cron recalls searched the overlay's words.
        - ``self.task``.
        """
        task = (getattr(self, "_recall_turn_text", None)
                or getattr(getattr(self, "orchestrator", None), "_recall_seed", None)
                or getattr(self, "task", "") or "")
        # D7 (review 2026-10-13): a short or low-information turn ("yes, do it",
        # "ok", "👍") gave a one-word or empty query exactly when the owner
        # confirmed an action whose context lives in memory. With fewer than two
        # informative terms, carry the previous informative subject (or the
        # task) along with it.
        try:
            from modules.memory.fts_terms import informative_terms
            if len(informative_terms(task)) >= 2:
                self._last_recall_subject = task
            else:
                prev = (getattr(self, "_last_recall_subject", None)
                        or getattr(getattr(self, "orchestrator", None), "_recall_seed", None)
                        or getattr(self, "task", "") or "")
                if prev and prev != task:
                    task = f"{task} {prev}".strip()
        except Exception:
            pass
        try:
            # D7: at the FIRST step of a run the brain state is the PREVIOUS
            # run's — its next_goal is usually end-of-turn boilerplate ("End the
            # turn - the answer is delivered …", review A3) and polluted the
            # query. Only a brain from THIS run enriches it.
            if getattr(getattr(self, "state", None), "n_steps", 0) <= 1:
                return task
            # Real attribute set in next_action_internal.py:646; field name is
            # next_goal (AgentBrain, views.py:39).  The old names (state.current_brain,
            # _last_brain, .next) were all wrong → brain was always None → permanent
            # no-op.  Also accept dict form for robustness.
            # INVARIANT: the `memory` field must NEVER enter the query.
            brain = getattr(self, "_last_brain_state", None)
            nxt = ""
            if isinstance(brain, dict):
                nxt = str(brain.get("next_goal") or brain.get("next") or "")
            else:
                nxt = str(getattr(brain, "next_goal", "") or "")
            query = f"{task} {nxt}".strip()
            return query or task
        except Exception:
            return task

    async def _maybe_prefetch_memory(self) -> None:
        """Inject a recalled-memory message on a cadence. Fail-open.

        Always fires on the first step. With cadence N>0 it ALSO fires every N steps
        (resolved via ``memory_prefetch_cadence()`` — 0/first-step-only on the server,
        3 by default under POLYROB_LOCAL, explicit env wins). Uses the task as the
        recall query and queues the result as an ephemeral (one-shot) message so it
        rides along on the next LLM call without bloating persistent history.
        """
        try:
            # P2-4: a delegated leaf sub-agent gets no automatic cross-session recall —
            # every other injector (episodic digest / autonomy continuity / continuity
            # bridge) already excludes sub-agents, and sub-agents are deliberately H-MEM-
            # isolated (NullTaskContextManager). Provider recall bypassed that on the read
            # side, polluting a focused subtask's context with tenant-wide memory.
            if getattr(self, "_is_sub_agent", False):
                return
            from core.surfaces.room_policy import is_public_session
            if is_public_session(getattr(self, "orchestrator", None)):
                return  # 044 T4: no tenant recall into a public room
            from agents.task.session_class import correspondent_facing
            if correspondent_facing(getattr(self, "orchestrator", None)):
                return  # H16: output goes to a correspondent — no owner-tenant state
            n_steps = getattr(self.state, "n_steps", 0)
            # SA-06: an autonomous (goal/cron) session defaults to a recurring
            # cadence — recalling once at step 1 (where the brain enrichment is
            # empty) left long missions memory-blind. Fail-open to non-autonomous.
            try:
                from core.autonomy_marker import is_autonomous
                _autonomous = is_autonomous(self.session_id)
            except Exception:
                _autonomous = False
            cadence = memory_prefetch_cadence(autonomous=_autonomous)
            should = (n_steps == 1) or (cadence > 0 and n_steps > 0 and n_steps % cadence == 0)
            if not should:
                return
            # B1: entries this run already got are not injected again; a run
            # (one chat turn, one cron/goal run) starts at step 1 with a fresh set.
            if n_steps == 1 or not isinstance(getattr(self, "_recall_seen", None), set):
                self._recall_seen = set()
            msg = await build_prefetch_message(
                self._build_recall_query(),
                session_id=self.session_id,
                user_id=getattr(self, "user_id", None),
                seen=self._recall_seen,
                reader=recall_reader(self),
                owner_notes=_owner_memory(self),
            )
            if msg is not None:
                self.message_manager.push_ephemeral_message(msg)
                self.logger.debug("Injected prefetched memory block")
                # T4-02: mirror the recall event to the live session feed so the
                # CLI (/verbose) and webview watchers see it as it happens.
                attrs = getattr(msg, "_memory_event", None)
                if attrs:
                    try:
                        await self.orchestrator.add_to_feed(
                            getattr(self, "agent_id", "agent"), "memory_recall", dict(attrs))
                    except Exception as feed_err:
                        self.logger.debug(f"memory recall feed mirror skipped: {feed_err}")
        except Exception as e:
            self.logger.debug(f"memory prefetch injection skipped: {e}")

    async def _maybe_inject_episodic_digest(self) -> None:
        """Inject a recent-activity digest of the agent's OWN recent runs, ONCE, on
        the first step of a chat/owner session (Task 5). Fail-open; inert unless
        ``AutonomyConfig.episodic_digest_inject()`` is on and an external episodic
        provider has rows in-window.

        Scoping (the key correctness property — never leak into an autonomous run):
        this is invoked from the first step of the RUN LOOP, not at Agent
        construction time. ``agents.task.runtime.run_as_session.run_task_as_session``
        calls ``mark_autonomous(session_id)`` AFTER ``create_session()`` returns but
        BEFORE ``run_session()`` starts stepping — so at construction time a
        goal/cron/planner session would not yet be marked autonomous and would be
        misread as a chat session. By the time step 1 runs, the marker is reliably
        set, so ``is_autonomous(self.session_id)`` here is trustworthy.
        """
        try:
            if getattr(self.state, "n_steps", 0) != 1:
                return
            if getattr(self, "_session_bootstrap_done", False):
                return
            if getattr(self, "_is_sub_agent", False):
                return
            from core.surfaces.room_policy import is_public_session
            if is_public_session(getattr(self, "orchestrator", None)):
                return  # 044 T4: no tenant recall into a public room
            from agents.task.session_class import correspondent_facing
            if correspondent_facing(getattr(self, "orchestrator", None)):
                return  # H16: output goes to a correspondent — no owner-tenant state
            from core.autonomy_marker import is_autonomous
            if is_autonomous(self.session_id):
                return
            from agents.task.agent.core.episodic_digest import build_activity_digest
            msg = await build_activity_digest(
                user_id=getattr(self, "user_id", None),
                kind="chat",
                is_sub_agent=self._is_sub_agent,
            )
            if msg is not None:
                self.message_manager.push_ephemeral_message(msg)
                self.logger.debug("Injected episodic activity digest")
        except Exception as e:
            self.logger.debug(f"episodic digest injection skipped: {e}")

    async def _maybe_inject_autonomous_continuity(self) -> None:
        """§7.5: carry recent activity INTO an autonomous goal/cron tick so it stops
        re-deriving 'nothing new' each time. Mirror-image scoping of the chat digest —
        fires ONLY for an autonomous session (never chat), first-step-only, never a
        sub-agent. Gated AUTONOMOUS_CONTINUITY_BRIDGE (default OFF). Fail-open.

        Once per SESSION, like its siblings: a self-wake re-entry resets n_steps
        back to 1, so without the bootstrap-done guard the continuity block would
        be re-injected on every wake (D6)."""
        try:
            if getattr(self.state, "n_steps", 0) != 1:
                return
            if getattr(self, "_session_bootstrap_done", False):
                return
            if getattr(self, "_is_sub_agent", False):
                return
            from core.surfaces.room_policy import is_public_session
            if is_public_session(getattr(self, "orchestrator", None)):
                # 044 I3: no owner-tenant state into a public room. A room's
                # SERVICE run is an autonomous goal run bound to the room's own
                # session, so it passed the `is_autonomous` test below and was
                # handed the owner's last 24 h of autonomous work — trades,
                # goals, spend — to answer strangers with.
                return
            from agents.task.session_class import correspondent_facing
            if correspondent_facing(getattr(self, "orchestrator", None)):
                return  # H16: output goes to a correspondent — no owner-tenant state
            from core.autonomy_marker import is_autonomous
            if not is_autonomous(self.session_id):
                return  # chat sessions use _maybe_inject_episodic_digest instead
            from agents.task.agent.core.episodic_digest import build_mission_continuity
            _cfg = getattr(getattr(self, "container", None), "config", None)
            msg = await build_mission_continuity(user_id=getattr(self, "user_id", None),
                                                 data_dir=getattr(_cfg, "data_dir", None))
            if msg is not None:
                self.message_manager.push_ephemeral_message(msg)
                self.logger.debug("Injected autonomous continuity bridge")
        except Exception as e:
            self.logger.debug(f"autonomous continuity injection skipped: {e}")

    async def _maybe_inject_continuity_bridge(self) -> None:
        """Seed a SESSION_BRIDGE 'last time we were discussing X' message, ONCE, on the
        first step of a chat/owner session (Task 6). Fail-open; inert unless
        ``AutonomyConfig.continuity_bridge_enabled()`` is on and a prior chat episode
        with a summary exists for this session's ``thread_key``.

        Scoped identically to ``_maybe_inject_episodic_digest`` (first-step-only,
        never sub-agent, never autonomous) — see that method's docstring for why the
        autonomy-marker check is only reliable from the first step of the run loop,
        not at Agent construction time.

        The new session's OWN chat episode isn't written until ITS OWN cleanup, so
        recalling "most-recent chat episode for this thread_key" here can only ever
        find the PRIOR session's episode — no self-exclusion needed.

        ``thread_key`` is resolved via a reverse lookup on the durable
        ``session_chat_registry`` (mirrors ``TaskAgent._rebind_recreated_chat`` in
        ``agents/task_agent_lite.py``) rather than reading ``orchestrator._chat_session_key``
        directly, so this also works after an orchestrator recreation.
        """
        try:
            if getattr(self.state, "n_steps", 0) != 1:
                return
            if getattr(self, "_session_bootstrap_done", False):
                return
            if getattr(self, "_is_sub_agent", False):
                return
            from core.surfaces.room_policy import is_public_session
            if is_public_session(getattr(self, "orchestrator", None)):
                return  # 044 T4: no tenant recall into a public room
            from agents.task.session_class import correspondent_facing
            if correspondent_facing(getattr(self, "orchestrator", None)):
                return  # H16: output goes to a correspondent — no owner-tenant state
            from core.autonomy_marker import is_autonomous
            if is_autonomous(self.session_id):
                return
            from core.config_policy import AutonomyConfig
            if not AutonomyConfig.continuity_bridge_enabled():
                return

            thread_key = None
            container = getattr(self, "container", None)
            if container is not None:
                registry = container.get_service("session_chat_registry")
                if registry is not None and hasattr(registry, "resolve_by_session_id"):
                    row = registry.resolve_by_session_id(self.session_id)
                    if row:
                        thread_key = row.get("session_key")
            if not thread_key:
                return

            from core.surfaces.continuity import build_bridge_message
            # E-C4: owner wording + longer bridge + open errand for the owner chat.
            from agents.task.session_class import OWNER_CHAT, session_regime
            msg = await build_bridge_message(
                user_id=getattr(self, "user_id", None), thread_key=thread_key,
                owner=session_regime(self) == OWNER_CHAT)
            if msg is not None:
                self.message_manager.push_ephemeral_message(msg)
                self.logger.debug("Injected continuity bridge message")
        except Exception as e:
            self.logger.debug(f"continuity bridge injection skipped: {e}")
