"""KB ingestion engine + `knowledge` agent tool (Task 6).

Adds file→KB ingestion and a registerable ``knowledge`` tool on top of the
Task-5 registry routers.  All KB I/O goes through ``modules.memory.registry``
(no direct provider access here).

``from __future__ import annotations`` is SAFE in this file — the param models are
explicit (passed to ``BaseTool.action``), so the Registry never needs to introspect
stringized first-arg annotations here.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from tools.base_tool import BaseTool
from tools.controller.types import ActionResult
from core.security.secret_guard import (
    is_secret_path,
    is_binary_file,
    estimate_tokens_rough,
)
from core.path_safety import is_within_root
from core.security.confined_read import read_confined_bytes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env-driven knobs (read inline, not in constants.py)
# ---------------------------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    from core.env import int_env
    return int_env(name, default)


def _kb_max_files() -> int:
    return _int_env("KB_MAX_FILES", 2000)


def _kb_max_bytes() -> int:
    return _int_env("KB_MAX_BYTES", 25 * 1024 * 1024)


def _kb_chunk_tokens() -> int:
    return _int_env("KB_CHUNK_TOKENS", 800)


def _kb_chunk_overlap() -> int:
    return _int_env("KB_CHUNK_OVERLAP", 100)


# ---------------------------------------------------------------------------
# Office / unsupported extension skip list
# ---------------------------------------------------------------------------

_OFFICE_EXTENSIONS: frozenset[str] = frozenset({
    ".doc", ".odt",
    ".xlsx", ".xls", ".ods",
    ".pptx", ".ppt",
    ".epub", ".rtf",
})

# Extensions handled by dedicated extractors (not in _OFFICE_EXTENSIONS skip list)
_DOCX_EXTENSION = ".docx"

_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".txt", ".md", ".rst", ".csv",
    ".py", ".js", ".ts", ".tsx", ".jsx",
    ".sh", ".bash", ".zsh",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".html", ".xml", ".sql",
    ".go", ".rs", ".c", ".cpp", ".h", ".hpp",
    ".java", ".kt", ".swift", ".rb", ".php",
    ".r", ".scala", ".clj",
})


# ---------------------------------------------------------------------------
# Pure engine helpers
# ---------------------------------------------------------------------------


# 2026-09-19: `data` is a blanket SECRET_DIR_PARTS entry, so `is_secret_path` calls
# EVERY file under a `data/` dir a credential — including the agent's own markdown
# plans in the project workspace (prod goal a049a57d1602: data/x-targets/*.md refused,
# skipped_secret=1 each, while a grep proved they held no secret). The broad
# classifier stays as it is for every other consumer (self_env, patch_source);
# the KB path alone admits a plain-text DOCUMENT under data/ — outside the data
# subtrees that hold runtime state — only when a content secret-scan passes.
_KB_TEXT_DOC_SUFFIXES = frozenset({".md", ".txt", ".rst"})
_KB_PROTECTED_DATA_SUBDIRS = frozenset({
    "auto", "database", "sessions", "identity", "streams", "characters", "prompts",
    "locks", "snapshots", "backups", "credentials",
})
_KB_SCAN_BYTES = 512 * 1024


def kb_ingest_secret_skip(path: Path, root: Path) -> bool:
    """True when the KB ingest must skip *path* as a secret (the walk + single-file
    rule). Delegates to ``is_secret_path`` and then re-admits ONE shape: a
    ``.md/.txt/.rst`` whose only offence is a parent dir named ``data`` (not a
    protected data subtree, no other secret rule hit) AND whose content shows no
    secret shape under the SSOT battery. Fail-closed on any read error."""
    if not is_secret_path(path, root=root):
        return False
    if path.suffix.lower() not in _KB_TEXT_DOC_SUFFIXES:
        return True
    parts = [x.lower() for x in path.parts]
    if "data" not in parts:
        return True  # some other rule fired — never re-admit
    if any(x in _KB_PROTECTED_DATA_SUBDIRS for x in parts):
        return True
    # Would it still be secret without the `data` dir rule? Re-check every other rule.
    from core.security.secret_guard import SECRET_DIR_PARTS, _dir_part_match
    if _dir_part_match(path, SECRET_DIR_PARTS - {"data"}):
        return True
    stripped = Path(*[x for x in path.parts if x.lower() != "data"])
    if is_secret_path(stripped, root=root):
        return True  # a name/path glob or the *.db rule fires on its own
    try:
        from core.secret_patterns import apply_ssot_shapes
        sample = read_confined_bytes(path, root, _KB_SCAN_BYTES).decode("utf-8", "replace")
        if apply_ssot_shapes(sample) != sample:
            return True
    except Exception:
        logger.debug("kb secret content scan failed for %s — skipping", path, exc_info=True)
        return True
    return False


def _iter_files(
    root: Path,
    *,
    recursive: bool = True,
    globs: Optional[List[str]] = None,
    max_files: int = 2000,
    max_bytes: int = 25 * 1024 * 1024,
) -> Tuple[List[Path], Dict[str, int]]:
    """Bounded, safe walk of *root*.

    Returns ``(files, skipped)`` where ``skipped`` maps reason→count.
    Prefers ``rg --files`` for gitignore-aware traversal; falls back to
    bounded ``scandir`` traversal skipping hidden dirs and ``__pycache__``.

    Hard-skips:
    - ``is_secret_path`` (credentials / env files)
    - ``is_binary_file`` (compiled binaries, images, etc.)
    - Files beyond ``max_files`` / ``max_bytes`` cumulative size limits.
    """
    root = root.resolve()
    skipped: Dict[str, int] = {
        "secret": 0,
        "binary": 0,
        "unsafe": 0,
        "max_files": 0,
        "max_bytes": 0,
    }

    from tools.knowledge_inventory import walk_files

    inventory = _rg_files(root) if recursive else None
    if inventory is None:
        inventory = walk_files(root, recursive=recursive)
    candidates, capped = inventory
    skipped["inventory_limit"] = int(capped)

    # Apply glob filters if requested ----------------------------------------
    if globs:
        import fnmatch as _fnmatch
        filtered = []
        for p in candidates:
            rel = str(p.relative_to(root)) if p.is_absolute() else str(p)
            if any(_fnmatch.fnmatch(rel, g) or _fnmatch.fnmatch(p.name, g) for g in globs):
                filtered.append(p)
        candidates = filtered

    # Filter: secret / binary; enforce limits --------------------------------
    collected: List[Path] = []
    total_bytes = 0

    for path in candidates:
        if path.is_symlink():
            skipped['unsafe'] += 1
            continue
        if not path.is_file():
            continue

        # Secret hard-skip (a plain-text doc under `data/` is re-checked by content)
        if kb_ingest_secret_skip(path, root):
            skipped["secret"] += 1
            continue

        # File count cap
        if len(collected) >= max_files:
            skipped["max_files"] += 1
            continue

        # Byte cap (skip oversized files; cumulative cap too)
        try:
            fsize = path.stat().st_size
        except OSError:
            continue
        if total_bytes + fsize > max_bytes:
            skipped["max_bytes"] += 1
            continue

        try:
            snapshot = read_confined_bytes(path, root, max_bytes - total_bytes)
        except OSError:
            skipped['unsafe'] += 1
            continue
        # Binary candidates consume the read budget too; otherwise a directory
        # of rejected files could cause unbounded aggregate I/O.
        total_bytes += len(snapshot)
        if is_binary_file(path, sample=snapshot):
            skipped['binary'] += 1
            continue
        collected.append(path)

    return collected, skipped


def _rg_files(root: Path):
    from tools.knowledge_inventory import rg_files
    return rg_files(root)


async def _extract_text(path: Path, content: Optional[bytes] = None) -> Tuple[Optional[str], Optional[str]]:
    """Extract text from *path*.

    Returns ``(text, skip_reason)`` — exactly one of the two is ``None``.

    - PDF/DOCX → bounded parser subprocess; no in-process fallback
    - text / code extensions → decode the bounded byte snapshot
    - office formats → ``(None, "office-skip")`` — no parser yet (Task 19)
    """
    if content is None:
        content = read_confined_bytes(path, path.parent, _kb_max_bytes())
    suffix = path.suffix.lower()

    # .docx — extract via python-docx (fail-open: if lib absent → skip-with-note)
    if suffix == _DOCX_EXTENSION:
        from tools.document_parser import parse_document_async
        try:
            text = (await parse_document_async('docx', content))['content']
        except Exception:
            text = None
        if text is None:
            return None, f"office-skip:{suffix}"
        return text, None

    # Other office formats — no parser available
    if suffix in _OFFICE_EXTENSIONS:
        return None, f"office-skip:{suffix}"

    # PDF extraction (async — directly await the mixin, no sync-from-async bridge)
    if suffix == ".pdf":
        try:
            text = await _extract_pdf_text(content)
            if text:
                return text, None
            return None, "pdf-empty"
        except Exception as e:
            logger.debug("PDF extraction failed for %s: %s", path, e)
            return None, f"pdf-error:{type(e).__name__}"

    # Plain text / code
    try:
        text = content.decode("utf-8", errors="replace")
        return text, None
    except OSError as e:
        return None, f"read-error:{type(e).__name__}"


async def _extract_pdf_text(content_bytes: bytes) -> str:
    from tools.document_parser import parse_document_async
    return (await parse_document_async('pdf', content_bytes))['content']


def _extract_docx(path: Path, content: Optional[bytes] = None) -> Optional[str]:
    """Compatibility sync adapter, using the same bounded document worker."""
    from tools.document_parser import parse_document
    try:
        if content is None:
            content = read_confined_bytes(path, path.parent, _kb_max_bytes())
        return parse_document('docx', content)['content']
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Structure-first chunker
# ---------------------------------------------------------------------------

# Markdown heading pattern
_MD_HEADING_RE = re.compile(r"^#{1,6} .+", re.MULTILINE)
# Top-level Python def/class
_PYDEF_RE = re.compile(r"^(?:def |class )\S", re.MULTILINE)


def _chunk(
    text: str,
    *,
    target: Optional[int] = None,
    overlap: Optional[int] = None,
) -> List[str]:
    """Structure-first text chunker.

    Splits on: markdown headings → blank-line paragraphs → top-level
    ``def``/``class`` → raw size.  Chunks are sized by ``estimate_tokens_rough``
    to ~``target`` tokens with ``overlap`` token carry.
    """
    target = target if target is not None else _kb_chunk_tokens()
    overlap = overlap if overlap is not None else _kb_chunk_overlap()

    if not text or not text.strip():
        return []

    # 1. Try markdown heading split
    parts = _split_on_headings(text)
    if len(parts) <= 1:
        # 2. Try blank-line paragraph split
        parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(parts) <= 1:
        # 3. Try top-level def/class split (Python)
        parts = _split_on_defs(text)
    if len(parts) <= 1:
        # 4. Raw line split
        parts = text.splitlines()

    # Merge small parts up to target size; carry overlap into next chunk
    return _merge_to_target(parts, target=target, overlap=overlap)


def _split_on_headings(text: str) -> List[str]:
    """Split *text* at markdown ``#`` headings (keep heading in chunk)."""
    positions = [m.start() for m in _MD_HEADING_RE.finditer(text)]
    if not positions:
        return [text]
    parts = []
    positions.append(len(text))
    for i in range(len(positions) - 1):
        chunk = text[positions[i]:positions[i + 1]].strip()
        if chunk:
            parts.append(chunk)
    # Prepend any text before the first heading
    if positions[0] > 0:
        preamble = text[:positions[0]].strip()
        if preamble:
            parts.insert(0, preamble)
    return parts if parts else [text]


