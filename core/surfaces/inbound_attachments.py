"""Shared INBOUND-attachment handling (2026-09-13 chat-media rail).

The symmetric twin of ``core/surfaces/attachments.py`` (which screens a workspace
file on its way OUT): this module takes bytes that arrived on a chat surface,
writes them into the session workspace, and turns a saved file into the text +
vision blocks a turn carries.

Why it lives in core: the pipeline existed only inside ``api/task_http_api.py``
(the console's upload path), so Telegram and email could not reuse it without an
upward ``surfaces -> api`` import. ``api`` now wraps this module, so the console's
behaviour is unchanged and every surface shares ONE answer.

Two hard rules:
  * A write NEVER escapes the session workspace — the caller-supplied filename is
    reduced to a basename and re-confined, so ``../../etc/passwd`` becomes
    ``inbound/passwd``.
  * A file that is refused (too large, unreadable, missing) is NAMED in the turn
    text. Silently dropping the owner's attachment is the bug this rail fixes.
"""
import base64
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from core.env import bool_env, float_env, int_env

logger = logging.getLogger(__name__)

#: Subdirectory of the session workspace every inbound file lands in. One place,
#: so the agent can be told "your attachments are in inbound/" once.
INBOUND_DIR = "inbound"

#: Extensions handed to the model as vision blocks. Kept in step with
#: ``api.task_http_api.inject_file_content_to_message``'s original set.
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

#: Text-ish extensions whose whole body is inlined when small enough.
INLINE_TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".log",
    ".py", ".js", ".ts", ".html", ".css", ".sh", ".sql", ".toml", ".ini",
}

#: Below this a text file is inlined whole; above it, referenced by path.
INLINE_SIZE_THRESHOLD = 30 * 1024

_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}

_EXT_BY_KIND = {
    "image": ".jpg", "photo": ".jpg", "voice": ".ogg", "audio": ".ogg",
    "video": ".mp4", "sticker": ".webp", "document": ".bin",
}

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


# --- flags -----------------------------------------------------------------

def inbound_media_enabled() -> bool:
    """Master switch for the inbound media rail. Default ON — a chat surface that
    silently discards the owner's photo is the defect, not the safe default."""
    return bool_env("INBOUND_MEDIA_ENABLED", True)


def inbound_media_max_mb() -> float:
    """Per-file cap for a file arriving from a chat surface."""
    return float_env("INBOUND_MEDIA_MAX_MB", 20.0)


def inbound_media_max_files() -> int:
    """Max files accepted from ONE inbound message; the rest are named, not dropped."""
    return int_env("INBOUND_MEDIA_MAX_FILES", 5)


def inbound_image_max_mb() -> float:
    """Per-image cap for building a vision block (base64 inflates ~33%)."""
    return float_env("INBOUND_IMAGE_MAX_MB", 10.0)


# --- helpers ---------------------------------------------------------------

def _safe_basename(filename: Optional[str], kind: Optional[str]) -> str:
    """Reduce a caller-supplied name to a safe basename inside ``inbound/``.

    A surface filename is attacker-influenced (an email Content-Disposition, a
    Telegram document name), so it is never trusted as a path: only the basename
    survives, and anything outside ``[A-Za-z0-9._-]`` collapses to ``_``.
    """
    base = os.path.basename((filename or "").strip().replace("\\", "/").rstrip("/"))
    base = _UNSAFE_NAME.sub("_", base).strip("._") or ""
    if not base:
        base = f"{(kind or 'file')}{_EXT_BY_KIND.get(kind or '', '.bin')}"
    return base[:120]


def _unique_path(directory: str, basename: str) -> str:
    """``shot.png`` -> ``shot.png``, then ``shot-1.png``, ``shot-2.png``…

    Two photos in one album share a filename; overwriting the first would lose the
    owner's file with no error anywhere.
    """
    stem, ext = os.path.splitext(basename)
    candidate = basename
    n = 0
    while os.path.exists(os.path.join(directory, candidate)):
        n += 1
        candidate = f"{stem}-{n}{ext}"
    return candidate


def _resolve_in_workspace(workspace_dir: str, rel_path: str) -> Optional[str]:
    """Resolve ``rel_path`` under the workspace, or None when it escapes."""
    try:
        ws_real = os.path.realpath(workspace_dir)
        target = os.path.realpath(os.path.join(ws_real, rel_path))
    except (ValueError, OSError):
        return None
    if target != ws_real and not target.startswith(ws_real + os.sep):
        return None
    return target


def _human_size(num_bytes: int) -> str:
    kb = num_bytes / 1024
    return f"{kb:.1f}KB" if kb < 1024 else f"{kb / 1024:.1f}MB"


# --- persist ---------------------------------------------------------------

