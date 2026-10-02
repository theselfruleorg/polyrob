"""Auto-ingest a produced document into the knowledge base (WS-K3, 2026-09-22).

The 2026-09-22 knowledge review measured prod: **76 indexed sources against 473
registered markdown artifacts.** The `/kb` pane on all four seats was working
correctly the whole time — it was rendering a store that held 16% of what the
agent had written, because ingestion was manual (`kb_ingest`, `@folder`) and
nothing ever ingested the agent's own output. Six of the 76 were probe files
left over from a validation run.

The registry already knows every document: `core/artifacts.py` writes one row
per produced file at write time. This module is the thin bridge between that
seam and the ingest path that already exists — **no second chunker, no second
walker, no second store.** `kb_ingest` keeps doing the work, so the secret skip,
the binary skip, the byte cap and the unchanged-hash dedup all apply unchanged.

Three gates, in order:

1. `KB_AUTO_INGEST_ARTIFACTS` (default **ON**) — the revert.
2. `KB_ENABLED` — an instance that never asked for a knowledge base must not
   grow one. With KB off this module is a no-op even though the sqlite provider
   carries the KB mixin either way.
3. :func:`should_ingest` — what a document IS. A run log is a RECORD, not
   knowledge: indexing one makes every recall noisier and is exactly the mix
   that put a live position table and an append-only log in one 1 MB file on
   2026-09-21. Until 060 WS-3 lands the declared marker, this reads the marker
   if present and falls back to the name.

⚠️ Fail-open and fire-and-forget: a bookkeeping ingest must never break, delay
or fail the write the agent already completed.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Set

from core.env import bool_env

logger = logging.getLogger(__name__)

#: The collection the auto-ingest writes into. `default` on purpose: it is what
#: `/kb` lists first and what `KB_PREFETCH_COLLECTION` searches, so an
#: auto-ingested document is recallable rather than parked in a side collection
#: nothing reads.
AUTO_COLLECTION = "default"

#: Text documents only. The ingest path skips binaries anyway; naming the set
#: keeps a 200 MB video out of the ingest call entirely. Imported from the
#: artifact vocabulary so the status section's "N of M indexed" ratio counts the
#: SAME denominator this predicate accepts (downward import, core <- tools).
from core.artifacts import TEXT_DOCUMENT_SUFFIXES as TEXT_SUFFIXES  # noqa: E402

#: Name fragments that mean "this file is an append-only RECORD".
_RECORD_NAME_HINTS = ("run-log", "runlog", "run_log", ".log", "-log-",
                      "changelog", "journal", "transcript")

#: A COPY is never knowledge. Prod carried 34 hand-made `.bak`/copy files under
#: the project tree — one of them a 1.0 MB backup of a 97 KB ledger — sitting in
#: the same directory the readers walk. Indexing a backup means recall answers
#: with a stale version of a document it also holds live.
_COPY_MARKERS = (".bak", ".old", ".orig", " copy.", "-copy.", ".archived",
                 ".pending")

#: How much of the head is read to look for a declared kind marker.
_MARKER_HEAD_BYTES = 400

#: Retained fire-and-forget tasks (the `spawn_retained` contract: without a hard
#: reference the loop may garbage-collect a pending task mid-write).
_PENDING: Set[Any] = set()


def kb_auto_ingest_enabled() -> bool:
    """Both gates: the revert flag AND an instance that asked for a KB."""
    if not bool_env("KB_AUTO_INGEST_ARTIFACTS", True):
        return False
    try:
        from core.config_policy.autonomy_config import AutonomyConfig
        return bool(AutonomyConfig.kb_enabled())
    except Exception as exc:
        logger.debug("kb auto-ingest gate unreadable (%s) — treating as off", exc)
        return False


def declared_kind(path: str) -> Optional[str]:
    """The document's own `kind:` front-matter marker, when it carries one.

    Forward-compatible with 060 WS-3, which makes the marker a first-class
    declaration. Absent marker returns None — this function never guesses.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(_MARKER_HEAD_BYTES).decode("utf-8", "replace")
    except OSError as exc:
        logger.debug("kind marker unreadable for %s: %s", path, exc)
        return None
    if not head.lstrip().startswith("---"):
        return None
    for line in head.splitlines()[1:]:
        if line.strip() in ("---", "..."):
            break
        if line.lower().startswith("kind:"):
            return line.split(":", 1)[1].strip().strip("\"'").lower() or None
    return None


