"""Shared outbound-attachment preparation (proposal 021 / QW-1, 2026-07-19).

ONE place decides whether a workspace file may ride an owner-bound message as
media: workspace confinement (relocated from ``tools/controller/message_send``
so the core delivery rail can reuse it without a core->tools import), per-file
size cap, secret-path filter and threat scan. Consumers: the ``message`` tool
(rejects the whole send on a bad path) and the goal-completion deliverables
producer (fail-closed to "listed, not attached").
"""
import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

from core.env import float_env, int_env

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# How much of a text file the threat scanner reads (bounded — attachments can
# be large; injected instructions target the readable head anyway).
_SCAN_HEAD_BYTES = 65536

#: The scanner SAW something — a real detection. This is the only reason that
#: may raise an `injection_flagged` event.
INJECTION_REASON = "content failed threat scan (injection-shaped text)"
#: The scanner itself fell over. The file is still refused (fail-closed), but
#: ⚠️ THIS IS NOT AN ATTACK. Both strings used to contain the substring
#: "threat scan", and every caller branched on `"threat scan" in reason` — so an
#: ImportError inside the scanner told the owner the agent was under attack, on
#: the one signal in the whole rollup that raises a health item.
SCAN_ERROR_REASON = "threat scan errored (refused fail-closed)"


def is_injection_reason(reason: Optional[str]) -> bool:
    """True only for a genuine detection. Callers MUST use this rather than a
    substring match — see :data:`SCAN_ERROR_REASON` for why prose matching is
    how a crashed scanner came to look like a detected injection."""
    return reason == INJECTION_REASON


def is_scan_error_reason(reason: Optional[str]) -> bool:
    """True when the scan could not run. Worth knowing — it means the screen is
    blind — but it is a FAULT, not a threat, and must never be reported as one."""
    return reason == SCAN_ERROR_REASON


def attach_max_mb() -> float:
    """Per-file attach cap in MB (Telegram bot API hard limit is 50)."""
    return float_env("DELIVERABLES_ATTACH_MAX_MB", 10.0)


def attach_max_files() -> int:
    """Max files attached to one completion message; the rest are listed."""
    return int_env("DELIVERABLES_ATTACH_MAX_FILES", 3)


def message_media_max_mb() -> float:
    """Per-file cap for the EXPLICIT `message` tool send — deliberately larger
    than the completion auto-attach default (an owner-directed send of a video
    or archive is legitimate; Telegram's bot-API hard limit is 50 MB)."""
    return float_env("MESSAGE_MEDIA_MAX_MB", 45.0)


def validate_media_paths(paths: List[str], workspace_dir: Optional[str]) -> Tuple[Optional[List[str]], Optional[str]]:
    """Every media path must resolve INSIDE the session workspace: reject '..'
    components, absolute paths outside the workspace, and symlink escapes (checked via
    a resolved-realpath prefix check). Returns (validated_realpaths, None) on success,
    or (None, error_message) — the whole call is rejected on ANY bad path.

    Path normalization (os.path.realpath) can raise on malformed input (e.g. an
    embedded null byte -> ValueError). Any such failure is caught and turned into
    the same graceful (None, error) rejection every other bad path gets."""
    if not workspace_dir:
        return None, "no session workspace available to validate media paths"
    try:
        ws_real = os.path.realpath(workspace_dir)
    except (ValueError, OSError) as e:
        return None, f"invalid session workspace: {e}"
    validated: List[str] = []
    for raw in paths:
        if not raw or not isinstance(raw, str):
            return None, f"invalid media path: {raw!r}"
        if any(part == ".." for part in Path(raw).parts):
            return None, f"media path escapes the workspace (contains '..'): {raw}"
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = Path(workspace_dir) / candidate
        try:
            real = os.path.realpath(str(candidate))
        except (ValueError, OSError) as e:
            return None, f"invalid media path: {raw!r} ({e})"
        if real != ws_real and not real.startswith(ws_real + os.sep):
            return None, f"media path outside session workspace: {raw}"
        validated.append(real)
    return validated, None


def media_entries_from_paths(paths: List[str]) -> list:
    entries = []
    for p in paths:
        kind = "image" if Path(p).suffix.lower() in _IMAGE_EXTS else "document"
        entries.append({"kind": kind, "path": p, "caption": None})
    return entries


def screen_attachment_path(real_path: str, *, max_mb: Optional[float] = None,
                           scanner=None) -> Optional[str]:
    """Attach-eligibility screen for an already-confinement-validated real path.

    Returns None when the file may be attached, else a short human-readable
    rejection reason. Checks: regular file, size cap, secret-shaped filename
    (``secret_guard.is_credential_file`` — a workspace can legitimately contain
    a .env the owner must still never receive over chat), content-level
    secret shapes (``core.secret_scrub`` — an innocuously-named report with an
    inlined API key must not ship, text OR binary head), and an optional
    prompt-injection ``scanner`` on null-free text content.

    ``scanner`` is INJECTED by the caller (layering ratchet: core never imports
    ``modules.*``) — pass ``modules.memory.task.threat_scan.is_suspicious`` from
    agents/tools tiers. None => no injection scan. A RAISING scanner rejects
    (fail-closed, mirroring the project-context precedent).
    """
    cap_mb = attach_max_mb() if max_mb is None else max_mb
    try:
        st = os.stat(real_path)
    except OSError:
        return "file not found"
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if st.st_size > cap_mb * 1024 * 1024:
        return (f"size {st.st_size / (1024 * 1024):.1f} MB exceeds the "
                f"{cap_mb:g} MB attach cap")
    try:
        from core.security.secret_guard import is_credential_file
        if is_credential_file(Path(real_path)):
            return "secret-shaped filename refused"
    except ImportError:
        pass
    try:
        with open(real_path, "rb") as f:
            head = f.read(_SCAN_HEAD_BYTES)
    except OSError:
        return "file unreadable"
    if head:
        text = head.decode("utf-8", "replace")
        try:
            from core.secret_scrub import scrub_secret_shapes
            if scrub_secret_shapes(text) != text:
                return "content contains secret-shaped material"
        except ImportError:
            pass
        if scanner is not None and b"\x00" not in head:
            try:
                if scanner(text):
                    return INJECTION_REASON
            except Exception as e:
                # fail-CLOSED: a crashing scanner must not wave content through.
                # Its own honest shape — a WARNING naming the fault. It is NOT
                # an attack and must never reach the injection lane.
                logger.warning(
                    "attachment threat scan raised (%s: %s) — refusing %s "
                    "fail-closed; the screen is BLIND, not under attack",
                    type(e).__name__, e, os.path.basename(real_path))
                return SCAN_ERROR_REASON
    return None
