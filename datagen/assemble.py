"""Assemble a canonical TrajectoryRecord from a session's on-disk artifacts.

Pure (no container, no pm() import): callers pass the session directory.
Readers tolerate both the current layout (``memory/message_history.json``,
``history/agent_history_*.json``, ``data/llm_usage/*.json``) and the legacy
root-level fallbacks, and never raise on missing/corrupt files.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from datagen.record import DEFAULT_LABELS, TrajectoryRecord

logger = logging.getLogger(__name__)


def read_message_history(session_dir: Path) -> dict:
    """Load ``message_history.json`` (current ``memory/`` location first,
    then the legacy session root). Returns ``{}`` when absent/unreadable."""
    session_dir = Path(session_dir)
    for candidate in (session_dir / "memory" / "message_history.json",
                      session_dir / "message_history.json"):
        if candidate.exists():
            try:
                return json.loads(candidate.read_text())
            except (json.JSONDecodeError, OSError):
                logger.warning("datagen: unreadable message history %s", candidate)
                return {}
    return {}


def read_agent_steps(session_dir: Path) -> list:
    """Concatenate every ``agent_history_*.json`` ``history`` list.

    The live ledger is written via ``pm().get_history_dir()`` =
    ``<session>/data/history/`` (agents/task/agent/core/history_io.py) — that
    candidate comes first; ``history/`` and the session root are legacy
    fallbacks."""
    session_dir = Path(session_dir)
    files: list[Path] = []
    for hist_dir in (session_dir / "data" / "history", session_dir / "history"):
        if hist_dir.is_dir():
            files = sorted(hist_dir.glob("agent_history_*.json"))
            if files:
                break
    if not files:
        files = sorted(session_dir.glob("agent_history_*.json"))
    steps: list = []
    for f in files:
        try:
            payload = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("datagen: unreadable agent history %s", f)
            continue
        entries = payload.get("history") if isinstance(payload, dict) else None
        if isinstance(entries, list):
            steps.extend(entries)
    return steps


def summarize_llm_usage(session_dir) -> Optional[dict]:
    """Aggregate the per-call ``llm_usage_*.json`` records under a session dir.

    Returns ``{records, total_tokens, total_cost_estimate}`` or None. (Canonical
    home of the helper previously inlined in ``cli/commands/session.py``.)
    """
    d = Path(session_dir) / "data" / "llm_usage"
    if not d.exists():
        return None
    records = []
    for f in sorted(d.glob("llm_usage_*.json")):
        try:
            records.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    legacy = d / "usage.json"
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text())
            records.extend(data if isinstance(data, list) else [data])
        except (json.JSONDecodeError, OSError):
            pass
    if not records:
        return None
    tokens = sum((r.get("token_count") or 0) for r in records if isinstance(r, dict))
    cost = sum((r.get("cost_estimate") or 0) for r in records if isinstance(r, dict))
    return {"records": len(records), "total_tokens": tokens,
            "total_cost_estimate": round(cost, 6)}


def find_memory_db(data_root: Path) -> Optional[Path]:
    """Locate the episodes DB (``memory.db``) for a session data root.

    The DB is created in ``BotConfig.data_dir`` (modules/memory/
    backend_factory.py) — which is the PARENT of ``pm().data_root`` on the
    local CLI (``<home>/sessions``) and of the server's default ``DATA_ROOT``
    (``data/task`` → ``data``). Legacy/test layouts keep it at the data root
    itself. First existing candidate wins; None when neither exists."""
    data_root = Path(data_root)
    for cand in (data_root / "memory.db", data_root.parent / "memory.db"):
        if cand.exists():
            return cand
    return None


def load_episode_labels(memory_db: Path, user_id: str,
                        session_id: str) -> Optional[dict]:
    """Read the newest ``episodes`` row for (user_id, session_id) from the
    memory DB, read-only. Returns a labels dict or None; never raises."""
    memory_db = Path(memory_db)
    if not memory_db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{memory_db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT outcome, summary, spend_usd, steps FROM episodes "
                "WHERE user_id = ? AND session_id = ? ORDER BY ts DESC LIMIT 1",
                (user_id, session_id)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    outcome, summary, spend_usd, steps = row
    labels = {"outcome": outcome or "unknown",
              "spend_usd": float(spend_usd or 0.0),
              "steps": int(steps or 0)}
    if summary:
        labels["summary"] = summary
    return labels


def assemble_record(session_dir: Path, *, session_meta: Optional[dict] = None,
                    labels: Optional[dict] = None,
                    user_id: str = "", instance_id: str = "") -> TrajectoryRecord:
    """Compose the canonical record for one session directory."""
    session_dir = Path(session_dir)
    history = read_message_history(session_dir)
    meta = session_meta or {}
    return TrajectoryRecord(
        session_id=str(history.get("session_id") or meta.get("id")
                       or session_dir.name),
        user_id=user_id,
        instance_id=instance_id,
        created_at=history.get("saved_at") or meta.get("created_at"),
        exported_at=datetime.now().isoformat(timespec="seconds"),
        model=meta.get("model"),
        provider=meta.get("provider"),
        task=meta.get("task"),
        messages=list(history.get("messages") or []),
        steps=read_agent_steps(session_dir),
        labels={**DEFAULT_LABELS, **(labels or {})},
        usage=summarize_llm_usage(session_dir) or {},
        provenance={"source": str(meta.get("source") or "export"),
                    "session_dir": str(session_dir)},
    )
