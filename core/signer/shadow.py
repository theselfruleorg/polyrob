"""``WALLET_SIGNER=shadow`` — sign locally, ask the signer, log the difference.

:class:`ShadowEvmSigner` wraps the agent's ``LocalEoaSigner``. Before every
local ``sign_transaction`` it asks ``polyrob-signer`` for an ``evm.verdict`` on
the SAME intent and transaction, then signs locally as today — whatever the
signer said, and whether or not it answered. Shadow never blocks a send.

Every comparison is one line of ``<wallet dir>/signer_shadow.jsonl``:

* ``agree`` — the signer would have signed it too;
* ``disagree`` — the signer would have refused (its code + reason);
* ``unreachable`` — no answer (the signer is down or slow);
* ``no_intent`` — the transaction reached the signing point without a recorded
  ``tx_guard`` authorization (a remote signer would refuse it).

The custody status section counts them over the last 7 days. The cut-over
criterion (066 §5.5) is a week with zero ``disagree``/``no_intent`` and a
non-zero ``agree`` count.
"""
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

KINDS = ("agree", "disagree", "unreachable", "no_intent")
_MAX_BYTES = 5 * 1024 * 1024
_VERDICT_TIMEOUT_SEC = 20.0
_lock = threading.Lock()


def shadow_log_path() -> str:
    from core.wallet.audit_sink import _wallet_data_dir
    return os.path.join(_wallet_data_dir(for_meta=True), "signer_shadow.jsonl")


def log_comparison(kind: str, **fields) -> None:
    """Append one comparison. Fail-open: shadow must never break a send."""
    entry = {"ts": time.time(), "kind": kind}
    entry.update({k: v for k, v in fields.items() if v is not None})
    try:
        path = shadow_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _lock:
            try:
                if os.path.getsize(path) > _MAX_BYTES:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    except Exception:
        logger.warning("signer shadow log write failed", exc_info=True)
    if kind in ("disagree", "no_intent"):
        logger.warning("signer shadow %s: %s", kind.upper(), fields.get("reason") or fields)


def summary(days: float = 7.0, *, now: Optional[float] = None) -> Dict[str, Any]:
    """Counts per kind over the window, the last disagreement, and whether the
    log could be read at all (an unreadable log is NOT an empty one)."""
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    counts = {k: 0 for k in KINDS}
    last: Optional[Dict[str, Any]] = None
    first_ts: Optional[float] = None
    try:
        path = shadow_log_path()
    except Exception as exc:
        return {"readable": False, "error": f"{type(exc).__name__}: {exc}", "counts": counts}
    paths = [p for p in (path + ".1", path) if os.path.exists(p)]
    try:
        for p in paths:
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    ts = float(e.get("ts") or 0)
                    if ts < cutoff:
                        continue
                    kind = e.get("kind")
                    if kind in counts:
                        counts[kind] += 1
                        first_ts = ts if first_ts is None else min(first_ts, ts)
                        if kind in ("disagree", "no_intent"):
                            last = e
    except OSError as exc:
        return {"readable": False, "error": f"{type(exc).__name__}: {exc}", "counts": counts}
    return {"readable": True, "counts": counts, "last_disagreement": last,
            "since": first_ts, "days": days}


class ShadowEvmSigner:
    """A ``LocalEoaSigner`` that also asks the signer. Never blocks."""

    def __init__(self, local, client=None):
        self._local = local
        self._client = client

    def _get_client(self):
        if self._client is None:
            from core.signer.client import SignerClient
            self._client = SignerClient(timeout=_VERDICT_TIMEOUT_SEC)
        return self._client

    @property
    def address(self) -> str:
        return self._local.address

    @property
    def account(self):
        return self._local.account

    def sign_message(self, data):
        return self._local.sign_message(data)

    def sign_typed_data(self, domain, types, message):
        return self._local.sign_typed_data(domain, types, message)

    def compare(self, tx: Dict[str, Any]) -> str:
        """Ask for a verdict on *tx* and record the comparison. Returns the kind."""
        from core.signer import attest, protocol
        intent = attest.take(tx)
        digest = None
        try:
            digest = protocol.tx_digest(tx)
        except Exception:
            pass
        if intent is None:
            log_comparison("no_intent", digest=digest,
                   reason="signed locally with no recorded tx_guard authorization")
            return "no_intent"
        try:
            verdict = self._get_client().call(
                "evm.verdict", {"intent": protocol.intent_to_wire(intent),
                                "tx": protocol.tx_to_wire(tx)})
        except Exception as exc:
            from core.signer.client import SignerRefused
            if isinstance(exc, SignerRefused):
                log_comparison("disagree", digest=digest, chain=getattr(intent, "chain", None),
                       code=exc.code, reason=exc.reason,
                       amount_usd=exc.extra.get("amount_usd"))
                return "disagree"
            log_comparison("unreachable", digest=digest, reason=f"{type(exc).__name__}: {exc}")
            return "unreachable"
        log_comparison("agree", digest=digest, chain=getattr(intent, "chain", None),
               amount_usd=verdict.get("amount_usd"), lane=verdict.get("lane"))
        return "agree"

    def sign_transaction(self, tx: Dict[str, Any]) -> bytes:
        try:
            self.compare(tx)
        except Exception:  # noqa: BLE001 — shadow never blocks a send
            logger.warning("signer shadow comparison failed", exc_info=True)
        return self._local.sign_transaction(tx)

    def __repr__(self) -> str:
        return f"<ShadowEvmSigner {self._local!r}>"


