"""Durable, append-only audit sink for the wallet PolicyGate (G3).

An in-memory ``list`` of audit entries is lost on restart, so the harness can't sum
lifetime/rolling spend across a service restart — a mainnet prerequisite. This sink
is a ``list`` subclass that mirrors every appended entry to an append-only JSONL file
and reloads prior entries on construction, so a fresh ``PolicyGate(audit_sink=...)``
sees the full history. Default behavior is unchanged: PolicyGate uses a plain list
unless a sink is injected (only the factory does, when the wallet is enabled).

Fail-open: a file I/O error never blocks an action (the in-memory copy is always
authoritative for the live process); persistence is best-effort.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


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
    the evidence survives further restarts. Fail-open throughout — the sidecar is
    best-effort and never blocks a spend.
    """

    def __init__(self, path: str):
        super().__init__()
        self._path = path
        self._hwm_path = path + ".hwm"
        self._hwm = 0
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._load()

    def _read_hwm(self) -> Optional[int]:
        try:
            with open(self._hwm_path, "r", encoding="utf-8") as fh:
                return int((fh.read() or "0").strip())
        except (OSError, ValueError):
            return None

    def _write_hwm(self) -> None:
        try:
            with open(self._hwm_path, "w", encoding="utf-8") as fh:
                fh.write(str(self._hwm))
        except OSError as e:
            logger.warning("wallet audit high-water write failed (%s): %s", self._hwm_path, e)

    def _load(self) -> None:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            list.append(self, json.loads(line))  # base append: no re-write
                        except json.JSONDecodeError:
                            continue  # skip a corrupt line, keep the rest
            except OSError as e:
                logger.warning("wallet audit sink load failed (%s): %s", self._path, e)
        # Tamper check: a persisted high-water mark greater than what we recovered
        # means the JSONL lost entries since the last write (truncation / tamper).
        loaded = len(self)
        persisted = self._read_hwm()
        if persisted is not None and loaded < persisted:
            logger.error(
                "wallet audit sink %s reloaded %d entries but %d were previously "
                "recorded — the audit log appears TRUNCATED/TAMPERED. The rolling-24h "
                "spend cap and payment-replay guard rebuild from this file, so they may "
                "have been reset; investigate before enabling spend.",
                self._path, loaded, persisted,
            )
        # Never let the mark regress (keep the evidence across further restarts).
        self._hwm = max(persisted or 0, loaded)

    def append(self, entry: dict) -> None:  # type: ignore[override]
        list.append(self, entry)
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError as e:
            logger.warning("wallet audit sink write failed (%s): %s", self._path, e)
        self._hwm = max(self._hwm, len(self))
        self._write_hwm()


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
    a funded bip44 wallet to legacy (different address). The audit path stays
    fail-open (a lost audit resets caps, but never blocks a live spend).
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
        if for_meta:
            # Money-critical: never resolve a derivation-meta path to a CWD-relative
            # fallback — set POLYROB_DATA_DIR rather than risk a silent scheme flip.
            raise RuntimeError(
                f"wallet data-home resolution failed ({e}) and POLYROB_DATA_DIR is "
                f"unset — refusing a CWD-relative wallet meta path (would risk a silent "
                f"derivation-scheme flip / wrong funded address); set POLYROB_DATA_DIR"
            ) from e
        logger.error("wallet data-dir resolution failed, using legacy ./data: %s", e)
        return os.path.join("data", "wallet")


def default_audit_sink(data_dir: Optional[str] = None) -> List[dict]:
    """The factory's default persistent sink at ``<data_dir or resolved home>/wallet/audit.jsonl``."""
    return JsonlAuditSink(os.path.join(_wallet_data_dir(data_dir), "audit.jsonl"))
