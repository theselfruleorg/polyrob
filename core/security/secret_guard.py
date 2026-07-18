"""Secret and binary file guard (canonical home: core.security — R-4 2026-07-17).

Pure, dependency-free helpers used by context-reference ingestion and knowledge-base
loaders to decide whether a file is safe to read or ingest.  Three concerns:

  1. ``is_secret_path`` — does this path look like a credential / secret file?
  2. ``is_binary_file`` — is this file binary (unreadable as text)?
  3. ``estimate_tokens_rough`` — cheap token-count estimate for budget checks.

No I/O side-effects on ``is_secret_path``; ``is_binary_file`` reads at most 4 096 bytes.
"""
from __future__ import annotations

import fnmatch
import mimetypes
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Secret detection
# ---------------------------------------------------------------------------

# Name globs matched case-insensitively against the file's basename.
# Entries that contain a path separator are matched against the path's
# components (see _matches_path_glob below).
SECRET_NAME_GLOBS: tuple[str, ...] = (
    ".env*",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_ed25519",
    "id_*",
    "*credential*",
    "*secret*",
    "*.p12",
    "*.pfx",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    "bot.db",
    # polyrob-specific relative-path patterns
    "config/.env.*",
    ".polyrob/.env",
    ".rob/.env",  # legacy home (still holds live secrets via transition fallback)
    # H3 (2026-07-15): the append-only wallet spend audit + its .hwm sidecar.
    "wallet/audit.jsonl*",
    # M2/sibling-gap fix (2026-07-16): `is_secret_path` is the SEPARATE guard
    # consumed by KB ingest / context-references / project-context (unlike
    # `is_credential_file`, which is the filesystem/coding write-refusal
    # guard). M2 added the snapshot globs only to CREDENTIAL_NAME_GLOBS below,
    # leaving this list unable to catch a snapshot-backed copy of a secret
    # config file (`.polyrob/snapshots/<ts>/config/00_.env.production` — a
    # renamed copy of `config/.env.production`, which can hold MASTER_SEED)
    # from flowing into model context via read-only ingestion paths. See the
    # CREDENTIAL_NAME_GLOBS comment below for the full rationale — the same
    # three globs, same reasoning, applied here.
    "*.env.*",
    "snapshots/*/config/*",
    "snapshots/*/dirs/*",
)

# Tool-facing subset: unambiguous CREDENTIAL filenames only. Used by the
# file-editing tools (filesystem/coding) to refuse reading/writing secret files
# that are legitimately IN-ROOT under POLYROB_LOCAL (workspace == project cwd, so
# confinement can't catch them). Deliberately NARROWER than SECRET_NAME_GLOBS:
# excludes the broad ``*secret*``/``*credential*`` substrings (would block a
# Python ``secrets.py``) and the ``data/`` dir rule (a legit project data dir),
# so ordinary project files stay editable. See is_credential_file().
CREDENTIAL_NAME_GLOBS: tuple[str, ...] = (
    ".env*",
    "*.env",   # WS-7: catch `polyrob.env`, `prod.env` etc — the prod env file's
               # basename does NOT start with `.env`, so `.env*` alone missed it,
               # leaving the file that holds AGENT_COMPUTE_POSTURE + approval flags
               # writable by an agent surface (a self-posture-escalation path).
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ed25519",
    "id_dsa",
    "id_ecdsa",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    ".htpasswd",
    # polyrob-specific relative-path patterns
    "config/.env.*",
    ".polyrob/.env",
    ".rob/.env",
    # H3 (2026-07-15): the wallet policy state — the write-once derivation record
    # and the append-only spend audit. Under POLYROB_LOCAL the workspace IS the
    # project cwd (which contains .polyrob/wallet/), so these are otherwise
    # agent-writable: zeroing the audit resets the rolling daily-cap window + the
    # payment replay-guard on the next restart, and rewriting meta.json bypasses
    # the write-once derivation guard (silent address flip). Matched anywhere the
    # `wallet/` dir appears (data-home is CWD-relative in local mode).
    "wallet/meta.json",
    # Minor #6 (2026-07-16): trailing `*` also catches the `.hwm` (high-water-mark)
    # sidecar `wallet/audit.jsonl.hwm` written alongside the audit log itself —
    # same money-policy-state rationale as the exact-match audit file.
    "wallet/audit.jsonl*",
    # M2 (2026-07-15): `cli/update/snapshot.py` backs up config files and dirs
    # under `<data_home>/snapshots/<ts>_<ver>/{config,dirs}/` with a NUMERIC
    # INDEX PREFIX on the copy name (`config/00_.env.production`,
    # `dirs/00_wallet/`). That prefix defeats every basename-exact / adjacent-
    # component rule above (`.env*`, `config/.env.*`, `wallet/meta.json`, ...) —
    # a local box's `config/.env.production` (holds MASTER_SEED) becomes
    # `read_file`-able once backed up. Two independent nets close this:
    #  1. `*.env.*` — a basename-only glob (deliberately broad: it matches ANY
    #     name containing literal ".env." wherever it sits, incl. a numeric
    #     prefix or a benign `test.env.example`). Over-denying a non-secret
    #     example file is an acceptable cost against under-denying a renamed
    #     credential copy.
    #  2. `snapshots/*/config/*` + `snapshots/*/dirs/*` — location rules over
    #     the snapshot layout itself (`*` = one wildcarded path component, so
    #     the numeric prefix on EITHER the timestamp dir or the copy name is
    #     irrelevant). This also covers dir backups whose basenames aren't
    #     credential-shaped (e.g. `dirs/00_wallet/meta.json`'s basename is
    #     just `meta.json`) — the gap `wallet/meta.json` alone can't catch
    #     once the parent dir is renamed `00_wallet`.
    "*.env.*",
    "snapshots/*/config/*",
    "snapshots/*/dirs/*",
)