def shadow_wallet_class():
    """``AgentWallet`` whose EVM signers are :class:`ShadowEvmSigner`s.

    Built lazily so ``core.wallet.agent_wallet`` never imports this package."""
    from core.wallet.agent_wallet import AgentWallet

    class ShadowWallet(AgentWallet):
        """The local wallet, unchanged, with every EVM send compared (066 shadow)."""

        def signer_for(self, venue: str):
            local = super().signer_for(venue)
            key = "__shadow:" + venue
            wrapped = self._signers.get(key)
            if wrapped is None:
                wrapped = ShadowEvmSigner(local)
                self._signers[key] = wrapped
            return wrapped

    return ShadowWallet


#: 066 §5.5: remote only after a week of shadow with agreements and no
#: disagreement. 6.5 days of data, so a week that started mid-day counts.
CUTOVER_MIN_DAYS = 6.5


def cutover_ready(*, now: Optional[float] = None):
    """``(ok, why, counts)`` — the cut-over criterion, read from the shadow log."""
    now = time.time() if now is None else now
    st = summary(7.0, now=now)
    counts = st.get("counts") or {}
    if not st.get("readable"):
        return False, f"shadow log unreadable ({st.get('error')})", counts
    if counts.get("disagree") or counts.get("no_intent"):
        return False, "disagreements in the last 7 days", counts
    if not counts.get("agree"):
        return False, "no agreed send in the last 7 days (nothing was compared)", counts
    since = st.get("since")
    if since is None or since > now - CUTOVER_MIN_DAYS * 86400:
        return False, f"less than {CUTOVER_MIN_DAYS} days of shadow data", counts
    return True, "a clean shadow week", counts


def signer_verdict_summary(state_dir: str, days: float = 7.0, *,
                           now: Optional[float] = None) -> Dict[str, Any]:
    """WAL-19: the shadow verdicts as the SIGNER recorded them.

    ``signer_shadow.jsonl`` lives in the agent's data home, so any agent-UID
    process can rewrite it into a clean week. The signer logs every
    ``evm.verdict`` in its own ``<state_dir>/signer.sqlite`` (0600, owned by
    ``polyrob-signer`` in a 0700 directory) — that is the record the cut-over
    trusts. Opened read-only; a missing or unreadable store is reported as
    unreadable, never as an empty week.
    """
    import sqlite3
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    counts = {"agree": 0, "disagree": 0}
    path = os.path.join(str(state_dir), "signer.sqlite")
    if not os.path.isfile(path):
        return {"readable": False, "error": f"{path} not found", "counts": counts}
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            rows = db.execute(
                "SELECT allowed, COUNT(*), MIN(ts) FROM decisions "
                "WHERE op = 'evm.verdict' AND ts >= ? GROUP BY allowed", (cutoff,)).fetchall()
        finally:
            db.close()
    except sqlite3.Error as exc:
        return {"readable": False, "error": f"{type(exc).__name__}: {exc}", "counts": counts}
    first_ts: Optional[float] = None
    for allowed, n, first in rows:
        counts["agree" if allowed else "disagree"] += int(n)
        if first is not None:
            first_ts = float(first) if first_ts is None else min(first_ts, float(first))
    return {"readable": True, "counts": counts, "since": first_ts, "days": days}


def signer_cutover_ready(state_dir: str, *, now: Optional[float] = None):
    """``(ok, why, counts)`` — the cut-over criterion from the signer's own log.

    The install script requires BOTH this and :func:`cutover_ready`: the agent
    log alone sees ``no_intent`` sends (they never reach the signer), but only
    the signer's log cannot be forged by the agent UID."""
    now = time.time() if now is None else now
    st = signer_verdict_summary(state_dir, 7.0, now=now)
    counts = st.get("counts") or {}
    if not st.get("readable"):
        return False, f"signer decision log unreadable ({st.get('error')})", counts
    if counts.get("disagree"):
        return False, "the signer refused a shadow verdict in the last 7 days", counts
    if not counts.get("agree"):
        return False, "the signer recorded no agreed verdict in the last 7 days", counts
    since = st.get("since")
    if since is None or since > now - CUTOVER_MIN_DAYS * 86400:
        return False, f"less than {CUTOVER_MIN_DAYS} days of signer-side shadow data", counts
    return True, "a clean shadow week in the signer's own log", counts
