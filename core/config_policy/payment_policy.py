"""Payment-approval policy: the import-frozen ``PAYMENT_APPROVAL_MODE`` / timeout / grant-TTL
snapshots and their single refreeze seam.

Split out of ``core/config_policy/policy.py`` (S2, 2026-08-29); ``policy.py`` re-exports
every name so existing importers are unaffected. Import order (a DAG): _env -> local_profile
-> autonomy_mode -> autonomy_posture -> compute_posture -> payment_policy -> capability_toggles
-> autonomy_config -> runtime_gates.
"""

import os
from core.config_policy._env import _float_env
from core.config_policy.autonomy_mode import full_autonomy_enabled


# The payment-approval action-name lanes (RECEIVE vs SPEND) are pure data and
# live in payment_tools.py (god-file ratchet); re-imported here so every
# existing importer of this module keeps working.
from core.config_policy.payment_tools import (  # noqa: F401,E402
    PAYMENT_APPROVAL_TOOLS,
    PAYMENT_RECEIVE_APPROVAL_TOOLS,
    VERB_OWNED_APPROVAL_GATES,
)


# fix pass 1 (Finding 2): the payment-approval flags are FROZEN AT IMPORT — snapshotted
# once, exactly like APPROVAL_REQUIRED_TOOLS/APPROVAL_PROVIDER are in
# `tools/controller/approval.py` (WS-7) — so a mid-process env mutation (e.g. a
# prompt-injected write to the process env, or a config-reload race) can never retarget
# money-critical gating (queue-vs-auto mode, the owner-response wait, or the one-shot
# grant TTL) mid-session. Operators set these in real process env at startup.
#
# 013 T7: the default (unset/invalid PAYMENT_APPROVAL_MODE) is MODE-DEPENDENT —
# supervised (default) keeps "approve" (byte-identical); under full autonomous mode
# it defaults to "auto", because the "approve" -> owner_queue path hard-denies
# forged/autonomous turns (`OwnerQueueApprover`, tools/controller/approval_queue.py),
# making autonomous invoicing impossible under "approve". "auto" still executes only
# within X402_INVOICE_MAX_USD/X402_INVOICE_DAILY_MAX and fires a post-hoc owner
# notify for every within-cap creation of a RECEIVE-side verb
# (PAYMENT_RECEIVE_APPROVAL_TOOLS). An explicit PAYMENT_APPROVAL_MODE always wins
# in both modes.
#
# T7 review (Important finding fix): PAYMENT_APPROVAL_TOOLS also holds SPEND-side
# live-trade order verbs (hyperliquid/polymarket, L9) — mode="auto" does NOT
# loosen pre-approval for those. Every entry in PAYMENT_APPROVAL_TOOLS that is
# NOT in PAYMENT_RECEIVE_APPROVAL_TOOLS is SPEND-side and keeps owner_queue
# pre-approval under BOTH "approve" and "auto" (see
# tools/controller/service.py::Controller.__init__ payment-approval wiring). This
# protection is unconditional — it also applies under an EXPLICIT
# PAYMENT_APPROVAL_MODE=auto, which is a deliberate behavior change vs the prior
# (T7-only) cut for any deployment that had set PAYMENT_APPROVAL_MODE=auto
# explicitly: previously that setting also act-and-reported trade verbs; it no
# longer does. The hard product line (proposal 013): money-SPEND/trading stays
# deny-by-default / owner-in-the-loop and is never act-and-report, in any mode.
def _snapshot_payment_approval_mode() -> str:
    raw = (os.getenv("PAYMENT_APPROVAL_MODE") or "").strip().lower()
    if raw in ("approve", "auto"):
        return raw
    return "auto" if full_autonomy_enabled() else "approve"


def _snapshot_payment_approval_timeout_sec() -> float:
    return _float_env("APPROVAL_TIMEOUT_SEC", 300.0)


def _snapshot_approval_grant_ttl_hours() -> float:
    return _float_env("APPROVAL_GRANT_TTL_HOURS", 24.0)


_FROZEN_PAYMENT_APPROVAL_MODE = _snapshot_payment_approval_mode()


_FROZEN_PAYMENT_APPROVAL_TIMEOUT_SEC = _snapshot_payment_approval_timeout_sec()


_FROZEN_APPROVAL_GRANT_TTL_HOURS = _snapshot_approval_grant_ttl_hours()


