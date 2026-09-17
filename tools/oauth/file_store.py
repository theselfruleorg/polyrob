"""Persisted token store for :class:`tools.oauth.manager.OAuthManager` (T2.4 Task 1).

``OAuthManager``'s default store is an in-memory ``dict`` — tokens die on every
restart. ``FileTokenStore`` is a drop-in ``MutableMapping`` (the exact shape
``OAuthManager.__init__(store=...)`` accepts) backed by one JSON file, so an
operator constructs ``OAuthManager(store=FileTokenStore())`` to get persistence
with zero change to the manager itself. This module is library-only — nothing
imports it yet (unwired), so constructing a plain ``OAuthManager()`` remains
byte-identical in-memory behaviour.

**Values stay opaque.** ``OAuthManager`` Fernet-encrypts every token BEFORE it
reaches ``store[key] = ...`` (see ``manager.py::store_token``); this store never
sees plaintext, and never invokes ``tools.mcp.security`` itself. It only
base64-encodes the ciphertext bytes so they fit inside a JSON string, and
base64-decodes them back to bytes on read. A stolen token file is exactly as
useless without the Fernet key as the encrypted blob was on its own.

**Key encoding.** ``OAuthManager`` keys are ``(user_id, provider)`` tuples, but
JSON object keys must be strings. Each part is percent-encoded
(``urllib.parse.quote(part, safe="")``) and the two parts joined with ``"|"``:
``f"{quote(user_id)}|{quote(provider)}"``. Percent-encoding a literal ``"|"``
inside ``user_id`` or ``provider`` turns it into ``"%7C"`` BEFORE the join, so a
``user_id`` that happens to contain ``"|"`` can never collide with the
separator or with another (user_id, provider) pair — e.g. ``("a|b", "c")`` and
``("a", "b|c")`` serialize to distinct strings (``"a%7Cb|c"`` vs
``"a|b%7Cc"``) and round-trip to their original tuples on read.

**Fault isolation.** A missing file is empty (first run). A corrupt/unreadable
file fails OPEN to an empty in-memory store with exactly ONE loud warning
logged at load time — it never raises, and it never deletes the bad file: on
load, a corrupt file is renamed aside to ``<name>.corrupt-<unix-ts>`` (via
``os.replace``, same directory) BEFORE the empty store is returned, and the
warning names the new path. Renaming immediately — rather than merely leaving
it in place — is what makes the preservation promise durable: the corrupt
bytes are out of the way of the original path, so the next successful write
(which creates a fresh file there) can never silently clobber the evidence.
If the rename itself fails (e.g. read-only filesystem) a warning is still
logged and the original corrupt file is left in place, untouched.

**Permissions.** Every write goes through a temp-file-then-``os.replace``
sequence in the same directory (atomic on POSIX, no half-written file ever
visible at the real path) and the temp file is ``chmod``'d ``0600`` before the
replace, so the token file is never briefly world/group-readable.

**Path.** Defaults to ``<data_home>/.mcp_oauth_tokens.json`` where
``data_home`` is ``core.runtime_paths.resolve_data_home()`` — the same data-home
axis every other sidecar store (``goals.db``, ``cron.db``, ``memory.db``, ...)
resolves through, so the token file lives next to them under
``POLYROB_DATA_DIR`` (server) or ``cwd/.polyrob`` (local CLI). It is
deliberately NOT added to ``core/db_manifest.py::SIDECAR_DB_NAMES`` — that
manifest is scoped to SQLite ``*.db`` files opened under
``surfaces/``/``core/surfaces/``/``tools/hf_deploy/`` (see its docstring and
the grep-based completeness test), and this is a plain JSON file outside that
inventory. A future backup/restore pass that wants to snapshot this file too
should extend that manifest (or its own) explicitly — noted here so it isn't
silently forgotten.

**Multi-process caveat (`workers>1`, T2.4 review Minor).** Each instance loads
the whole file into memory once at construction and holds that copy for the
process's lifetime — mirroring the in-process ``SessionRegistry``'s
``workers>1`` landmine (``agents/task/sqlite_session_registry.py``): two
worker processes each hold their own snapshot, so a write from one is
invisible to the other's in-memory copy, and the next write from either
silently clobbers the other's on-disk write (last-writer-wins, not a crash,
not detected). Fine for the single-process CLI/dev shape this ships for
today; a genuine `workers>1` deployment needs a cross-process-safe store
(e.g. the same WAL+jittered-retry SQLite pattern `SqliteSessionRegistry` uses)
before concurrent token writes are safe here.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import stat
import tempfile
import time
from collections.abc import MutableMapping
from pathlib import Path
from typing import Iterator, Optional, Tuple
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)

Key = Tuple[str, str]

TOKENS_FILENAME = ".mcp_oauth_tokens.json"


def _default_path() -> Path:
    from core.runtime_paths import resolve_data_home
    return resolve_data_home() / TOKENS_FILENAME


def _encode_key(key: Key) -> str:
    user_id, provider = key
    return f"{quote(str(user_id), safe='')}|{quote(str(provider), safe='')}"


def _decode_key(raw: str) -> Key:
    user_part, _, provider_part = raw.partition("|")
    return unquote(user_part), unquote(provider_part)


class FileTokenStore(MutableMapping):
    """One JSON file, keyed by ``(user_id, provider)``, values opaque bytes.

    Implements the full :class:`collections.abc.MutableMapping` contract
    (``__getitem__``/``__setitem__``/``__delitem__``/``__iter__``/``__len__``,
    plus ``__contains__``/``get``/``keys``/``values``/``items``/``update``/``pop``
    from the mixin) so it is a drop-in replacement for the plain ``dict``
    :class:`tools.oauth.manager.OAuthManager` defaults to.

    Every mutation (``__setitem__``/``__delitem__``) flushes the WHOLE file —
    simple and correct for the tiny (per-tenant-per-provider) token count this
    is scoped to; not intended for high-churn or large-N use.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else _default_path()
        self._data: dict = self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as e:
            # UNREADABLE is not CORRUPT. On 2026-09-17 prod's polyrob-email unit
            # (a non-root service identity) got EACCES on the root-owned 0600
            # store that polyrob.service had just written, and the corruption
            # branch below RENAMED IT ASIDE — deleting the agent's freshly
            # imported X OAuth2 pair from under the process that owned it.
            # A process that cannot read a file has no standing to move it.
            logger.warning(
                "FileTokenStore: %s is not readable by this process (%s) — using an "
                "empty in-memory token store for this process and leaving the file "
                "untouched. If this unit needs the store, fix its ownership/mode.",
                self._path, e,
            )
            return {}
        try:
            raw = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError(f"expected a JSON object at top level, got {type(raw).__name__}")
            decoded: dict = {}
            for k, v in raw.items():
                decoded[_decode_key(k)] = base64.b64decode(str(v).encode("ascii"))
            return decoded
        except Exception as e:
            corrupt_path = self._path.parent / f"{self._path.name}.corrupt-{int(time.time())}"
            try:
                os.replace(str(self._path), str(corrupt_path))
                logger.warning(
                    "FileTokenStore: %s was corrupt or unreadable (%s) — renamed aside "
                    "to %s and starting with an empty in-memory token store. Inspect or "
                    "recover the renamed file if needed; the next successful write "
                    "creates a fresh file at the original path.",
                    self._path, e, corrupt_path,
                )
            except OSError as rename_exc:
                logger.warning(
                    "FileTokenStore: %s is corrupt or unreadable (%s) and could not be "
                    "renamed aside (%s) — starting with an empty in-memory token store. "
                    "The file on disk is left untouched.",
                    self._path, e, rename_exc,
                )
            return {}

    def _flush(self) -> None:
        payload = {
            _encode_key(k): base64.b64encode(v).decode("ascii")
            for k, v in self._data.items()
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".mcp_oauth_tokens.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            self._match_target_identity(tmp_name)
            os.replace(tmp_name, str(self._path))
        except Exception:
            try:
                os.remove(tmp_name)
            except OSError:
                pass
            raise

    def _match_target_identity(self, tmp_name: str) -> None:
        """Give the temp file the group + mode the FINAL path should carry.

        Prod runs each unit as its own identity (`polyrob-agent`, `polyrob-web`,
        `polyrob-email`) sharing the `polyrob-data` group, and every data file
        follows `<writer>:polyrob-data 0660` (deployment/hardening/
        install-service-identities.sh). This store wrote `0600` in the writer's
        primary group, so a pair imported by the root CLI was UNREADABLE by the
        agent unit that needed it (EACCES, 2026-09-17) — and an agent-written
        refresh would have been unreadable by the owner's CLI the same way.

        Rule: an existing target keeps its mode and group; a new file inherits
        the parent directory's group and is `0660` when that directory is
        group-writable (the shared-data convention), else `0600`. A chown the
        process is not allowed to make is skipped, never fatal.
        """
        try:
            st = os.stat(self._path)
            mode, gid = stat.S_IMODE(st.st_mode), st.st_gid
        except FileNotFoundError:
            dst = os.stat(self._path.parent)
            gid = dst.st_gid
            mode = 0o660 if (dst.st_mode & stat.S_IWGRP) else 0o600
        try:
            os.chown(tmp_name, -1, gid)
        except OSError:
            pass
        os.chmod(tmp_name, mode)

    # -- MutableMapping ---------------------------------------------------

    def __getitem__(self, key: Key) -> bytes:
        return self._data[key]

    def __setitem__(self, key: Key, value: bytes) -> None:
        # Read-modify-write against the CURRENT file: a second instance over the
        # same file (e.g. the X login store keyed provider "x" and the signup
        # progress store keyed "x_signup" share .x_session.json) holds a snapshot
        # taken at ITS construction, so flushing that stale dict would erase rows
        # this instance never saw. Reloading here folds in the other instance's
        # writes before we flush. (Cross-PROCESS workers>1 is still last-writer-
        # wins — see the module docstring; this only closes the same-process
        # two-instance clobber.)
        self._data = self._load()
        self._data[key] = bytes(value)
        self._flush()

    def __delitem__(self, key: Key) -> None:
        # Same read-modify-write discipline as __setitem__: reload so deleting one
        # key can't flush away another instance's rows (the X session clobber).
        self._data = self._load()
        if key not in self._data:
            raise KeyError(key)
        del self._data[key]
        self._flush()

    def __iter__(self) -> Iterator[Key]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __repr__(self) -> str:  # never echo values (opaque ciphertext)
        return f"FileTokenStore(path={self._path!s}, n={len(self._data)})"
