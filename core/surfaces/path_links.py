"""A filesystem path is not an address (communication contract C4).

``core/surfaces/deep_link.py`` has always been able to turn a workspace file into
a console URL, and ``core/surfaces/attachments.py`` has always been able to screen
one for attachment. Both were reachable from exactly two places — the goal-run
completion push (``agents/task/goals/dispatcher.py``) and its deliverables block —
so a message the AGENT wrote could only ever carry ``/var/lib/polyrob/...``, which
the owner cannot open from a phone.

This module is that rule, extracted so every agent→user emit inherits it. For each
workspace-confined path found in an outbound body, in strict precedence:

1. **attach** — the surface carries media and the file passes
   :func:`core.surfaces.attachments.screen_attachment_path` (size cap, secret-shaped
   name, secret-shaped content, optional injection scan);
2. **link** — a console URL via :func:`core.surfaces.deep_link.webview_artifact_link`;
3. **honest** — ``server-only: <name> (<reason>)``.

The bare absolute path never survives. Presenting an unreachable path as if it were
an address is the failure this closes; saying "server-only" is worse UX than a link
but it is *true*, and that ordering is deliberate.

Layering: ``core/`` may not import ``modules.*``, so the threat scanner is injected
by the caller exactly as ``screen_attachment_path(scanner=)`` already requires.
"""
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Absolute POSIX-ish paths whose last segment looks like a FILE — either
#: ``name.ext`` or a dotfile (``.env``). Deliberately narrow: an extension-less
#: path is far more likely to be prose ("/api/session") than a deliverable, and a
#: false positive here rewrites text the agent meant to keep. Dotfiles are in
#: scope precisely because they are where a leak would hurt most.
_PATH_RE = re.compile(
    r"(?<![\w/])(/(?:[\w.\-+@]+/)*"
    r"(?:[\w\-+@][\w.\-+@]*\.[A-Za-z0-9]{1,8}|\.[A-Za-z0-9][\w.\-+@]*))")


@dataclass
class Resolution:
    """The rewritten body plus what happened to each path."""
    text: str
    attachments: list = field(default_factory=list)
    #: ``(absolute_path, "attached" | "linked" | "server-only:<reason>")`` — the
    #: receipt the agent is shown, so it stops guessing whether attaching worked.
    resolved: List[Tuple[str, str]] = field(default_factory=list)


def path_links_enabled() -> bool:
    """Whether workspace paths are resolved to addresses (``CHAT_PATH_LINKS``)."""
    from core.env import bool_env
    return bool_env("CHAT_PATH_LINKS", True)


def _confined(raw: str, ws_real: str) -> Optional[str]:
    """The real path when *raw* resolves inside the workspace, else None.

    Symlink escapes are rejected by resolving first and prefix-checking after —
    the same contract ``attachments.validate_media_paths`` enforces.
    """
    try:
        real = os.path.realpath(raw)
    except (ValueError, OSError):
        return None
    if real != ws_real and not real.startswith(ws_real + os.sep):
        return None
    return real


def resolve_paths(text: str, *, session_id: Optional[str],
                  workspace_dir: Optional[str], media_ok: bool,
                  user_id: Optional[str] = None, scanner=None) -> Resolution:
    """Rewrite workspace paths in *text* into addresses the reader can use.

    Fail-open in every branch: a resolution fault returns the original text, so a
    bug here can cost a link but never a message.
    """
    original = Resolution(text=text)
    if not text or not path_links_enabled() or not workspace_dir:
        return original
    try:
        ws_real = os.path.realpath(workspace_dir)
    except (ValueError, OSError):
        return original

    try:
        from core.surfaces.attachments import attach_max_files, screen_attachment_path
        max_files = attach_max_files()
    except Exception:
        logger.debug("path_links: attachments unavailable (fail-open)", exc_info=True)
        return original

    attachments: list = []
    resolved: List[Tuple[str, str]] = []
    # One decision per distinct path, so the same file named twice in a body is
    # attached once and both mentions render identically.
    decisions: dict = {}

    def _decide(real: str) -> Tuple[str, str]:
        """``(replacement_text, how)`` for one confined real path."""
        rel = os.path.relpath(real, ws_real)
        name = os.path.basename(real)
        reason = None
        if media_ok and len(attachments) < max_files:
            try:
                reason = screen_attachment_path(real, scanner=scanner)
            except Exception:
                reason = "screening errored"
            if reason is None:
                kind = ("image" if Path(real).suffix.lower()
                        in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
                        else "document")
                attachments.append({"kind": kind, "path": real, "caption": None})
                return f"`{name}` (attached)", "attached"
        elif media_ok:
            reason = f"attachment limit of {max_files} reached"
        try:
            from core.surfaces.deep_link import webview_artifact_link
            url = webview_artifact_link(session_id or "", rel)
        except Exception:
            url = None
        if url:
            return url, "linked"
        why = reason or "this surface cannot carry files and no console is configured"
        return f"`{name}` (server-only: {why})", f"server-only:{why}"

    def _sub(match: "re.Match") -> str:
        raw = match.group(1)
        real = _confined(raw, ws_real)
        if real is None:
            return raw  # outside the workspace: prose, config, someone else's file
        if real not in decisions:
            try:
                decisions[real] = _decide(real)
            except Exception:
                logger.debug("path_links: decision failed (fail-open)", exc_info=True)
                return raw
            resolved.append((real, decisions[real][1]))
        return decisions[real][0]

    try:
        rewritten = _PATH_RE.sub(_sub, text)
    except Exception:
        logger.debug("path_links: rewrite failed (fail-open)", exc_info=True)
        return original
    return Resolution(text=rewritten, attachments=attachments, resolved=resolved)


def receipt(resolution: Resolution) -> Optional[str]:
    """One honest line describing what happened to the files, or None.

    Appended to the ACTION RESULT (never to the user's message). Without it the
    agent is attachment-blind: on 2026-07-19 that blindness had it resend a file
    ~12 times and then wrongly conclude ``media_paths`` was unsupported.
    """
    if not resolution.resolved:
        return None
    attached = [os.path.basename(p) for p, how in resolution.resolved if how == "attached"]
    linked = [os.path.basename(p) for p, how in resolution.resolved if how == "linked"]
    server = [(os.path.basename(p), how.split(":", 1)[1])
              for p, how in resolution.resolved if how.startswith("server-only")]
    parts = []
    if attached:
        parts.append(f"attached {len(attached)} file(s): {', '.join(attached)}")
    if linked:
        parts.append(f"linked {len(linked)} file(s): {', '.join(linked)}")
    for name, why in server:
        parts.append(f"{name} is server-only ({why})")
    return "[" + "; ".join(parts) + "]" if parts else None
