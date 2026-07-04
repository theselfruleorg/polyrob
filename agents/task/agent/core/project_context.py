"""C9: Auto-load project context files (CLAUDE.md / AGENTS.md / .cursorrules).

CLI-only, default-OFF on server (gated by ``AutonomyConfig.project_context_autoload()``
AND ``local_mode_enabled()`` in construction.py).

``load_project_context(root, *, cap_tokens)`` walks from *root* up to the git root,
finds the first occurrence of each recognised filename, runs the injection-threat scan
(fail-OPEN on import error, fail-CLOSED on scan error), caps total content to
*cap_tokens* via ``estimate_tokens_rough``, and returns the concatenated result.
Returns ``None`` if nothing is found or any unrecoverable error occurs (fully
fail-open at the outer level).

Safety properties:
  - Skips any file whose path is flagged by ``is_secret_path``.
  - Rejects any document whose content is flagged by the ``is_suspicious`` threat
    scanner (fail-OPEN if the scanner is unavailable; fail-CLOSED if it raises).
  - Truncates concatenated content to *cap_tokens* with an appended notice.
  - All I/O errors are swallowed; the whole function returns ``None`` on exception.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Recognised context filenames, in descending precedence.
# Only ONE name is loaded: the highest-precedence name that exists anywhere on the
# walk wins, and its most-local occurrence is used. Recognised names are NOT
# concatenated (a repo with both AGENTS.md and CLAUDE.md loads only AGENTS.md).
#   polyrob.md  — native control name (target POLYROB without touching other agents' files)
#   POLYROB.md  — uppercase variant
#   AGENTS.md   — vendor-neutral standard (Codex/OpenClaw interop)
#   CLAUDE.md   — Claude Code interop
#   .cursorrules — legacy
_CONTEXT_FILENAMES: tuple[str, ...] = (
    "polyrob.md",
    "POLYROB.md",
    "AGENTS.md",
    "CLAUDE.md",
    ".cursorrules",
)

# Header template injected before each file's content so the model knows the source.
_FILE_HEADER_TPL = "<!-- project-context: {filename} -->"


def should_load_project_context(
    *, autoload: bool, local: bool, server_mode: bool
) -> bool:
    """Decide whether to load project context, given the resolved gates.

    - Local CLI single-owner: load when ``autoload`` is on (trusted framing).
    - Server: load ONLY when ``server_mode`` is explicitly opted in (untrusted
      framing). ``autoload`` defaults OFF on the server, so the default is no-load
      → byte-identical to the pre-Phase-2 behaviour.
    """
    if local:
        return autoload
    return server_mode


def resolve_project_context_root(
    *, local: bool, cwd: str, workspace_dir: Optional[str]
) -> Optional[str]:
    """Pick the directory to search for project-context files, by tier.

    - Local CLI: the process CWD (the user's project root).
    - Server: the tenant's session ``workspace_dir`` ONLY — **never** the process
      CWD, which on a multi-tenant deployment is the install dir (e.g.
      ``/opt/polyrob``). Reading CWD there would leak the deployment's own files
      (or another tenant's) into every session. Returns ``None`` when no server
      workspace is resolvable, so the loader simply loads nothing.
    """
    if local:
        return cwd
    return workspace_dir


def build_project_context_message(
    *,
    local: bool,
    autoload: bool,
    server_mode: bool,
    cwd: str,
    workspace_dir: Optional[str],
    cap_tokens: int = 20000,
) -> Optional[str]:
    """End-to-end: decide → resolve the tier root → load → frame.

    Returns the foundation-message body (trusted/steering on local, untrusted-DATA
    wrapped on the server opt-in), or ``None`` when nothing should/could be loaded.
    Pure except for the filesystem read in :func:`load_project_context`; the caller
    resolves ``workspace_dir`` (via the path manager) and passes it in, so this stays
    unit-testable without a live session.
    """
    if not should_load_project_context(
        autoload=autoload, local=local, server_mode=server_mode
    ):
        return None
    root = resolve_project_context_root(local=local, cwd=cwd, workspace_dir=workspace_dir)
    if root is None:
        return None
    ctx = load_project_context(root, cap_tokens=cap_tokens)
    if ctx is None:
        return None
    return frame_project_context(ctx, trusted=local)


def frame_project_context(content: str, *, trusted: bool) -> str:
    """Frame loaded project context for injection as a foundation message.

    A project file is owner-authored config on the local CLI (trusted → returned
    unchanged, read as steering). On the server the same file may come from a repo
    the operator merely opened, so it is untrusted input: wrap it in
    ``<untrusted_tool_result>`` DATA delimiters so the model treats it as a
    DESCRIPTION of the project, not as instructions it must obey. The secret-skip
    and ``is_suspicious`` scan in :func:`load_project_context` still run first.
    """
    if trusted:
        return content
    from agents.task.agent.core.untrusted_wrap import wrap_untrusted

    return wrap_untrusted("project-context", content)


def _find_git_root(start: Path) -> Optional[Path]:
    """Walk from *start* upward to the first directory containing ``.git``.

    Returns the directory itself, or ``None`` if no ``.git`` marker is found
    before reaching the filesystem root.
    """
    current = start.resolve()
    for _ in range(50):  # hard cap: prevents infinite loops on pathological FSes
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            # Reached the filesystem root without finding .git.
            return None
        current = parent
    return None


def load_project_context(
    root: str | Path,
    *,
    cap_tokens: int = 20000,
) -> Optional[str]:
    """Load and return project context from recognised context files.

    Walks from *root* upward to the git root, collects the first occurrence of
    each name in ``_CONTEXT_FILENAMES``, filters secret/suspicious files, and
    returns their concatenated content capped to *cap_tokens*.

    Returns ``None`` when nothing is found or the whole function fails.
    """
    try:
        return _load_project_context_impl(Path(root), cap_tokens=cap_tokens)
    except Exception as e:
        logger.debug("load_project_context failed (non-fatal): %s", e)
        return None


def _load_project_context_impl(root: Path, *, cap_tokens: int) -> Optional[str]:
    """Implementation (raises on error; caller wraps in try/except)."""
    from agents.task.agent.core.secret_guard import is_secret_path, estimate_tokens_rough

    # Resolve the threat-scanner once; None means scanner unavailable (fail-OPEN).
    try:
        from modules.memory.task.threat_scan import is_suspicious
    except Exception:
        is_suspicious = None  # type: ignore[assignment]

    root_resolved = root.resolve()
    git_root = _find_git_root(root_resolved)
    # If there is no .git root, fall back to the provided root so at least the
    # immediate directory is searched.
    search_root = git_root if git_root is not None else root_resolved

    # Collect candidate directories: from root upward to (and including) git_root.
    dirs: list[Path] = []
    current = root_resolved
    while True:
        dirs.append(current)
        if current == search_root:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    # Name-first precedence: walk filenames in precedence order; for each, find its
    # most-local occurrence across the dirs. The FIRST name that yields a usable
    # file wins, and we stop — recognised names are not concatenated (L1 fix).
    found: list[tuple[str, str]] = []  # at most one (filename, content)

    for filename in _CONTEXT_FILENAMES:
        if found:
            break
        for directory in dirs:  # dirs is most-local → git-root order
            candidate = directory / filename
            if not candidate.is_file():
                continue

            # Secret-path guard.
            if is_secret_path(candidate, root=search_root):
                logger.debug("project_context: skipping secret path %s", candidate)
                continue

            # Read the file.
            try:
                raw = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                logger.debug("project_context: could not read %s: %s", candidate, e)
                continue

            if not raw.strip():
                logger.debug("project_context: %s is empty, skipping", candidate)
                continue

            # Threat-scan (fail-OPEN on import absence; fail-CLOSED on scan error).
            if is_suspicious is not None:
                try:
                    flagged = is_suspicious(raw)
                except Exception as scan_err:
                    logger.warning(
                        "project_context: %s rejected (scan error, fail-closed): %s",
                        candidate, scan_err,
                    )
                    continue
                if flagged:
                    logger.warning(
                        "project_context: %s rejected (suspicious content)", candidate
                    )
                    continue

            found.append((filename, raw))
            break  # most-local occurrence of the winning name — stop walking dirs

    if not found:
        return None

    # Concatenate with per-file headers.
    parts: list[str] = []
    for filename, content in found:
        header = _FILE_HEADER_TPL.format(filename=filename)
        parts.append(f"{header}\n{content}")

    combined = "\n\n".join(parts)

    # Cap to cap_tokens.
    total_tokens = estimate_tokens_rough(combined)
    if total_tokens > cap_tokens:
        # Truncate the combined text to approximately cap_tokens.
        # chars = tokens * 4 (estimate_tokens_rough uses len // 4).
        max_chars = cap_tokens * 4
        truncated = combined[:max_chars]
        notice = f"\n\n<!-- project-context: truncated to {cap_tokens} tokens -->"
        combined = truncated + notice
        logger.debug(
            "project_context: truncated from ~%d to ~%d tokens", total_tokens, cap_tokens
        )

    logger.debug(
        "project_context: loaded %d file(s) [%s], ~%d tokens",
        len(found),
        ", ".join(name for name, _ in found),
        estimate_tokens_rough(combined),
    )
    return combined
