"""Durable, append-only audit sink for the wallet PolicyGate (G3).

An in-memory ``list`` of audit entries is lost on restart, so the harness can't sum
lifetime/rolling spend across a service restart — a mainnet prerequisite. This sink
is a ``list`` subclass that mirrors every appended entry to an append-only JSONL file
and reloads prior entries on construction, so a fresh ``PolicyGate(audit_sink=...)``
sees the full history. Default behavior is unchanged: PolicyGate uses a plain list
unless a sink is injected (only the factory does, when the wallet is enabled).

I/O failures latch an unhealthy state. PolicyGate refuses further spends until
the durable ledger is repaired and reloaded; telemetry must not reset money caps.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import asyncio
from contextlib import asynccontextmanager, contextmanager
import logging
import os
import tempfile
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

#: Domain separation for the ledger seal key (derived from the master seed).
_SEAL_KEY_INFO = b"polyrob/wallet-audit-seal/v1"
_SEAL_VERSION = 1
_ZERO_CHAIN = b"\x00" * 32
_AUTO = object()


def _derive_seal_key() -> Optional[bytes]:
    """The ledger seal key, or None when this process holds no master seed.

    The key is derived from ``AGENT_WALLET_MASTER_SEED`` — a secret that lives
    in a root-owned env file and is popped from ``os.environ`` at wallet load,
    so a same-UID child (shell, MCP server, lazy build) never sees it and
    cannot forge a seal. A seedless process (the web/email units, the agent
    under ``WALLET_SIGNER=remote``) neither verifies nor seals; under remote
    the signer's own ledger is the authoritative one.
    """
    try:
        from core.security.custody_env import custody_secret
        seed = custody_secret("AGENT_WALLET_MASTER_SEED")
    except Exception:
        return None
    seed = (seed or "").strip()
    if len(seed) < 32:
        return None
    return hmac.new(seed.encode("utf-8"), _SEAL_KEY_INFO, hashlib.sha256).digest()


def _chain_step(chain: bytes, line: str) -> bytes:
    return hashlib.sha256(chain + line.encode("utf-8")).digest()


def _seal_mac(key: bytes, count: int, chain: bytes) -> str:
    msg = b"seal|%d|" % count + chain
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


class JsonlAuditSink(list):
    """A list of audit dicts mirrored to an append-only JSONL file.

    Tamper-evident (H3, 2026-07-15): a monotonic high-water mark (the count of
    entries ever persisted) is written to a ``<path>.hwm`` sidecar on every
    append. On reload, if the recovered JSONL is SHORTER than that mark the sink
    warns loudly — an injected local agent that truncates ``audit.jsonl`` would
    otherwise silently reset the rolling-24h spend cap AND the payment-replay
    guard on the next restart (both are rebuilt entirely from this sink). This is
    the defense-in-depth backstop; the tool-facing deny surface that blocks the
    write in the first place is a separate change. The mark never regresses, so
    the evidence survives further restarts. Damage or I/O failure marks the sink unhealthy; PolicyGate blocks
    subsequent spending until storage is repaired and reloaded.
    """

    def __init__(self, path: str, *, lock_timeout: Optional[float] = None,
                 seal_key=_AUTO):
        super().__init__()
        self._path = path
        #: WAL-9: rows are chain-hashed and the chain head is MAC'd into
        #: ``<path>.seal`` with a key derived from the master seed. A seed-holding
        #: process verifies the sealed prefix at load (an edited row = unhealthy,
        #: spending refused) and seals every row it appends. Rows past the seal
        #: (a seedless writer, a crash between append and seal) are re-sealed.
        self._seal_key = _derive_seal_key() if seal_key is _AUTO else seal_key
        self._seal_path = path + ".seal"
        self._chain = _ZERO_CHAIN
        self._chain_n = 0
        #: 068 R3-4: when set, every lock this sink takes (the construction-time
        #: load included) gives up after this many seconds instead of waiting
        #: forever — an operator path must not hang behind a stalled spender.
        self._lock_timeout = lock_timeout
        self._hwm_path = path + ".hwm"
        self._hwm = 0
        self.healthy = True
        self._reservation_fd = None
        self._reservation_owner = None
        #: Bytes of the JSONL already folded into the in-memory list. Advanced by
        #: both `_load` and `append`, so `refresh()` reads only what ANOTHER writer
        #: added and can never double-count this process's own entries.
        self._offset = 0
        # A READ never creates the store (2026-09-21). Constructing the sink used
        # to mkdir `<home>/wallet/` and mint an `audit.jsonl.lock` before reading
        # a byte, so every read-only caller that reaches `get_policy_gate()` —
        # `polyrob doctor`'s money section, the unified ledger's caps block —
        # planted a wallet directory in whatever home it resolved (in a bare CWD,
        # `./.polyrob/wallet/`). With neither the ledger nor its high-water mark
        # on disk there is nothing to load and nothing to serialise against, so
        # the directory and the lock are deferred to the first real write.
        if os.path.exists(path) or os.path.exists(self._hwm_path):
            self._ensure_parent()
            with self._write_lock():
                self._load()
        else:
            record = self._wallet_of_record()
            if record:
                # CR-L31: a wallet of record with NEITHER the ledger NOR its
                # high-water mark is the "delete both before a restart" reset —
                # it would silently zero the rolling cap and the replay guard.
                # Every wallet created since this check writes the genesis mark
                # (see ensure_genesis), so this state is never a fresh install.
                self.healthy = False
                logger.error(
                    "wallet audit sink %s: the ledger and its high-water mark are both "
                    "MISSING but a wallet of record exists (%s) — the rolling-24h spend "
                    "cap and payment-replay guard would restart from zero. Spending is "
                    "refused. If this wallet has truly never spent, the owner creates an "
                    "empty %s to confirm it; otherwise restore the ledger from backup.",
                    path, record, path)

    #: The files whose presence means "this wallet already exists".
    _RECORD_FILES = ("meta.json", "public_identity.json")

    def _wallet_of_record(self) -> Optional[str]:
        parent = os.path.dirname(self._path) or "."
        for name in self._RECORD_FILES:
            candidate = os.path.join(parent, name)
            try:
                if os.path.isfile(candidate):
                    return candidate
            except OSError:
                continue
        return None

    def ensure_genesis(self) -> None:
        """Write the genesis high-water mark (0) for a wallet that has none.

        Called where a wallet comes into being (a seeded ``AgentWallet``,
        ``write_scheme_once``) so "no ledger and no mark" can later be read as
        tampering (CR-L31). A no-op when either file exists or the sink is
        unhealthy — genesis must never paper over a missing ledger.
        """
        if not self.healthy:
            return
        if os.path.exists(self._path) or os.path.exists(self._hwm_path):
            return
        self._ensure_parent()
        with self._write_lock():
            if not (os.path.exists(self._path) or os.path.exists(self._hwm_path)):
                self._write_hwm()

    def _ensure_parent(self) -> None:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    @asynccontextmanager
    async def reserve(self):
        """Hold the shared file lock over check, network spend and record.

        Nonblocking flock attempts keep the event loop responsive, including
        cancellation while another process is spending. Unsupported platforms
        refuse instead of silently weakening the cap.
        """
        import fcntl
        self._ensure_parent()
        fd = os.open(self._path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.05)
            self._reservation_fd = fd
            self._reservation_owner = asyncio.current_task()
            yield
        finally:
            if self._reservation_fd == fd:
                self._reservation_fd = None
                self._reservation_owner = None
            os.close(fd)

    @contextmanager
    def reserve_blocking(self, timeout: float = 30.0):
        """The synchronous twin of :meth:`reserve` for a CLI/operator path.

        068 N3: normal spending takes this audit lock FIRST and touches the
        submission journal second (``SpendLedger.record`` -> ``mark_booked``).
        An operator release must use the same order, or the two can each hold
        one lock while waiting on the other. Appends made while this is held
        reuse the reservation instead of re-locking (a second ``flock`` on a new
        descriptor would block this very process).
        """
        import fcntl
        import time as _time
        self._ensure_parent()
        fd = os.open(self._path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = _time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if _time.monotonic() >= deadline:
                        raise TimeoutError("wallet audit lock is held by a spend in "
                                           "progress; retry when it finishes")
                    _time.sleep(0.02)
            self._reservation_fd = fd
            self._reservation_owner = None
            yield
        finally:
            if self._reservation_fd == fd:
                self._reservation_fd = None
                self._reservation_owner = None
            os.close(fd)

    @contextmanager
    def _write_lock(self):
        if self._reservation_fd is not None:
            try:
                owner = asyncio.current_task()
            except RuntimeError:  # A synchronous call on a different thread.
                owner = None
            if owner is not self._reservation_owner:
                raise RuntimeError('wallet audit reservation belongs to another task')
            yield
            return
        import fcntl
        self._ensure_parent()
        fd = os.open(self._path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if self._lock_timeout is None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                import time as _time
                deadline = _time.monotonic() + self._lock_timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if _time.monotonic() >= deadline:
                            raise TimeoutError("wallet audit lock is held by a spend in "
                                               "progress; retry when it finishes")
                        _time.sleep(0.02)
            yield
        finally:
            os.close(fd)

    def refresh(self) -> int:
        """Fold in entries appended by another process; return how many.

        The ledger is a single file shared by every wallet-touching process, but it
        was read once at construction — so a second process (the owner running a
        CLI trade while the daemon trades) computed its rolling-24h spend from a
        snapshot taken at its own start and never saw the other's later spends.
        Both could clear a nearly-exhausted daily cap. Reading forward from the
        byte offset makes the durable file, not one process's memory, the shared
        view. An I/O or parse error retains the list for diagnostics but marks
        the sink unhealthy so it cannot authorize new spending.
        """
        try:
            size = os.path.getsize(self._path)
        except FileNotFoundError:
            if self._offset or self._hwm:
                self.healthy = False
            return 0
        except OSError:
            self.healthy = False
            return 0
        if size <= self._offset:
            if size < self._offset:
                self.healthy = False
            # Truncation (size < offset) is the tamper case the high-water mark
            # already reports loudly; don't silently re-read from 0 here.
            return 0
        added = 0
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                fh.seek(self._offset)
                for line in fh:
                    if not line.endswith("\n"):
                        self.healthy = False
                        break  # incomplete durable record: spending must wait for repair
                    self._offset += len(line.encode("utf-8"))
                    line = line.strip()
                    if not line:
                        continue
                    self._extend_chain(line)
                    try:
                        list.append(self, json.loads(line))  # base append: no re-write
                        added += 1
                    except json.JSONDecodeError:
                        self.healthy = False
                        continue
        except (OSError, UnicodeError) as e:
            self.healthy = False
            logger.warning("wallet audit sink refresh failed (%s): %s", self._path, e)
            return added
        if added:
            self._hwm = max(self._hwm, len(self))
        return added

    def _read_hwm(self) -> Optional[int]:
        try:
            with open(self._hwm_path, "r", encoding="utf-8") as fh:
                count = int(fh.read().strip())
            if count < 0:
                raise ValueError("negative high-water mark")
            return count
        except FileNotFoundError:
            return None  # legacy ledgers predate the sidecar
        except (OSError, ValueError, UnicodeError):
            self.healthy = False
            return None

    def _write_hwm(self) -> None:
        self._atomic_write(self._hwm_path, str(self._hwm), "high-water")

    def _atomic_write(self, target: str, text: str, what: str) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=os.path.dirname(target) or ".",
                prefix=".audit-%s-" % what, delete=False,
            ) as fh:
                temporary = fh.name
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, target)
            directory = os.open(os.path.dirname(target) or ".", os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as e:
            self.healthy = False
            logger.warning("wallet audit %s write failed (%s): %s", what, target, e)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    # -- WAL-9 seal ---------------------------------------------------------
    def _extend_chain(self, line: str) -> None:
        self._chain = _chain_step(self._chain, line)
        self._chain_n += 1

    def _read_seal(self):
        """``(count, mac)`` from the sidecar, ``None`` when absent; unhealthy on damage."""
        try:
            with open(self._seal_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            count, mac = int(raw["n"]), str(raw["mac"])
            if count < 0 or int(raw.get("v", 0)) != _SEAL_VERSION:
                raise ValueError("bad seal")
            return count, mac
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            return False

    def _write_seal(self) -> None:
        if not self._seal_key or not self.healthy:
            return
        payload = {"v": _SEAL_VERSION, "n": self._chain_n,
                   "mac": _seal_mac(self._seal_key, self._chain_n, self._chain)}
        self._atomic_write(self._seal_path, json.dumps(payload), "seal")

    def _verify_seal(self, chain_at: dict) -> None:
        """Check the sealed prefix; re-seal the tail. Runs under the write lock."""
        if not self._seal_key:
            return
        seal = self._read_seal()
        if seal is False:
            self.healthy = False
            logger.error("wallet audit seal %s is unreadable — the ledger cannot be "
                         "verified; spending is refused until the owner restores it.",
                         self._seal_path)
            return
        if seal is None:
            if self._chain_n:
                logger.warning(
                    "wallet audit ledger %s has %d unsealed rows — sealing them now. "
                    "Expected ONCE (the first start with ledger sealing); if it repeats, "
                    "something removed %s.", self._path, self._chain_n, self._seal_path)
            self._write_seal()
            return
        count, mac = seal
        head = chain_at.get(count)
        if head is None or not hmac.compare_digest(
                _seal_mac(self._seal_key, count, head), mac):
            self.healthy = False
            logger.error(
                "wallet audit ledger %s FAILED its seal (sealed rows: %d, present: %d) — "
                "a sealed row was edited, removed or reordered. The rolling-24h cap and "
                "the replay guard rebuild from this file, so spending is refused until "
                "the owner restores the ledger.", self._path, count, self._chain_n)
            return
        if self._chain_n > count:
            self._write_seal()

    def _load(self) -> None:
        chain_at = {0: _ZERO_CHAIN}
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        if not line.endswith("\n"):
                            self.healthy = False
                        line = line.strip()
                        if not line:
                            continue
                        self._extend_chain(line)
                        chain_at[self._chain_n] = self._chain
                        try:
                            list.append(self, json.loads(line))  # base append: no re-write
                        except json.JSONDecodeError:
                            self.healthy = False
                            continue  # skip a corrupt line, keep the rest
                try:
                    self._offset = os.path.getsize(self._path)
                except OSError:
                    self._offset = 0
            except (OSError, UnicodeError) as e:
                self.healthy = False
                logger.warning("wallet audit sink load failed (%s): %s", self._path, e)
        # Tamper check: a persisted high-water mark greater than what we recovered
        # means the JSONL lost entries since the last write (truncation / tamper).
        loaded = len(self)
        persisted = self._read_hwm()
        if persisted is not None and loaded < persisted:
            self.healthy = False
            logger.error(
                "wallet audit sink %s reloaded %d entries but %d were previously "
                "recorded — the audit log appears TRUNCATED/TAMPERED. The rolling-24h "
                "spend cap and payment-replay guard rebuild from this file, so they may "
                "have been reset; investigate before enabling spend.",
                self._path, loaded, persisted,
            )
        # Never let the mark regress (keep the evidence across further restarts).
        self._hwm = max(persisted or 0, loaded)
        self._verify_seal(chain_at)

    def append(self, entry: dict) -> None:  # type: ignore[override]
        with self._write_lock():
            # Refresh before our own write so advancing the byte offset cannot
            # skip a different process's spend.
            self.refresh()
            self._append_locked(entry)

    def _append_locked(self, entry: dict) -> None:
        list.append(self, entry)
        try:
            line = json.dumps(entry)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._extend_chain(line)
            # Consume our own write so `refresh()` never re-reads it as another
            # process's entry (which would double-count it against the cap).
            self._offset = os.path.getsize(self._path)
        except (OSError, UnicodeError) as e:
            self.healthy = False
            logger.warning("wallet audit sink write failed (%s): %s", self._path, e)
        self._hwm = max(self._hwm, len(self))
        self._write_hwm()
        self._write_seal()


def _wallet_data_dir(data_dir: Optional[str] = None, *, for_meta: bool = False) -> str:
    """The wallet's data home (``<data_dir>/wallet/``) — the single resolution
    shared by the audit sink and ``core.wallet.derivation`` so both write under
    the same directory.

    An explicit ``data_dir`` (tests, ``--data-dir``) always wins unchanged. With
    no override, this used to resolve a bare CWD-relative ``./data/wallet`` —
    money-critical bug (2026-07-14 final review, Finding 1): `polyrob wallet
    init` run from directory A and a later `polyrob run`/service start from
    directory B would resolve DIFFERENT files, so `resolve_scheme()` silently
    falls back to "legacy" and derives a DIFFERENT treasury address than the one
    the operator funded. Anchor instead to the SAME data home every other
    subsystem uses (goals.db/cron.db/memory.db): ``POLYROB_DATA_DIR`` if set,
    else ``core.runtime_paths.resolve_data_home()`` (``cwd/.polyrob`` in local
    mode — still CWD-based, but now consistent with the rest of the CLI rather
    than a second, undocumented CWD-relative root).

    L3 (2026-07-15): a relative ``POLYROB_DATA_DIR`` is ``.resolve()``d to an
    absolute path so it can't shift with the process CWD (which would re-split the
    meta/audit root and flip ``resolve_scheme`` to legacy). And ``for_meta=True``
    (the derivation meta read) FAILS CLOSED when the data-home resolution raises
    instead of silently using ``./data/wallet`` — a wrong meta path silently flips
    a funded bip44 wallet to legacy (different address). The audit path also fails
    closed: a fallback would silently reset spend accounting.
    """
    if data_dir is not None:
        return os.path.join(data_dir, "wallet")
    env_dir = (os.environ.get("POLYROB_DATA_DIR") or "").strip()
    if env_dir:
        # L3: absolutize so a relative dir is CWD-independent (meta & audit agree).
        try:
            env_dir = str(Path(env_dir).resolve())
        except Exception:
            env_dir = os.path.abspath(env_dir)
        return os.path.join(env_dir, "wallet")
    try:
        from core.runtime_paths import resolve_data_home  # lazy: core-tier only
        return str(resolve_data_home() / "wallet")
    except Exception as e:
        raise RuntimeError(
            "wallet data-home resolution failed and POLYROB_DATA_DIR is unset — "
            "refusing a fallback wallet path; set POLYROB_DATA_DIR"
        ) from e



def default_audit_sink(data_dir: Optional[str] = None, *,
                       lock_timeout: Optional[float] = None) -> List[dict]:
    """The factory's default persistent sink at ``<data_dir or resolved home>/wallet/audit.jsonl``.

    ``lock_timeout`` bounds every lock the sink takes, construction included
    (068 R3-4); None keeps the blocking behaviour every spend path relies on.
    """
    return JsonlAuditSink(os.path.join(_wallet_data_dir(data_dir), "audit.jsonl"),
                          lock_timeout=lock_timeout)
