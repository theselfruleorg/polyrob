"""Deliverables block for goal/cron completion pushes (QW-1, proposal 021).

Turns a run's artifact list (``RunOutcome.artifacts`` — ledger descriptors +
workspace file scan) into (a) attach-ready media entries for the owner rail
and (b) honest text lines: every file is either ``attached`` or
``server-only: <path> (<reason>)`` — never a bare filename the owner can't
reach (the 2026-07-19 usability assessment's core failure mode).

Per-run attribution: on a shared project-root workspace the time-window scan
lists OTHER goals' files too (assessment §3.9), so when the ledger carries
``filesystem_write_file`` descriptors, only THEIR files are considered this
run's deliverables; the scan is the fallback when no write descriptor exists.
"""
import logging
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_FILEPATH_RE = re.compile(r'"(?:filepath|file_path|filePath)"\s*:\s*"([^"]+)"')

# Bound on the noise the never-drop rule can add on a shared workspace: at most
# this many UNATTRIBUTED (other-run) files are listed individually.
_MAX_UNATTRIBUTED_LINES = 5


def _fmt_size(n: Optional[int]) -> str:
    if not isinstance(n, (int, float)) or n < 0:
        return "?"
    if n < 1024:
        return f"{int(n)} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _written_names(artifacts: list) -> set:
    """File names claimed by ANY ledger output descriptor (not just
    filesystem_write_file — fs_write/apply_patch/coding actions produce files
    too; review Important #2). The kind is irrelevant: any descriptor whose
    detail names a filepath-shaped key attributes that file to THIS run."""
    names = set()
    for a in artifacts or []:
        if isinstance(a, dict) and a.get("kind") and a.get("detail"):
            for m in _FILEPATH_RE.finditer(str(a.get("detail") or "")):
                names.add(m.group(1))
    return names


def build_deliverables(artifacts: list, session_id: str, user_id: Optional[str],
                       *, attach: bool = True) -> Tuple[List[dict], List[str]]:
    """(attach-ready media entries, deliverables text lines) for a run.

    ``attach=False`` (flag off / caller policy) lists every file as
    server-only and returns no media entries — the block stays honest either
    way. Fail-open: any resolution fault degrades a file to a listed line,
    never raises.
    """
    files = [a for a in artifacts or []
             if isinstance(a, dict) and a.get("path")]
    if not files:
        return [], []
    written = _written_names(artifacts)
    # Never-drop (review Important #2): unattributed files don't attach, but
    # they stay LISTED — the contract is "every artifact accounted for".
    unattributed: set = set()
    if written:
        unattributed = {str(f["path"]) for f in files
                        if str(f["path"]) not in written
                        and Path(str(f["path"])).name not in written}
    workspace_dir: Optional[str] = None
    try:
        from agents.task.path import pm
        workspace_dir = str(pm().get_workspace_dir(session_id, user_id))
    except Exception:
        logger.debug("deliverables: workspace resolution failed", exc_info=True)

    try:  # injected into the core screen (layering ratchet: core never imports modules.*)
        from modules.memory.task.threat_scan import is_suspicious as _scanner
    except ImportError:
        _scanner = None
    from core.surfaces.attachments import (attach_max_files,
                                           media_entries_from_paths,
                                           screen_attachment_path,
                                           validate_media_paths)
    max_files = attach_max_files()
    attachments: List[dict] = []
    lines: List[str] = []
    skipped_unattributed = 0
    for f in files:
        rel = str(f["path"])
        size = _fmt_size(f.get("bytes"))
        real: Optional[str] = None
        reason: Optional[str] = None
        try:
            validated, err = validate_media_paths([rel], workspace_dir)
            if err:
                reason = err
            else:
                real = validated[0]
                reason = screen_attachment_path(real, scanner=_scanner)
        except Exception as e:
            reason = f"validation error: {e}"
        if rel in unattributed:
            reason = reason or "unattributed to this run (shared workspace)"
            if sum("unattributed" in ln for ln in lines) >= _MAX_UNATTRIBUTED_LINES:
                skipped_unattributed += 1
                continue
        elif not attach:
            reason = reason or "attaching disabled"
        elif reason is None and len(attachments) >= max_files:
            reason = "attachment limit reached"
        if reason is None and real:
            attachments.extend(media_entries_from_paths([real]))
            # The absolute path rides IN the line (review Important #3): a
            # quiet-held/capped/fallback re-delivery is text-only, so the text
            # alone must keep the file reachable.
            lines.append(f"- {rel} ({size}) — attached ({real})")
        else:
            where = real or (os.path.join(workspace_dir, rel) if workspace_dir else rel)
            lines.append(f"- {rel} ({size}) — server-only: {where} ({reason})")
    if skipped_unattributed:
        lines.append(f"- (+{skipped_unattributed} more unattributed shared-workspace file(s))")
    return attachments, lines
