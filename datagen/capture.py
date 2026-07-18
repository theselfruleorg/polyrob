"""Opt-in run-end trajectory capture (design spec §A3, ``TRAJECTORY_CAPTURE``).

Rides the ``run_task_to_outcome`` seam: after the RunOutcome is assembled the
session's persisted artifacts are captured as a canonical record labeled from
the outcome, under ``<data_root>/datagen/captured/<user_id>/<session_id>.json``.

Fail-open by contract — capture must NEVER break a run — but the scrub gate
inside stays fail-closed (a scrub failure skips the capture, never exports
unverified content). Correspondent-tainted sessions are skipped.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from agents.task.path import pm

from datagen.assemble import assemble_record
from datagen.formats import render_raw
from datagen.scrub import ScrubError, has_correspondent_content, scrub_record

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    from core.env import bool_env
    return bool_env("TRAJECTORY_CAPTURE", False)


def trajectory_capture_enabled() -> bool:
    """Public gate for callers that want to skip dispatch entirely (e.g. the
    run_as_session call site avoids spawning a to_thread hop when capture is off)."""
    return _enabled()


def outcome_labels(outcome: Any) -> dict:
    """Map a RunOutcome onto record labels (pure)."""
    if getattr(outcome, "refusal", False):
        status = "failed"
    elif getattr(outcome, "done_called", None) is True:
        status = "done"
    elif getattr(outcome, "blocked", False):
        status = "partial"
    else:
        status = "unknown"
    return {
        "outcome": status,
        "verified": str(getattr(outcome, "verified", "unverified")),
        "refusal": bool(getattr(outcome, "refusal", False)),
        "all_actions_errored": bool(getattr(outcome, "all_actions_errored",
                                            False)),
        "steps": int(getattr(outcome, "steps", 0) or 0),
        "spend_usd": float(getattr(outcome, "spend_usd", 0.0) or 0.0),
    }


def maybe_capture(task_agent: Any, outcome: Any, *,
                  user_id: str) -> Optional[Path]:
    """Capture one finished run as a training record. Never raises."""
    try:
        if not _enabled():
            return None
        session_id = getattr(outcome, "session_id", None)
        if not session_id:
            return None
        session_dir = pm().get_session_root(session_id, user_id)
        record = assemble_record(
            Path(session_dir),
            session_meta={"source": "run"},
            labels=outcome_labels(outcome),
            user_id=str(user_id or ""),
        )
        record.session_id = str(session_id)
        if not record.messages:
            return None
        if has_correspondent_content(record):
            logger.debug("trajectory capture: correspondent-tainted session "
                         "%s skipped", session_id)
            return None
        try:
            scrub_record(record)
        except ScrubError:
            logger.warning("trajectory capture: scrub failed for %s — skipped",
                           session_id)
            return None
        out = (Path(pm().data_root) / "datagen" / "captured"
               / (str(user_id or "anon")) / f"{session_id}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(render_raw(record), default=str))
        return out
    except Exception:  # noqa: BLE001 — fail-open by contract
        logger.debug("trajectory capture failed", exc_info=True)
        return None