def persist_inbound_file(
    workspace_dir: str,
    filename: Optional[str],
    data: Optional[bytes],
    *,
    kind: Optional[str] = None,
    max_mb: Optional[float] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Write inbound bytes into ``<workspace>/inbound/``.

    Returns ``(relative_path, None)`` on success or ``(None, reason)`` — the reason
    is user-facing text, because a refused attachment must be named on the turn.
    """
    if not workspace_dir:
        return None, "no session workspace available to store the attachment"
    if not data:
        return None, "the file arrived empty (0 bytes) and was not stored"

    cap_mb = inbound_media_max_mb() if max_mb is None else max_mb
    if len(data) > cap_mb * 1024 * 1024:
        return None, (f"the file is too large ({_human_size(len(data))}, "
                      f"cap {cap_mb:g}MB) and was not stored")

    directory = os.path.join(workspace_dir, INBOUND_DIR)
    try:
        os.makedirs(directory, exist_ok=True)
        basename = _unique_path(directory, _safe_basename(filename, kind))
        full = os.path.join(directory, basename)
        # Re-confine AFTER joining: _safe_basename already strips traversal, this
        # is the belt-and-braces check that the write lands inside the workspace.
        if _resolve_in_workspace(workspace_dir, os.path.join(INBOUND_DIR, basename)) is None:
            return None, "the attachment path resolved outside the session workspace"
        with open(full, "wb") as fh:
            fh.write(data)
    except OSError as e:
        logger.warning("inbound attachment write failed (%s): %s", filename, e)
        return None, f"could not be stored ({type(e).__name__})"
    return os.path.join(INBOUND_DIR, basename), None


# --- describe on the turn --------------------------------------------------

def inject_file_content(
    workspace_dir: str,
    rel_path: str,
    message_text: str,
    *,
    max_image_mb: Optional[float] = None,
) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
    """Append a workspace file to the turn text, plus vision blocks for an image.

    Returns ``(text, image_attachments | None)``. ``image_attachments`` entries are
    OpenAI-shaped ``{"type": "image_url", ...}`` dicts — the shape
    ``agents/task/agent/messages/guidance.py`` already builds a multimodal
    ``HumanMessage`` from and ``modules/llm/adapters.py`` converts per provider.
    """
    full_path = _resolve_in_workspace(workspace_dir, rel_path)
    if full_path is None:
        return (f"{message_text}\n\n[Error: attachment path is outside the session "
                f"workspace and was refused: {rel_path}]", None)
    if not os.path.exists(full_path):
        return f"{message_text}\n\n[Error: attached file not found: {rel_path}]", None

    size = os.path.getsize(full_path)
    ext = os.path.splitext(full_path)[1].lower()
    name = os.path.basename(full_path)

    if ext in IMAGE_EXTENSIONS:
        cap = inbound_image_max_mb() if max_image_mb is None else max_image_mb
        if size > cap * 1024 * 1024:
            return (f"{message_text}\n\n[Attached image {name} is too large "
                    f"({_human_size(size)}, cap {cap:g}MB) to read. It IS saved at "
                    f"{rel_path} — say so rather than guessing its content.]", None)
        try:
            with open(full_path, "rb") as fh:
                encoded = base64.b64encode(fh.read()).decode("utf-8")
        except OSError as e:
            return (f"{message_text}\n\n[Error: could not read attached image "
                    f"{name}: {type(e).__name__}]", None)
        mime = _MIME_BY_EXT.get(ext, "image/png")
        text = (f"{message_text}\n\n[Attached image: {name} ({_human_size(size)})]\n"
                f"Path in workspace: {rel_path}")
        return text, [{"type": "image_url",
                       "image_url": {"url": f"data:{mime};base64,{encoded}"}}]

    if size < INLINE_SIZE_THRESHOLD and ext in INLINE_TEXT_EXTENSIONS:
        try:
            with open(full_path, "r", encoding="utf-8") as fh:
                content = fh.read()
            return (f"{message_text}\n\n[Attached file: {name} ({_human_size(size)})]\n"
                    f"--- File Content Start ---\n{content}\n--- File Content End ---"), None
        except (UnicodeDecodeError, OSError):
            pass  # fall through to the reference form

    return (f"{message_text}\n\n[Attached file: {name} ({_human_size(size)})]\n"
            f"File type: {ext or 'unknown'}\n"
            f"Path in workspace: {rel_path}\n"
            f'To read it, use: filesystem_read_file(path="{rel_path}")'), None


# --- the rail --------------------------------------------------------------

#: Media kinds the transcription path owns. A voice note is never absorbed as an
#: attachment: ``voice_guard``/``voice_echo`` read ``media[0]`` and the transcript
#: is already the turn's text.
_TRANSCRIBED_KINDS = {"voice"}


async def absorb_inbound_media(
    media: list,
    workspace_dir: str,
    *,
    fetch_bytes,
    base_text: str = "",
    max_files: Optional[int] = None,
) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
    """Download, store and DESCRIBE every attachment on an inbound message.

    ``fetch_bytes`` is an async ``(Media) -> Optional[bytes]`` supplied by the owning
    surface — the only transport-specific part. Each stored file's workspace-relative
    path is stamped back onto its ``Media.path``.

    Returns ``(text, image_attachments | None)``:
      * ``text`` is ``base_text`` plus one honest paragraph per attachment. A file
        that could not be fetched or stored is NAMED with its reason — the whole
        point of this rail is that an owner's attachment never vanishes silently.
      * ``image_attachments`` is the ``{"type": "image_url", …}`` list that rides on
        ``metadata['image_attachments']``, or None.

    Fail-open: any single attachment's failure is reported in the text and the rest
    are still absorbed. The caller's turn always proceeds.
    """
    text = base_text or ""
    if not media or not inbound_media_enabled():
        return text, None

    candidates = [m for m in media
                  if getattr(m, "kind", None) not in _TRANSCRIBED_KINDS]
    if not candidates:
        return text, None

    cap = inbound_media_max_files() if max_files is None else max_files
    overflow = max(0, len(candidates) - cap)
    images: List[Dict[str, Any]] = []

    for item in candidates[:cap]:
        label = getattr(item, "filename", None) or getattr(item, "kind", None) or "file"
        try:
            data = await fetch_bytes(item)
        except Exception as e:  # a transport fault is reported, never raised at the turn
            logger.warning("inbound media fetch failed (%s): %s", label, e)
            data = None
        if not data:
            text = (f"{text}\n\n[Attachment {label} could not be downloaded from the "
                    f"chat surface and is NOT available — say so rather than guessing "
                    f"its content.]")
            continue

        rel, reason = persist_inbound_file(
            workspace_dir, getattr(item, "filename", None), data,
            kind=getattr(item, "kind", None))
        if rel is None:
            text = f"{text}\n\n[Attachment {label} {reason}.]"
            continue

        try:
            item.path = rel
        except Exception:  # a non-Media duck-typed entry: the path still rides in text
            pass
        text, imgs = inject_file_content(workspace_dir, rel, text)
        if imgs:
            images.extend(imgs)

    if overflow:
        text = (f"{text}\n\n[{overflow} more attachment(s) on this message were not "
                f"read — the per-message cap is {cap}. Ask for them one at a time.]")

    return text, (images or None)


# --- console upload allowlist ----------------------------------------------
# The browser upload path is stricter than a chat surface on purpose: a file
# picker + a spoofable Content-Type is a wider door than a Telegram file_id.
# These are the ONE answer the API endpoint, the MIME sniff and the template's
# `accept=` attribute all read, so the three can never disagree (before this they
# were three hand-maintained lists and the template already allowed .doc/.docx
# that the endpoint's MIME allowlist rejected).

UPLOAD_EXTENSIONS = {
    # documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf", ".odt",
    # text / data / code
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log",
    ".py", ".js", ".ts", ".html", ".css", ".sh", ".sql", ".toml", ".ini",
    # images (vision)
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic",
    # audio / video
    ".mp3", ".m4a", ".ogg", ".oga", ".opus", ".wav", ".flac",
    ".mp4", ".mov", ".webm", ".mkv", ".avi",
    # archives
    ".zip", ".tar", ".gz", ".tgz",
}

#: MIME types accepted by the content sniff. ``text/plain`` covers most code and
#: data files, which ``libmagic`` reports generically. Deliberately NOT here:
#: ``image/svg+xml`` (an SVG is script-bearing markup) and anything
#: ``application/x-executable``-shaped.
UPLOAD_MIME_TYPES = {
    "application/pdf", "application/msword", "application/rtf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
    "application/vnd.oasis.opendocument.text",
    "text/plain", "text/markdown", "text/csv", "text/tab-separated-values",
    "text/html", "text/css", "text/xml", "text/x-python", "text/x-shellscript",
    "application/json", "application/xml", "application/javascript",
    "application/x-yaml", "text/yaml",
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
    "image/heic", "image/heif",
    "audio/mpeg", "audio/mp4", "audio/ogg", "audio/opus", "audio/wav",
    "audio/x-wav", "audio/flac", "audio/x-flac",
    "video/mp4", "video/quicktime", "video/webm", "video/x-matroska",
    "video/x-msvideo",
    "application/zip", "application/x-tar", "application/gzip",
    "application/x-gzip", "application/octet-stream",
}


def upload_max_mb() -> float:
    """Per-file cap for a console upload. Shares the chat-surface cap so the two
    doors into a workspace answer the same number."""
    return inbound_media_max_mb()


def upload_accept_attribute() -> str:
    """The `accept=` value for the console's file input, derived from the
    allowlist rather than hand-maintained beside it."""
    return ",".join(sorted(UPLOAD_EXTENSIONS))
