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

Two bounds are UNCONDITIONAL (chat-first review 2026-08-22, G1/G4). Attribution
only exists when the ledger carried a write descriptor, so a run that produced
files through any other action had no bound at all and listed everything the
scan found — 20 unreadable lines on a phone. The line cap and the roll-up below
do not depend on attribution, and every path is emitted inside backticks so the
renderer marks it as code (Telegram otherwise auto-links a ``*.md`` filename as
a Moldovan domain — G2).
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


def deliverables_max_lines() -> int:
    """Hard cap on individually-listed deliverable lines in one owner message.

    The remainder becomes ONE roll-up line plus the console link. This is the
    bound that holds regardless of ledger attribution.
    """
    from core.env import int_env
    return max(1, int_env("DELIVERABLES_MAX_LINES", 5))


def _code(text: str) -> str:
    """Render a path/filename as a code span.

    ``core.surfaces.rendering`` only emits ``<code>`` for backticked source, and
    a bare ``report.md`` in prose is auto-linked by Telegram as a domain (``.md``
    is Moldova's TLD), giving the owner a tappable link to nowhere.
    """
    return f"`{text}`"


def deliverable_line_for(rel: str, size: str, *, url: Optional[str] = None,
                         fallback: Optional[str] = None) -> str:
    """One deliverable line, preferring a PUBLISHED URL over a server path.

    "attached" and "server-only: <path>" were the honest answers while the agent
    had no way to put a file at an address. Once it does (the U2 ship rail), the
    useful answer is the URL — that is what closes build -> publish -> the owner
    gets a link, instead of the 2026-07-19 bare-filename failure mode.
    """
    if url:
        return f"- {_code(rel)} ({size}) — published: {url}"
    return fallback if fallback is not None else f"- {_code(rel)} ({size})"


def published_url_for(user_id: str, path: str) -> Optional[str]:
    """The published URL recorded for *path*, if the artifact ledger has one."""
    try:
        import os as _os

        from core.artifacts import get_artifact_ledger
        row = get_artifact_ledger()._row_by_path(user_id, _os.path.realpath(path))
        return (row or {}).get("url") or None
    except Exception:
        return None


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
                                           is_injection_reason,
                                           media_entries_from_paths,
                                           screen_attachment_path,
                                           validate_media_paths)
    max_files = attach_max_files()
    max_lines = deliverables_max_lines()
    attachments: List[dict] = []
    lines: List[str] = []
    skipped_unattributed = 0
    overflow = 0
    # Files this run is known to have written come FIRST, so the line cap spends
    # its budget on this run's own output rather than on whatever the scan swept
    # up from a shared workspace.
    ordered = sorted(files, key=lambda f: str(f["path"]) in unattributed)
    for f in ordered:
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
                # I4: `"threat scan" in reason` also matched SCAN_ERROR_REASON,
                # so a scanner that merely RAISED reported an attack.
                if is_injection_reason(reason):
                    from core.security.threat_report import report_threat
                    report_threat("file", source="deliverables", detail=rel,
                                  session_id=session_id, user_id=user_id or "")
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
        # A PUBLISHED file is reachable by anyone — say so, and stop there. It
        # outranks both "attached" and "server-only: <path>", which exist only
        # because the agent used to have no way to give a file an address.
        published = published_url_for(user_id, real) if (real and user_id) else None
        if published:
            lines.append(deliverable_line_for(rel, size, url=published))
        elif reason is None and real:
            attachments.extend(media_entries_from_paths([real]))
            # The absolute path rides IN the line (review Important #3): a
            # quiet-held/capped/fallback re-delivery is text-only, so the text
            # alone must keep the file reachable.
            lines.append(f"- {_code(rel)} ({size}) — attached ({_code(real)})")
        elif len(lines) >= max_lines:
            # The unconditional bound: an unreachable file beyond the cap becomes
            # part of the roll-up. Attached and published files are never rolled
            # up — the owner HAS those, so naming them is the point of the block.
            overflow += 1
        else:
            # A console link is a real address the owner can tap; a server path is
            # only an address to someone with a shell. Prefer the link, fall back
            # to the path so the file stays reachable either way.
            link = _artifact_link(session_id, rel)
            if link:
                lines.append(f"- {_code(rel)} ({size}) — console: {link} ({reason})")
            else:
                where = real or (os.path.join(workspace_dir, rel) if workspace_dir else rel)
                lines.append(f"- {_code(rel)} ({size}) — server-only: {_code(where)} ({reason})")
    rolled = skipped_unattributed + overflow
    if rolled:
        lines.append(_rollup_line(rolled, session_id))
    return attachments, lines


_PUBLISHED_MARK = "— published: "


def reachability_note(lines: Optional[List[str]], *,
                      rail_available: Optional[bool] = None) -> Optional[str]:
    """ONE line when a run's deliverables exist and none is at a public URL.

    Publishing & app-deployment evaluation (2026-09-05): 336 artifacts on prod,
    0 with a URL, and every completion notice read as shipped. The block already
    prefers a URL over a path (:func:`deliverable_line_for`); this names the gap
    when there is none — and names the switch when the ``publish`` tool is not
    registered (``rail_available`` False; None = unknown) — so "done" never
    implies "reachable" again. None when there are no deliverables or one of
    them is published.
    """
    if not lines:
        return None
    if any(_PUBLISHED_MARK in ln for ln in lines):
        return None
    if rail_available is False:
        return ("No file above is at a public URL — the publish rail is not "
                "registered (PUBLISH_ENABLED).")
    return "No file above is at a public URL — the run did not publish."


def publish_rail_available(container) -> Optional[bool]:
    """Whether the ``publish`` tool is registered in this process (None = unknown)."""
    try:
        if container is None or not hasattr(container, "has_service"):
            return None
        return bool(container.has_service("publish"))
    except Exception:
        return None


def _artifact_link(session_id: str, rel: str) -> Optional[str]:
    """Console URL that serves this file, when a console is configured."""
    try:
        from core.surfaces.deep_link import webview_artifact_link
        return webview_artifact_link(session_id, rel)
    except Exception:
        logger.debug("deliverables: artifact link resolution failed", exc_info=True)
        return None


def _rollup_line(count: int, session_id: str) -> str:
    """ONE line for everything the cap held back, pointing at the detail plane.

    Seventeen unreachable paths are noise; one line plus a link the owner can
    tap is the same information in a form a phone can carry.
    """
    try:
        from core.surfaces.deep_link import webview_session_link
        link = webview_session_link(session_id)
    except Exception:
        link = None
    tail = f" — see {link}" if link else " — see the session workspace"
    return f"- (+{count} more file(s){tail})"