# WS-7: absolute system config directories whose contents are HARD-DENIED for any
# agent-writable surface (esp. the posture-2 `self_env` patch_source, which reaches
# the install tree). These hold the frozen security flags / secrets; the agent must
# never edit them. Source files elsewhere under the install tree stay patchable.
PROTECTED_CONFIG_DIRS: tuple[str, ...] = (
    "/etc/polyrob",
    "/etc/rob",
)


def is_protected_config_path(path: Path) -> bool:
    """True if *path* is inside a protected system-config dir (WS-7).

    Realpath-free NAME/prefix check on the given path (callers that need traversal
    safety realpath first). Complements :func:`is_credential_file` (name-shaped
    secrets anywhere) with a location rule for absolute system config the self_env
    tool could otherwise reach.

    Also protects ``preferences.toml`` and ``contract.md`` files under any
    ``identity/`` path segment (owner-UX P1 T6) — these files are only writable
    through gated action/CLI/webview seams, never by agent file tools.
    """
    try:
        s = str(path)
    except Exception:
        return False
    norm = s.replace("\\", "/")

    # Check system config directories
    for d in PROTECTED_CONFIG_DIRS:
        if norm == d or norm.startswith(d + "/"):
            return True

    # Check for protected files under an identity/ path segment. Case-insensitive
    # and segment-robust: compares lowered path PARTS (not a substring test), so
    # it catches a mixed-case segment (``Identity/``, case-insensitive
    # filesystems) and a bare relative path with no leading slash
    # (``identity/u/preferences.toml``), while still rejecting a segment that
    # merely CONTAINS "identity" as a substring (e.g. ``nonidentity/``).
    basename = path.name.lower()
    if basename in ("preferences.toml", "contract.md"):
        parts_lower = [part.lower() for part in path.parts]
        if "identity" in parts_lower:
            return True

    return False


def is_credential_file(path: Path) -> bool:
    """Return *True* if *path*'s NAME looks like a credential/secret file.

    Basename + multi-component path-glob checks against ``CREDENTIAL_NAME_GLOBS``
    only — NO parent-dir (``data/``) rule and NO ``*secret*`` substring, so it is
    safe to apply to arbitrary project files in local mode. This is the guard the
    filesystem/coding tools use to refuse a secret file that lives inside the
    workspace (the gap the confinement floor can't cover when workspace == cwd).
    """
    name = path.name
    for glob in CREDENTIAL_NAME_GLOBS:
        if "/" not in glob:
            if _matches_name_glob(name, glob):
                return True
        elif _matches_path_glob(path, glob):
            return True
    return False


# Any *directory component* of the path that matches one of these names makes
# the whole path a secret path.
SECRET_DIR_PARTS: frozenset[str] = frozenset({
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".azure",
    ".config/gh",
    "data",
})


def _path_parts_lower(path: Path) -> list[str]:
    """Return the parts of *path* in lower-case (POSIX separators)."""
    return [p.lower() for p in path.parts]


def _matches_name_glob(name: str, glob: str) -> bool:
    """Case-insensitive fnmatch of *name* against *glob*."""
    return fnmatch.fnmatch(name.lower(), glob.lower())


