"""C9: Auto-load project context files (CLAUDE.md / AGENTS.md / .cursorrules).

CLI-only, default-OFF on server (gated by ``AutonomyConfig.project_context_autoload()``
AND ``local_mode_enabled()`` in construction.py).

``load_project_context(root, *, cap_tokens)`` walks from *root* up to the git root,
selects the highest-precedence recognised filename, runs the injection-threat scan
(fail-CLOSED on unavailable scanner or scan error), bounds the file snapshot to 4 MiB,
caps content to *cap_tokens*, and returns the selected context.
Returns ``None`` if nothing is found or any unrecoverable error occurs (fully
fail-open at the outer level).

F25 (2026-09-22 cache/context review) changed three things about the cap:
  - the cap follows the MODEL WINDOW (``clamp(window * 4 %, 4 000, 40 000)``) instead
    of a fixed 20 000 tokens, falling back to *cap_tokens* when the window is unknown;
  - the cut keeps a HEAD and a TAIL (70 % / 20 %) with a NAMED marker that carries the
    file's path, so the end of the file — where a project file usually puts its
    landmines — is not the part that silently disappears;
  - one-file-wins still holds (a documented precedence decision), but a
    lower-precedence sibling in the same directory is NAMED as skipped.

Safety properties:
  - Skips any file whose path is flagged by ``is_secret_path``.
  - Rejects any document whose content is flagged by the ``is_suspicious`` threat
    scanner (fail-CLOSED if unavailable or if it raises).
  - Truncates content to the effective cap with a named head+tail marker.
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

#: The pre-F25 fixed cap, and the value ``PROJECT_CONTEXT_MAX_TOKENS`` still defaults
#: to. Used as the fallback when the model window is unknown, and as the sentinel
#: construction.py compares against to tell "operator pinned it" from "default".
DEFAULT_PROJECT_CONTEXT_CAP_TOKENS = 20000

#: Share of the model's input window the project file may occupy, and the floor/ceiling
#: the share is clamped into. 4 % of a 200 K window is 8 K; of a 1 M window, 40 K.
_WINDOW_SHARE = 0.04
_CAP_FLOOR_TOKENS = 4000
_CAP_CEILING_TOKENS = 40000

#: Head/tail split of the cap when the file is over budget (a reference agent's ratio). The
#: remaining 10 % is headroom for the marker.
_KEEP_HEAD_RATIO = 0.70
_KEEP_TAIL_RATIO = 0.20


def resolve_cap_tokens(
    context_window: Optional[int],
    *,
    default_cap: int = DEFAULT_PROJECT_CONTEXT_CAP_TOKENS,
) -> int:
    """Effective project-context cap for a model whose input window is *context_window*.

    ``clamp(window * 4 %, 4 000, 40 000)``. Returns *default_cap* unchanged when the
    window is unknown or not a usable positive int — so a caller that cannot resolve a
    window keeps exactly the pre-F25 behaviour.
    """
    if not isinstance(context_window, int) or isinstance(context_window, bool):
        return default_cap
    if context_window <= 0:
        return default_cap
    share = int(context_window * _WINDOW_SHARE)
    return max(_CAP_FLOOR_TOKENS, min(_CAP_CEILING_TOKENS, share))


#: Named-skip line appended when a lower-precedence sibling sits next to the winner.
_SKIPPED_SIBLING_TPL = (
    "<!-- project-context: also present in this directory but NOT loaded "
    "(lower precedence): {names} — read the file directly if you need it -->"
)


def _lower_precedence_siblings(winner: str, directory: Path) -> list[str]:
    """Recognised names below *winner* in precedence that exist in *directory*."""
    try:
        rank = _CONTEXT_FILENAMES.index(winner)
    except ValueError:
        return []
    out: list[str] = []
    for name in _CONTEXT_FILENAMES[rank + 1:]:
        try:
            if (directory / name).is_file():
                out.append(name)
        except OSError:
            continue
    return out


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
    cap_tokens: int = DEFAULT_PROJECT_CONTEXT_CAP_TOKENS,
    context_window: Optional[int] = None,
) -> Optional[str]:
    """End-to-end: decide → resolve the tier root → load → frame.

    Returns the foundation-message body (trusted/steering on local, untrusted-DATA
    wrapped on the server opt-in), or ``None`` when nothing should/could be loaded.
    Pure except for the filesystem read in :func:`load_project_context`; the caller
    resolves ``workspace_dir`` (via the path manager) and passes it in, so this stays
    unit-testable without a live session.

    *context_window* is the model's input window. When it is given, the cap is
    window-relative (:func:`resolve_cap_tokens`) — a 20 000-token file was 10 % of a
    200 K window and 2 % of a 1 M one. When it is ``None``, *cap_tokens* is used
    unchanged, so every existing caller keeps its exact behaviour.
    """
    if not should_load_project_context(
        autoload=autoload, local=local, server_mode=server_mode
    ):
        return None
    root = resolve_project_context_root(local=local, cwd=cwd, workspace_dir=workspace_dir)
    if root is None:
        return None
    effective_cap = resolve_cap_tokens(context_window, default_cap=cap_tokens)
    # P1-8: on the server the walk is CONFINED to the tenant workspace — it must
    # never ascend to a surrounding git root, which on a deployment whose data
    # root lives inside a source checkout is the install's own AGENTS.md/CLAUDE.md.
    ctx = load_project_context(root, cap_tokens=effective_cap, confine_to_root=not local)
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
    from core.security.untrusted_wrap import wrap_untrusted

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
    cap_tokens: int = DEFAULT_PROJECT_CONTEXT_CAP_TOKENS,
    confine_to_root: bool = False,
) -> Optional[str]:
    """Load and return project context from recognised context files.

    Walks from *root* upward to the git root, selects the highest-precedence
    usable name in ``_CONTEXT_FILENAMES``, filters secret/suspicious files, and
    returns its bounded snapshot capped to *cap_tokens*.

    ``confine_to_root=True`` (the server tier, P1-8) caps the search at *root*
    itself — no upward walk — so a tenant workspace nested inside a deployment
    git tree can never read the install's own context files.

    Returns ``None`` when nothing is found or the whole function fails.
    """
    try:
        return _load_project_context_impl(
            Path(root), cap_tokens=cap_tokens, confine_to_root=confine_to_root)
    except Exception as e:
        logger.debug("load_project_context failed (non-fatal): %s", e)
        return None


def _load_project_context_impl(root: Path, *, cap_tokens: int,
                               confine_to_root: bool = False) -> Optional[str]:
    """Implementation (raises on error; caller wraps in try/except)."""
    from core.security.secret_guard import is_secret_path, estimate_tokens_rough

    # A missing scanner cannot authorize a new project instruction source.
    try:
        from modules.memory.task.threat_scan import is_suspicious
    except Exception:
        logger.warning("project context not loaded: threat scanner unavailable")
        return None

    root_resolved = root.resolve()
    # P1-8: confined mode never ascends — the search root IS the given root.
    git_root = None if confine_to_root else _find_git_root(root_resolved)
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
    found: list[tuple[str, str, Path]] = []  # at most one (filename, content, path)

    for filename in _CONTEXT_FILENAMES:
        if found:
            break
        for directory in dirs:  # dirs is most-local → git-root order
            candidate = directory / filename
            if not candidate.is_file():
                continue

            # Secret-path guard — evaluated on the path RELATIVE to the search
            # root: components ABOVE the root (which the walk cannot escape and
            # the tenant cannot control) must not poison the check. A server
            # workspace lives under `data/…/workspace`, and the guard's blanket
            # `data` dir rule flagged the workspace's own AGENTS.md through the
            # absolute path (P1-8 follow-up). Secret paths INSIDE the walk
            # (config/.env.*, .polyrob/…) still match on the relative form.
            try:
                rel_candidate = candidate.relative_to(search_root)
            except ValueError:
                rel_candidate = candidate
            if is_secret_path(rel_candidate, root=search_root):
                logger.debug("project_context: skipping secret path %s", candidate)
                continue

            # Read the file.
            try:
                from core.security.confined_read import read_confined_bytes
                # A prompt-token cap does not bound the allocation while reading.
                # Allow up to 4 UTF-8 bytes per character; refuse oversized files.
                raw = read_confined_bytes(candidate, search_root, 4 * 1024 * 1024).decode(
                    "utf-8", errors="replace")
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

            found.append((filename, raw, candidate))
            break  # most-local occurrence of the winning name — stop walking dirs

    if not found:
        return None

    # Concatenate with per-file headers.
    parts: list[str] = []
    for filename, content, _path in found:
        header = _FILE_HEADER_TPL.format(filename=filename)
        parts.append(f"{header}\n{content}")

    combined = "\n\n".join(parts)
    winner_name, _winner_raw, winner_path = found[0]

    # Cap to cap_tokens. F25: head + tail, NAMED, with the path as the way out — a
    # head-only cut with an HTML comment told the model nothing about what it lost
    # or how to get it, and a project file's landmines are usually near the end.
    total_tokens = estimate_tokens_rough(combined)
    if total_tokens > cap_tokens:
        from agents.task.agent.core.result_budget import named_truncation
        combined = named_truncation(
            combined,
            cap_tokens,
            way_out=f"read {winner_path} for the full file",
            keep_head_ratio=_KEEP_HEAD_RATIO,
            keep_tail_ratio=_KEEP_TAIL_RATIO,
            label="project context ",
        )
        logger.debug(
            "project_context: truncated from ~%d to ~%d tokens", total_tokens, cap_tokens
        )

    # F25: one-file-wins stays (a precedence decision, not an accident), but a
    # lower-precedence sibling next to the winner is NAMED — otherwise the model
    # cannot know the file exists, let alone read it on purpose.
    skipped = _lower_precedence_siblings(winner_name, winner_path.parent)
    if skipped:
        combined += (
            "\n\n" + _SKIPPED_SIBLING_TPL.format(names=", ".join(skipped))
        )

    logger.debug(
        "project_context: loaded %d file(s) [%s], ~%d tokens",
        len(found),
        ", ".join(name for name, _, _ in found),
        estimate_tokens_rough(combined),
    )
    return combined
