"""``AGENT_COMPUTE_POSTURE`` — the frozen-at-import compute-capability ladder and the ONE gate
predicate ``compute_posture_allows``.

Split out of ``core/config_policy/policy.py`` (S2, 2026-08-29); ``policy.py`` re-exports
every name so existing importers are unaffected. Import order (a DAG): _env -> local_profile
-> autonomy_mode -> autonomy_posture -> compute_posture -> payment_policy -> capability_toggles
-> autonomy_config -> runtime_gates.
"""

import os
from core.config_policy.local_profile import local_mode_enabled


# --- AGENT_COMPUTE_POSTURE — the compute-capability ladder (computer-use parity) ----
#
# A THIRD capability axis, orthogonal to POLYROB_LOCAL (single-user trust profile)
# and AUTONOMY_POSTURE (which autonomy loops run): how much host/compute capability
# the agent has.
#
#   0  confined      (default) — today's docker sandbox, no persistent shell.
#   1  sandbox-dev   — persistent networked sandbox + importable pip installs +
#                      `shell` scoped INTO the container + loopback port-publish.
#   2  self-maintain — posture 1 + the approval-gated `self_env` verbs.
#   3  host          — full host access; requires POLYROB_LOCAL and a
#                      single-tenant box (refused on network-facing surfaces).
#
# SECURITY CONTRACT:
# - Default CLOSED: unset/garbage/out-of-range -> 0. Garbage NEVER rounds up —
#   only the literal values 0|1|2|3 are accepted (a typo'd "9" must not grant the
#   host tier).
# - FROZEN AT IMPORT (re-frozen ONCE by `core.bootstrap.load_env` after
#   env-file layering, 026 P1.1): a mid-process env mutation (e.g. a
#   prompt-injected write that reached an env-mutating surface) can never
#   raise the running posture. Operators set it in real process env (systemd
#   EnvironmentFile / shell) or the owner-controlled `.polyrob/.env` ladder
#   loaded at process start — never at runtime.
# - The posture is the "may" side only; the existing correspondent-taint gate and
#   delegation blocklist stay the "never" side. `compute_posture_allows` is the
#   ONE predicate every posture-gated capability must call.

def _resolve_compute_posture(raw) -> int:
    """PURE: parse one env value -> posture int. Only literal 0|1|2|3 accepted;
    anything else (unset/garbage/out-of-range) degrades CLOSED to 0."""
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return val if val in (0, 1, 2, 3) else 0


_COMPUTE_POSTURE_FROZEN = _resolve_compute_posture(os.getenv("AGENT_COMPUTE_POSTURE"))


def compute_posture() -> int:
    """Resolved AGENT_COMPUTE_POSTURE (0-3), FROZEN at import (see block comment)."""
    return _COMPUTE_POSTURE_FROZEN


def _refreeze_compute_posture() -> int:
    """Re-snapshot the frozen posture from the current env (026 P1.1).

    Callers: ``core.bootstrap.load_env`` (once per process, after env-file
    layering, before any agent code — see the payment twin above) and tests.
    A mid-session env mutation still cannot raise the running posture.
    """
    global _COMPUTE_POSTURE_FROZEN
    _COMPUTE_POSTURE_FROZEN = _resolve_compute_posture(os.getenv("AGENT_COMPUTE_POSTURE"))
    return _COMPUTE_POSTURE_FROZEN


#: Back-compat alias — existing tests import the ``_for_tests`` name.
_refreeze_compute_posture_for_tests = _refreeze_compute_posture


def compute_posture_allows(execution_context, min_posture: int) -> bool:
    """THE single gate predicate for posture-gated compute capabilities.

    True only when ALL hold:
      (a) frozen ``compute_posture() >= min_posture``;
      (b) the session tenant is the OWNER — ``is_owner_local_safe`` (principal
          match always wins; the POLYROB_LOCAL bypass is honored ONLY for the
          CLI's ``local`` operator tenant, so a forgeable network sender under
          POLYROB_LOCAL=1 on a public surface is never auto-owned);
      (c) not a leaf/sub-agent context;
      (d) not a forged self-wake/delegation-result re-entry turn (the
          ``metadata["turn_kind"]`` stamp, SK-F10).

    An autonomous goal/cron session of the owner tenant PASSES (no forged stamp;
    role is orchestrator) — deliberate: WS-8 provisions those runs with the
    compute toolset, and the correspondent-taint gate + delegation blocklist
    remain the independent "never" side. ``min_posture <= 0`` is the
    unconditional baseline (posture-0 capabilities keep their own gates).
    Fail-CLOSED: any fault in resolution denies.
    """
    if min_posture <= 0:
        return True
    try:
        if compute_posture() < min_posture:
            return False
        if execution_context is None:
            return False
        if getattr(execution_context, "is_sub_agent", False):
            return False
        if getattr(execution_context, "role", "leaf") != "orchestrator":
            return False
        metadata = getattr(execution_context, "metadata", None) or {}
        # R-4: the forged-turn kinds SSOT is core-tier now — no fallback needed.
        from core.security.forged_turns import FORGED_TURN_KINDS as _kinds
        if metadata.get("turn_kind") in _kinds:
            return False
        from core.instance import is_owner_local_safe, resolve_owner_principal
        return is_owner_local_safe(
            getattr(execution_context, "user_id", None),
            owner_principal=resolve_owner_principal(),
            local_enabled=local_mode_enabled(),
        )
    except Exception:
        return False  # fail-closed: can't prove entitlement -> deny


def compute_posture_allows_safe(execution_context, min_posture: int) -> bool:
    """:func:`compute_posture_allows`, fail-CLOSED: any fault in the probe is a
    refusal. The per-call gate every host-capability tool (shell, process,
    code_exec dev mode, self_env) wraps the predicate in."""
    try:
        return bool(compute_posture_allows(execution_context, min_posture))
    except Exception:
        return False