def _matches_path_glob(path: Path, glob: str) -> bool:
    """Match a multi-component glob like ``config/.env.*`` or ``.rob/.env``.

    Strategy: slide a window of ``len(glob_parts)`` consecutive path parts and
    check if they join (with ``/``) case-insensitively to the glob.
    """
    glob_parts = glob.lower().replace("\\", "/").split("/")
    n = len(glob_parts)
    parts = [p.lower() for p in path.parts]
    for i in range(len(parts) - n + 1):
        window = "/".join(parts[i : i + n])
        # Rebuild glob as a single fnmatch pattern with / separators.
        if fnmatch.fnmatch(window, "/".join(glob_parts)):
            return True
    return False


def _is_data_db(path: Path) -> bool:
    """``*.db`` rule: applies only when a parent directory is named ``data``."""
    if not path.name.lower().endswith(".db"):
        return False
    # Check parent directories (not the file itself).
    return any(p.lower() == "data" for p in path.parts[:-1])


def _dir_part_match(path: Path, dir_parts: Iterable[str]) -> bool:
    """True if any directory *component* of *path* matches a ``dir_parts`` entry.

    Multi-segment entries (e.g. ``.config/gh``) are matched as a consecutive
    window of path parts joined with ``/``.
    """
    parts_lower = [p.lower() for p in path.parts[:-1]]  # skip the filename
    for dp in dir_parts:
        dp_lower = dp.lower()
        dp_segs = dp_lower.split("/")
        n = len(dp_segs)
        window_pat = "/".join(dp_segs)
        for i in range(len(parts_lower) - n + 1):
            window = "/".join(parts_lower[i : i + n])
            if fnmatch.fnmatch(window, window_pat):
                return True
    return False


def is_secret_path(path: Path, *, root: Path) -> bool:  # noqa: ARG001
    """Return *True* if *path* looks like a credential / secret file.

    Checks (in order):
    1. Any parent directory component matches ``SECRET_DIR_PARTS``.
    2. The basename matches a simple (no ``/``) ``SECRET_NAME_GLOBS`` entry.
    3. The path contains a multi-component pattern from ``SECRET_NAME_GLOBS``
       (e.g. ``config/.env.production``, ``.rob/.env``).
    4. The ``*.db`` rule: applies only when a parent dir is named ``data``.

    ``root`` is accepted for callers that want to pass it (future: relative-path
    normalisation), but is not required for the current checks.
    """
    # 1. Parent-directory check
    if _dir_part_match(path, SECRET_DIR_PARTS):
        return True

    name = path.name

    # 2. Simple (no-slash) glob matches on the basename
    simple_globs = [g for g in SECRET_NAME_GLOBS if "/" not in g]
    for glob in simple_globs:
        if _matches_name_glob(name, glob):
            return True

    # 3. Multi-component path patterns
    path_globs = [g for g in SECRET_NAME_GLOBS if "/" in g]
    for glob in path_globs:
        if _matches_path_glob(path, glob):
            return True

    # 4. *.db inside a `data/` directory
    if _is_data_db(path):
        return True

    return False


# ---------------------------------------------------------------------------
# Binary detection
# ---------------------------------------------------------------------------

# Extensions we always treat as plain text regardless of mime type.
_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".txt", ".json", ".yaml", ".yml", ".toml",
    ".js", ".ts", ".tsx", ".sh",
    ".csv", ".html", ".xml", ".rst",
    ".ini", ".cfg", ".sql", ".md", ".py",
})

# Extensions for extractable document formats — callers decide how to read
# them; we report them as NOT binary so they are not silently skipped.
_EXTRACTABLE_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".docx", ".doc", ".odt",
    ".xlsx", ".xls", ".ods",
    ".pptx", ".ppt",
    ".epub", ".rtf",
})


def is_binary_file(path: Path) -> bool:
    """Return *True* if *path* is a binary file that cannot be decoded as text.

    Detection order:
    1. Text-extension allowlist → always False (text).
    2. Extractable-document list → False (callers handle extraction).
    3. mimetypes guess: ``text/*`` → False.
    4. Null-byte sniff of the first 4 096 bytes → True if a null byte is found.
    5. Default → False (assume text when uncertain).
    """
    suffix = path.suffix.lower()

    if suffix in _TEXT_EXTENSIONS:
        return False
    if suffix in _EXTRACTABLE_EXTENSIONS:
        return False

    mime, _ = mimetypes.guess_type(str(path))
    if mime and mime.startswith("text/"):
        return False

    # Null-byte sniff
    try:
        data = path.read_bytes()[:4096]
    except OSError:
        return False  # can't read → let the caller handle it

    return b"\x00" in data


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def estimate_tokens_rough(text: str) -> int:
    """Rough token count: ``max(1, len(text) // 4)`` (chars-per-token ≈ 4)."""
    return max(1, len(text) // 4)