def should_ingest(path: str) -> bool:
    """Is this produced file KNOWLEDGE (index it) or a RECORD (leave it)?"""
    name = os.path.basename(path).lower()
    if os.path.splitext(name)[1] not in TEXT_SUFFIXES:
        return False
    if any(hint in name for hint in _RECORD_NAME_HINTS):
        return False
    lowered = path.replace("\\", "/").lower()
    if any(marker in lowered for marker in _COPY_MARKERS):
        return False
    kind = declared_kind(path)
    if kind and kind != "instruction" and kind != "knowledge":
        return False
    return True


async def ingest_artifact(user_id: str, path: str, *, session_id: str = "",
                          force: bool = False) -> Optional[Dict[str, Any]]:
    """Ingest ONE produced document, reusing the existing ingest path.

    Returns the ingest counts, or None when a gate refused. Never raises.

    ``force`` skips the two ENABLEMENT gates for an explicit owner verb
    (``polyrob kb reindex``) — an operator who typed the command has asked for
    it. It never skips :func:`should_ingest`: a run log is a record whoever
    asked, and the secret/binary/size guards live further down in ``kb_ingest``
    and are not reachable from here at all.
    """
    if not user_id or not path:
        return None
    if not force and not kb_auto_ingest_enabled():
        return None
    if not should_ingest(path):
        return None
    if _written_under_correspondent_taint(user_id, session_id):
        logger.info("kb auto-ingest skipped %s: its session holds a correspondent's "
                    "message (M03)", path)
        return None
    try:
        from tools.knowledge_ingest import kb_ingest
        result = await kb_ingest(path, collection=AUTO_COLLECTION,
                                 recursive=False, user_id=str(user_id),
                                 session_id=str(session_id or ""))
    except Exception as exc:
        logger.debug("kb auto-ingest failed for %s: %s", path, exc, exc_info=True)
        return None
    if result.get("error"):
        # Confinement and "no such path" answer here; both are ordinary (an
        # artifact outside the session workspace is not ingestible), so this is
        # a debug line and not a failure the write path ever sees.
        logger.debug("kb auto-ingest declined %s: %s", path, result.get("error"))
    return result


def _written_under_correspondent_taint(user_id: str, session_id: str) -> bool:
    """M03 (2026-09-23): a document written while a third party's text drove the
    session is never auto-indexed — the KB feeds every FUTURE session's recall,
    so indexing it would persist the injection. Owner/autonomous output still
    indexes; every KB read path frames results as untrusted DATA. Fail-CLOSED."""
    if not session_id:
        return False
    try:
        from agents.task.session.hitl_ingress import session_taint_recorded
        return session_taint_recorded(session_id, user_id)
    except Exception:
        return True


def schedule_artifact_ingest(user_id: Optional[str], path: str, *,
                             session_id: str = "") -> None:
    """Fire-and-forget entry from the SYNC artifact-record seam.

    On the agent loop this spawns a retained task (the write returns
    immediately). Off a loop — a CLI verb, a background thread — it runs on the
    shared background loop via the one `core.async_bridge` seam rather than
    building a second event loop. Never raises.
    """
    if not user_id or not kb_auto_ingest_enabled() or not should_ingest(path):
        return
    try:
        import asyncio
        coro = ingest_artifact(str(user_id), path, session_id=session_id)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            from core.async_bridge import run_coroutine_sync
            run_coroutine_sync(coro, timeout=30.0)
            return
        from core.async_bridge import spawn_retained
        spawn_retained(coro, _PENDING)
    except Exception as exc:
        logger.debug("kb auto-ingest not scheduled for %s: %s", path, exc)


def install_artifact_hook() -> None:
    """Listen for produced documents on the ONE registry seam.

    ⚠️ Registered from the TOOLS tier, never called from core: the layering
    runs downward (`core <- modules <- agents <- tools`) and
    `tests/test_layering_ratchet.py` refuses a new upward edge. `core/
    artifacts.py` owns the hook list; this is the listener.
    """
    try:
        from core.artifacts import register_artifact_hook
        register_artifact_hook(_on_artifact)
    except Exception as exc:
        logger.debug("kb artifact hook not installed: %s", exc)


def _on_artifact(user_id: str, path: str, *, session_id: str = "") -> None:
    schedule_artifact_ingest(user_id, path, session_id=session_id)


__all__ = ["AUTO_COLLECTION", "declared_kind", "ingest_artifact",
           "install_artifact_hook", "kb_auto_ingest_enabled",
           "schedule_artifact_ingest", "should_ingest"]