def payment_approval_mode() -> str:
    """PAYMENT_APPROVAL_MODE — the owner-legible switch for outward payment requests
    (`tools/controller/service.py` wires this against :data:`PAYMENT_APPROVAL_TOOLS`,
    split into :data:`PAYMENT_RECEIVE_APPROVAL_TOOLS` vs the SPEND-side remainder):

      - ``"approve"``: every request (receive AND spend) queues through the durable,
        remote-capable ``owner_queue`` provider (`tools/controller/approval_queue.py`)
        — a real owner tap that works over Telegram even though prod Rob is headless
        (closes G-2: the only approver used to be a blocking stdin prompt).
      - ``"auto"``: the RECEIVE-side subset (``x402_request``) is NOT queued — it
        executes immediately (still bounded by `modules/x402/invoicing.py`'s own
        per-invoice/per-day caps, never duplicated here), and a post-execution owner
        notification + audit event fires for every within-cap creation. The SPEND-side
        subset (live-trade order verbs) is UNAFFECTED by this mode — it still queues
        through ``owner_queue`` exactly as under ``"approve"``, because trading is
        never act-and-report (see :data:`PAYMENT_RECEIVE_APPROVAL_TOOLS`).

    An explicit env value (typo aside) always wins. When ``PAYMENT_APPROVAL_MODE`` is
    unset (or an invalid value), the DEFAULT is mode-dependent (013 T7): ``"approve"``
    under supervised mode (byte-identical to pre-013), ``"auto"`` under full autonomous
    mode (`full_autonomy_enabled()`) — because ``"approve"``'s owner_queue path
    hard-denies forged/autonomous turns, which would otherwise make receive-side
    invoicing impossible for a single-owner autonomous instance. Regardless of default
    vs explicit, ``"auto"`` only ever act-and-reports the RECEIVE-side subset — money-
    SPEND (x402_pay, wallet, trading order-placement) always keeps owner-in-the-loop
    pre-approval, in EVERY mode (013 T7 review fix, closing an Important finding: the
    original T7 cut let an explicit or mode-defaulted ``"auto"`` silently drop
    pre-approval for the live-trade verbs too).

    FROZEN AT IMPORT (fix pass 1 / Finding 2) — see :func:`_refreeze_payment_approval_flags_for_tests`
    for the test-only re-snapshot seam.
    """
    return _FROZEN_PAYMENT_APPROVAL_MODE


def payment_approval_timeout_sec() -> float:
    """``APPROVAL_TIMEOUT_SEC`` for payment-creation actions specifically — reuses
    the SAME env var as the generic approval seam
    (`tools/controller/approval.py::DEFAULT_APPROVAL_TIMEOUT_SEC`, default 30s), but
    an owner_queue wait defaults to a money-appropriate **300s**: a real owner
    round-trip over Telegram needs minutes, not seconds, and 30s would time out
    almost every legitimate approval. An operator who explicitly sets
    ``APPROVAL_TIMEOUT_SEC`` still wins for BOTH the generic and the payment seam —
    no new flag.

    FROZEN AT IMPORT (fix pass 1 / Finding 2) — see :func:`_refreeze_payment_approval_flags_for_tests`.
    """
    return _FROZEN_PAYMENT_APPROVAL_TIMEOUT_SEC


def approval_grant_ttl_hours() -> float:
    """TTL (hours) for a ``owner_queue`` ONE-SHOT grant: an owner decision recorded
    AFTER the requester already timed out and gave up still lets the NEXT identical
    request (same tool + params + tenant) through without re-queuing — but only within
    this window, so a decision from days ago can't silently auto-approve a fresh replay.

    FROZEN AT IMPORT (fix pass 1 / Finding 2) — see :func:`_refreeze_payment_approval_flags_for_tests`.
    """
    return _FROZEN_APPROVAL_GRANT_TTL_HOURS


def _refreeze_payment_approval_flags() -> None:
    """Re-snapshot the payment-approval flags from the current env (026 P1.1).

    TWO legitimate callers, nothing else: ``core.bootstrap.load_env`` (EXACTLY
    ONCE per process, after env-file layering, before any container/agent — so
    an owner-controlled ``.polyrob/.env`` value reaches the freeze; the
    once-guard lives in bootstrap) and tests (after a monkeypatch + in
    teardown). The security property is unchanged: a mid-session env mutation
    still cannot move money-critical gating.
    """
    global _FROZEN_PAYMENT_APPROVAL_MODE, _FROZEN_PAYMENT_APPROVAL_TIMEOUT_SEC, \
        _FROZEN_APPROVAL_GRANT_TTL_HOURS
    _FROZEN_PAYMENT_APPROVAL_MODE = _snapshot_payment_approval_mode()
    _FROZEN_PAYMENT_APPROVAL_TIMEOUT_SEC = _snapshot_payment_approval_timeout_sec()
    _FROZEN_APPROVAL_GRANT_TTL_HOURS = _snapshot_approval_grant_ttl_hours()


#: Back-compat alias — existing tests import the ``_for_tests`` name.
_refreeze_payment_approval_flags_for_tests = _refreeze_payment_approval_flags
