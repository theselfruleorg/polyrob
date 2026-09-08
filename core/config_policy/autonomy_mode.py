"""``AUTONOMY_MODE`` — the capability/approval master switch (proposal 013) and its single-owner
clamp.

Split out of ``core/config_policy/policy.py`` (S2, 2026-08-29); ``policy.py`` re-exports
every name so existing importers are unaffected. Import order (a DAG): _env -> local_profile
-> autonomy_mode -> autonomy_posture -> compute_posture -> payment_policy -> capability_toggles
-> autonomy_config -> runtime_gates.
"""

import logging
import os
from core.config_policy.local_profile import local_mode_enabled


# --- AUTONOMY_MODE — the capability/approval master switch (proposal 013) -----------
#
# A FOURTH axis, reconciled with the existing three: POLYROB_LOCAL = trust profile,
# AUTONOMY_POSTURE = which autonomy loops run (its DEFAULT is raised to `full` by this
# mode), AGENT_COMPUTE_POSTURE = host capability (NOT touched by this mode).
#
#   supervised (default) — today's behavior, byte-identical. Deny-by-default gates.
#   autonomous           — single-owner act-and-report instance: capability flags below
#                          default ON, autonomous toolset defaults to the full non-money
#                          set, approvals default to allow+audit+notify, outbound
#                          defaults to policy `open` with caps. Money-SPEND, secrets and
#                          host access are NEVER moved by this mode.
#
# Activation is guarded: `autonomous` is only effective on a single-owner deployment
# (local mode + a bound owner principal); otherwise it clamps to supervised with a
# one-time WARN, so a multi-tenant server can never drift into it.
_AUTONOMY_MODES = ("supervised", "autonomous")


_MODE_CAPABILITY_FLAGS = frozenset({
    "TWITTER_ENABLED",
    "MCP_ENABLED",
    "GROUP_CHAT_ENABLED",
    "EMAIL_SURFACE_ENABLED",
    "X402_INVOICE_ENABLED",           # RECEIVE side only; x402_pay/wallet stay OFF
    "X402_SETTLE_ONCHAIN_DETECT",     # RECEIVE side (W1.5): scan-only, moves no money
    "MESSAGE_AUTONOMOUS_ALLOWLISTED",
    "CORRESPONDENT_ACCESS_ENABLED",   # substrate for outbound-policy rails (T5/T6)
    "CORRESPONDENT_REPLY_ENABLED",
})


_FULL_AUTONOMY_WARNED = False


def autonomy_mode() -> str:
    """Resolved AUTONOMY_MODE (supervised|autonomous). Default `supervised` —
    byte-identical to pre-013 behavior. Unknown values degrade to supervised so a
    typo never activates the capable posture. Access-time (tests/bootstrap see env)."""
    raw = (os.getenv("AUTONOMY_MODE") or "").strip().lower()
    return raw if raw in _AUTONOMY_MODES else "supervised"


def full_autonomy_clamp_reason():
    """Why an ``AUTONOMY_MODE=autonomous`` request would clamp on THIS
    deployment (None = the single-owner guard would grant it), independent of
    whether the mode is set — the write-path clamp echo (026 P1.6) calls this
    at `config set` time so the owner hears about the clamp when they write."""
    if not local_mode_enabled():
        return "POLYROB_LOCAL is not set (multi-tenant/server deployment)"
    try:
        from core.instance import (
            resolve_owner_email,
            resolve_owner_principal,
            resolve_owner_telegram_id,
        )
        owner_bound = (
            resolve_owner_principal(default_to_instance=False) is not None
            or bool(resolve_owner_telegram_id())
            or bool(resolve_owner_email())
        )
        if not owner_bound:
            return "no owner principal is bound (POLYROB_OWNER_USER_ID/…)"
    except Exception as e:  # never let the guard itself crash a resolver
        return f"owner resolution failed: {e}"
    return None


def full_autonomy_enabled() -> bool:
    """True only when the operator set AUTONOMY_MODE=autonomous AND this deployment is
    a single-owner instance (local mode + a bound owner principal). Anything else
    clamps to supervised semantics with a one-time WARN — multi-tenant conservatism is
    by construction, not convention."""
    global _FULL_AUTONOMY_WARNED
    if autonomy_mode() != "autonomous":
        return False
    reason = full_autonomy_clamp_reason()
    if reason:
        if not _FULL_AUTONOMY_WARNED:
            logging.getLogger(__name__).warning(
                "AUTONOMY_MODE=autonomous requested but %s — clamping to supervised", reason)
            _FULL_AUTONOMY_WARNED = True
        return False
    return True


def autonomy_mode_display() -> str:
    """One-line human display of the resolved autonomy mode (T10 control-plane
    visibility — Telegram `/status`, `polyrob owner show`, `polyrob doctor`).

    Three exact strings:
      - ``"supervised"`` — AUTONOMY_MODE unset/supervised (today's default).
      - ``"autonomous (effective)"`` — AUTONOMY_MODE=autonomous AND the
        single-owner guard actually granted it (:func:`full_autonomy_enabled`).
      - ``"autonomous (clamped — needs POLYROB_LOCAL + owner binding)"`` — the
        operator requested autonomous but the guard clamped it back to
        supervised (multi-tenant deployment or no bound owner).
    """
    if autonomy_mode() != "autonomous":
        return "supervised"
    if full_autonomy_enabled():
        return "autonomous (effective)"
    return "autonomous (clamped — needs POLYROB_LOCAL + owner binding)"


def _mode_capability_default(flag_name: str) -> bool:
    """Default for a mode-governed capability flag: ON under effective autonomous
    mode, else OFF. Explicit per-flag env ALWAYS wins at the call site (this is only
    the default argument to _bool_env/bool_env)."""
    return full_autonomy_enabled() and flag_name in _MODE_CAPABILITY_FLAGS


def reset_autonomy_mode_warnings() -> None:
    """TEST-ONLY seam: clear the one-time full-autonomy clamp warning so the next
    ``full_autonomy_enabled()`` call re-evaluates (and may re-warn) from scratch.

    Replaces the previous ``monkeypatch.setattr(constants, "_FULL_AUTONOMY_WARNED", False)``
    poke, which no longer reaches this module's global after the WS-1 relocation. Production
    code never calls this.
    """
    global _FULL_AUTONOMY_WARNED
    _FULL_AUTONOMY_WARNED = False