def _split_on_defs(text: str) -> List[str]:
    """Split on top-level Python ``def``/``class`` lines."""
    positions = [m.start() for m in _PYDEF_RE.finditer(text)]
    if not positions:
        return [text]
    parts = []
    positions.append(len(text))
    if positions[0] > 0:
        preamble = text[:positions[0]].strip()
        if preamble:
            parts.append(preamble)
    for i in range(len(positions) - 1):
        chunk = text[positions[i]:positions[i + 1]].strip()
        if chunk:
            parts.append(chunk)
    return parts if parts else [text]


def _merge_to_target(parts: List[str], *, target: int, overlap: int) -> List[str]:
    """Merge *parts* into chunks of at most *target* tokens, carrying *overlap*.

    If a single *part* exceeds *target* tokens, it is further split on word
    boundaries so no output chunk grows unboundedly.
    """
    chunks: List[str] = []
    current_lines: List[str] = []
    current_tokens = 0
    overlap_carry = ""

    def _flush():
        nonlocal current_lines, current_tokens, overlap_carry
        if not current_lines:
            return
        chunk_text = overlap_carry + ("\n\n" if overlap_carry else "") + "\n\n".join(current_lines)
        chunks.append(chunk_text.strip())
        overlap_carry = _tail_tokens(chunk_text, overlap)
        current_lines = []
        current_tokens = estimate_tokens_rough(overlap_carry)

    for part in parts:
        part_tokens = estimate_tokens_rough(part)

        # If a single part is too large, split it on word boundaries first
        if part_tokens > target:
            # Flush anything accumulated so far
            _flush()
            # Split oversized part into word-boundary slices
            for sub in _split_by_tokens(part, target):
                sub_tokens = estimate_tokens_rough(sub)
                if current_tokens + sub_tokens > target and current_lines:
                    _flush()
                current_lines.append(sub)
                current_tokens += sub_tokens
            continue

        if current_tokens + part_tokens > target and current_lines:
            _flush()

        current_lines.append(part)
        current_tokens += part_tokens

    # Final flush
    _flush()

    return [c for c in chunks if c]


def _split_by_tokens(text: str, target: int) -> List[str]:
    """Split *text* into slices of at most *target* tokens (word-boundary split)."""
    words = text.split()
    slices: List[str] = []
    current_words: List[str] = []
    current_chars = 0
    target_chars = target * 4  # estimate_tokens_rough uses // 4

    for word in words:
        wlen = len(word) + 1  # +1 for space
        if current_chars + wlen > target_chars and current_words:
            slices.append(" ".join(current_words))
            current_words = [word]
            current_chars = wlen
        else:
            current_words.append(word)
            current_chars += wlen

    if current_words:
        slices.append(" ".join(current_words))

    return slices if slices else [text]


def _tail_tokens(text: str, n_tokens: int) -> str:
    """Return the last ~*n_tokens* token-equivalents of *text* (char-based approx)."""
    if n_tokens <= 0:
        return ""
    tail_chars = n_tokens * 4  # estimate_tokens_rough uses // 4
    return text[-tail_chars:].lstrip()


# ---------------------------------------------------------------------------
# Async ingestion entry point
# ---------------------------------------------------------------------------


async def kb_ingest(
    path: str,
    collection: str = "default",
    recursive: bool = True,
    globs: Optional[List[str]] = None,
    *,
    user_id: str,
    session_id: str,
    source_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Ingest a file or directory into the KB.

    Returns counts: ``{ingested, skipped_secret, skipped_binary, unchanged,
    n_chunks, skipped_office}``.

    Path confinement:
    - Server sessions: confined to ``pm().get_workspace_dir(session_id, user_id)``
    - CLI (workspace_is_project_root): confined to ``Path.cwd()``

    ``source_name`` (optional): a STABLE logical identity to store as the chunk's
    ``source_path`` / dedup key instead of the volatile on-disk path. Used by the
    HTTP upload path so a re-upload of the same file dedups by its original filename
    rather than a fresh ``mkstemp`` path. Only honored for single-file ingestion
    (a directory walk has many sources, so ``source_name`` is ignored there). When
    ``None`` the behavior is byte-identical to before (on-disk path is the source).
    """
    from modules.memory.registry import (
        kb_replace_source as _kb_replace_source,
        kb_source_hash as _kb_source_hash,
    )

    target_path = Path(path)

    # ------------------------------------------------------------------
    # Path confinement
    # ------------------------------------------------------------------
    allowed_root = _resolve_confinement_root(session_id, user_id)
    resolved = target_path.resolve() if target_path.is_absolute() else (allowed_root / path).resolve()

    if not is_within_root(str(resolved), str(allowed_root)):
        return {
            "error": f"Path '{path}' is outside the allowed workspace root '{allowed_root}'",
            "ingested": 0,
            "skipped_secret": 0,
            "skipped_binary": 0,
            "unchanged": 0,
            "n_chunks": 0,
            "skipped_office": 0,
            "skipped_too_large": 0,
            "failed": 0,
        }

    # ------------------------------------------------------------------
    # Walk files
    # ------------------------------------------------------------------
    if resolved.is_file():
        files = [resolved]
        walk_skipped: Dict[str, int] = {
            "secret": 0, "binary": 0, "max_files": 0, "max_bytes": 0, "too_large": 0,
        }
        # Still apply secret/binary guards for single-file ingestion
        if kb_ingest_secret_skip(resolved, allowed_root):
            walk_skipped["secret"] += 1
            files = []
        else:
            # Byte cap also applies to a single file — never read an unbounded file
            # into memory (the directory walk caps cumulative size; this caps the
            # one-file path, which would otherwise OOM on a multi-GB file).
            try:
                if resolved.stat().st_size > _kb_max_bytes():
                    walk_skipped["too_large"] += 1
                    files = []
            except OSError:
                files = []
    elif resolved.is_dir():
        files, walk_skipped = await asyncio.to_thread(
            _iter_files, resolved,
            recursive=recursive,
            globs=globs,
            max_files=_kb_max_files(),
            max_bytes=_kb_max_bytes(),
        )
    else:
        return {
            "error": f"Path '{path}' does not exist or is not accessible.",
            "ingested": 0,
            "skipped_secret": 0,
            "skipped_binary": 0,
            "unchanged": 0,
            "n_chunks": 0,
            "skipped_office": 0,
            "skipped_too_large": 0,
            "failed": 0,
        }

    # ------------------------------------------------------------------
    # Ingest each file
    # ------------------------------------------------------------------
    counts: Dict[str, int] = {
        "ingested": 0,
        "skipped_secret": walk_skipped.get("secret", 0),
        "skipped_binary": walk_skipped.get("binary", 0),
        "unchanged": 0,
        "n_chunks": 0,
        "skipped_office": 0,
        # oversized single file ("too_large") + oversized files in a dir walk ("max_bytes")
        "skipped_too_large": walk_skipped.get("too_large", 0) + walk_skipped.get("max_bytes", 0),
        "failed": walk_skipped.get("unsafe", 0),
        "inventory_limited": walk_skipped.get("inventory_limit", 0),
    }

    target_tokens = _kb_chunk_tokens()
    overlap_tokens = _kb_chunk_overlap()

    # A stable source_name only makes sense for single-file ingestion (a directory
    # walk has many distinct sources). Ignore it for multi-file walks.
    use_source_name = source_name if (source_name and len(files) == 1) else None

    remaining_bytes = _kb_max_bytes()
    for fpath in files:
        source_path_str = use_source_name or str(fpath)

        # Read + hash off the event loop — reading a large file and hashing it is
        # synchronous CPU/IO that would otherwise stall every other session during a
        # bulk ingest.
        try:
            if kb_ingest_secret_skip(fpath, allowed_root):
                counts['skipped_secret'] += 1
                continue
            snapshot = await asyncio.to_thread(
                read_confined_bytes, fpath, allowed_root, remaining_bytes)
            remaining_bytes -= len(snapshot)
            if is_binary_file(fpath, sample=snapshot):
                counts['skipped_binary'] += 1
                continue
            file_hash = hashlib.sha256(snapshot).hexdigest()
        except OSError as e:
            logger.warning("Cannot read %s: %s", fpath, e)
            counts["failed"] += 1
            continue

        # Check existing hash via registry
        existing_hash = await _kb_source_hash(
            user_id=user_id,
            collection=collection,
            source_path=source_path_str,
        )

        if existing_hash == file_hash:
            counts["unchanged"] += 1
            continue

        # Extract text
        text, skip_reason = await _extract_text(fpath, content=snapshot)
        if text is None:
            if skip_reason and skip_reason.startswith("office-skip"):
                counts["skipped_office"] += 1
            else:
                logger.debug("Skipping %s: %s", fpath, skip_reason)
            continue

        # Chunk (CPU-bound) off the event loop too.
        chunks = await asyncio.to_thread(
            _chunk, text, target=target_tokens, overlap=overlap_tokens
        )
        if not chunks:
            continue

        # The provider publishes chunks + hash/count atomically. Never remove
        # the last good source before a replacement transaction commits.
        mime = "application/pdf" if fpath.suffix.lower() == ".pdf" else "text/plain"
        ok = await _kb_replace_source(
            user_id=user_id, collection=collection, source_path=source_path_str,
            source_hash=file_hash, chunks=chunks, mime=mime,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        if not ok:
            counts["failed"] += 1
            continue

        counts["ingested"] += 1
        counts["n_chunks"] += len(chunks)

    return counts


def _resolve_confinement_root(session_id: str, user_id: str) -> Path:
    """Return the allowed root for path confinement.

    Server: ``pm().get_workspace_dir(session_id, user_id)``
    CLI / local mode: ``Path.cwd()``.

    Fail-CLOSED on the server: if the workspace root can't be resolved (``pm()``
    raises), we must NOT silently widen confinement to the process CWD (e.g.
    ``/opt/rob`` — app code/config). Only local mode (single-user) accepts the
    CWD fallback; on the server the error propagates so the caller refuses.
    """
    try:
        from agents.task.path import pm
        path_manager = pm()
        workspace = path_manager.get_workspace_dir(session_id, user_id)
        return workspace.resolve()
    except Exception:
        from core.config_policy import local_mode_enabled
        if local_mode_enabled():
            return Path.cwd().resolve()
        raise


# ---------------------------------------------------------------------------
# Param models
# ---------------------------------------------------------------------------


class KbIngestParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(..., description="File or directory path to ingest into the KB.")
    collection: str = Field("default", description="KB collection name.")
    recursive: bool = Field(True, description="Recurse into subdirectories.")
    globs: Optional[List[str]] = Field(
        None, description="Optional glob patterns to restrict which files are included."
    )


class KbSearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., description="Search query.")
    collection: str = Field("default", description="KB collection to search.")
    limit: int = Field(8, ge=1, le=50, description="Maximum number of results.")


class KbListParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection: Optional[str] = Field(None, description="Filter by collection (omit for all).")


class KbRemoveParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection: str = Field(..., description="KB collection name.")
    source: Optional[str] = Field(None, description="Source path to remove (omit to clear entire collection).")


# ---------------------------------------------------------------------------
# KnowledgeTool
# ---------------------------------------------------------------------------


class KnowledgeTool(BaseTool):
    """Agent tool for tenant-scoped knowledge-base ingest and search."""

    @staticmethod
    def _user(execution_context) -> str:
        return getattr(execution_context, "user_id", None) or "_anonymous_"

    @staticmethod
    def _session(execution_context) -> str:
        return getattr(execution_context, "session_id", None) or ""

    @BaseTool.action(
        "Ingest a file or directory into the agent's knowledge base so future kb_search "
        "calls can retrieve it. Skips secrets, binaries, and unchanged files (dedup by hash).",
        param_model=KbIngestParams,
    )
    async def kb_ingest(
        self, params: KbIngestParams, execution_context=None
    ) -> ActionResult:
        user_id = self._user(execution_context)
        session_id = self._session(execution_context)
        try:
            result = await kb_ingest(
                params.path,
                collection=params.collection,
                recursive=params.recursive,
                globs=params.globs,
                user_id=user_id,
                session_id=session_id,
            )
        except Exception as e:
            return ActionResult(error=f"KB ingest failed: {e}", include_in_memory=True)

        if "error" in result:
            return ActionResult(error=result["error"], include_in_memory=True)

        summary = (
            f"KB ingest complete — ingested={result['ingested']}, "
            f"unchanged={result['unchanged']}, chunks={result['n_chunks']}, "
            f"failed={result.get('failed', 0)}, "
            f"skipped(secret={result['skipped_secret']}, "
            f"binary={result['skipped_binary']}, "
            f"office={result.get('skipped_office', 0)}, "
            f"too_large={result.get('skipped_too_large', 0)})"
        )
        return ActionResult(extracted_content=summary, include_in_memory=True)

    @BaseTool.action(
        "Search the agent's knowledge base for relevant content.",
        param_model=KbSearchParams,
    )
    async def kb_search(
        self, params: KbSearchParams, execution_context=None
    ) -> ActionResult:
        from modules.memory.registry import kb_search as _kb_search
        user_id = self._user(execution_context)
        try:
            result = await _kb_search(
                params.query,
                user_id=user_id,
                collection=params.collection,
                limit=params.limit,
            )
        except Exception as e:
            return ActionResult(error=f"KB search failed: {e}", include_in_memory=True)
        if not result:
            return ActionResult(
                extracted_content="No KB results found.", include_in_memory=True
            )
        return ActionResult(extracted_content=result, include_in_memory=True)

    @BaseTool.action(
        "List ingested sources in the knowledge base.",
        param_model=KbListParams,
    )
    async def kb_list(
        self, params: KbListParams, execution_context=None
    ) -> ActionResult:
        from modules.memory.registry import kb_list_sources as _kb_list_sources
        user_id = self._user(execution_context)
        try:
            sources = await _kb_list_sources(
                user_id=user_id, collection=params.collection
            )
        except Exception as e:
            return ActionResult(error=f"KB list failed: {e}", include_in_memory=True)
        if not sources:
            return ActionResult(
                extracted_content="No sources ingested.", include_in_memory=True
            )
        lines = [f"- {s}" for s in sources]
        return ActionResult(
            extracted_content="KB sources:\n" + "\n".join(lines),
            include_in_memory=True,
        )

    @BaseTool.action(
        "Remove a source (or entire collection) from the knowledge base.",
        param_model=KbRemoveParams,
    )
    async def kb_remove(
        self, params: KbRemoveParams, execution_context=None
    ) -> ActionResult:
        from modules.memory.registry import kb_remove as _kb_remove
        user_id = self._user(execution_context)
        try:
            removed = await _kb_remove(
                user_id=user_id,
                collection=params.collection,
                source=params.source,
            )
        except Exception as e:
            return ActionResult(error=f"KB remove failed: {e}", include_in_memory=True)
        return ActionResult(
            extracted_content=f"Removed {removed} chunk(s) from KB.",
            include_in_memory=True,
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def kb_enabled() -> bool:
    from core.config_policy import AutonomyConfig
    return AutonomyConfig.kb_enabled()


def register_knowledge_tool(force: bool = False) -> bool:
    """Register the 'knowledge' descriptor + class IFF KB_ENABLED (or forced).

    Delegates to ``register_optional_tool``.  No-op when KB is off, so default
    deploys are unaffected.  ``knowledge`` is never in the default ``tool_ids``
    — agents opt in via ``tool_ids=['knowledge']``.
    """
    from tools.descriptors import (
        ToolDescriptor,
        ToolCategory,
        register_optional_tool,
    )

    return register_optional_tool(
        "knowledge",
        KnowledgeTool,
        ToolDescriptor(
            name="knowledge",
            description=(
                "Ingest/search a tenant-scoped knowledge base "
                "(kb_ingest/kb_search/kb_list/kb_remove)"
            ),
            category=ToolCategory.INTEGRATION,
            required_config=[],
            init_priority=80,
            is_optional=True,
        ),
        kb_enabled,
        force=force,
    )
