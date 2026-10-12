"""Host execution beside signing credentials (owner decision D1, 2026-10-10).

The agent uses the wallet AND keeps full host capabilities: git, MCP stdio, lazy
installs, coding snapshots/LSP, self_env and the host shell run under wallet
custody in every signer mode. The key is protected by ISOLATION, not by refusing
the tool: the seed leaves ``os.environ`` at load (``custody_env``), the process
is non-dumpable (``process_hardening``), and every host child gets an
allowlisted env with no custody secret.

Two things still refuse:

* the floor row ``key_isolation_gap`` — this process holds a key and the
  non-dumpable hardening did not hold, so a same-UID child could read it;
* the owner's ``SECURITY_SETTINGS`` row ``host_exec_under_custody=refuse``
  (``core.security.settings``).
"""
from core.env import bool_env
from core.security.custody_env import holds_custody_secret


def wallet_custody_enabled() -> bool:
    """Treat an enabled wallet or any supported signing secret as custody.

    Never construct a wallet or return credential contents just to decide
    whether model-controlled subprocesses may run on the host.

    066 P0.2: the seeds leave ``os.environ`` once the wallet config loads, so
    the secret half reads through :func:`holds_custody_secret` (env, then the held
    copy) — a loaded custody process never stops counting as custody.
    """
    if bool_env("AGENT_WALLET_ENABLED", False):
        return True
    return holds_custody_secret()


def custody_isolation_gap():
    """Why a same-UID host child could read this process's wallet key now, or None.

    None when this process holds no key (remote mode, a public-only unit) or the
    exec-time env is closed (``held`` on Linux, ``unsupported`` elsewhere). Never
    names the secret. Fail-closed: an error is a gap."""
    try:
        from core.security.custody_env import holds_custody_secret
        if not holds_custody_secret():
            return None
        from core.security import process_hardening as ph
        state = ph.hardening_state()
        if state.state == ph.STATE_NOT_APPLIED:
            state = ph.harden_custody_process()
        if state.state in (ph.STATE_HELD, ph.STATE_UNSUPPORTED):
            return None
        return ("this process holds a wallet key and a same-UID child could read it "
                f"({state.detail})")
    except Exception as e:  # pragma: no cover - defensive
        return f"key isolation could not be checked ({type(e).__name__})"


def host_execution_refusal():
    """None when a host child (git, MCP stdio, an install, LSP, a snapshot, the
    host shell) may run now; else the refusal. No custody and no remote signer:
    always None. Under custody: the isolation floor, then the owner's row."""
    from core.signer import MODE_REMOTE, signer_mode
    if signer_mode() != MODE_REMOTE and not wallet_custody_enabled():
        return None
    gap = custody_isolation_gap()
    if gap:
        return (f"host execution refused under wallet custody: {gap}. Restart the agent "
                "unit; its startup log names why the hardening failed")
    from core.security import settings
    if settings.value("host_exec_under_custody") == "refuse":
        return ("host execution refused under wallet custody or remote signing: "
                + settings.refusal_note("host_exec_under_custody")
                + "; use a sandbox backend")
    return None


#: 073 W1: the ONE seat that may run the host shell — a foreground terminal of
#: this process (`polyrob` REPL / `polyrob run`), stamped per turn as
#: ``metadata["seat"]`` from ``core.surfaces.binding.terminal_attached``. Telegram,
#: A2A, `/v1`, the console and rooms never carry it. (The console is NOT a local
#: seat even on loopback: behind the prod reverse proxy every request arrives
#: from 127.0.0.1, so a loopback test would admit the internet.)
HOST_SEAT = "terminal"


def host_seat_allowed(execution_context) -> bool:
    try:
        metadata = getattr(execution_context, "metadata", None) or {}
        return metadata.get("seat") == HOST_SEAT
    except Exception:
        return False


def host_shell_refusal(execution_context):
    """None when THIS turn may run the shell on the host (posture 3); else why not.

    All four must hold (each alone refuses — 073 §6):
      1. ``compute_posture_allows(ctx, 3)`` — posture 3, owner tenant, orchestrator,
         not a sub-agent, not a forged turn;
      2. ``POLYROB_LOCAL`` — a single-user box, never a server;
      3. :func:`host_execution_refusal` is None — key isolation holds and the
         owner's ``host_exec_under_custody`` row allows it;
      4. the turn came from a foreground terminal seat (:func:`host_seat_allowed`).
    Fail-closed: any fault refuses.
    """
    try:
        from core.config_policy import compute_posture, compute_posture_allows_safe, local_mode_enabled
        if compute_posture() < 3:
            return "the host shell needs AGENT_COMPUTE_POSTURE=3"
        if not compute_posture_allows_safe(execution_context, 3):
            return "the host shell is only for the owner's own, non-delegated turn"
        if not local_mode_enabled():
            return "the host shell needs POLYROB_LOCAL (a single-user machine, never a server)"
        custody = host_execution_refusal()
        if custody:
            return custody
        if not host_seat_allowed(execution_context):
            return ("the host shell is only for a turn typed at this machine's terminal "
                    "(polyrob / polyrob run); chat, API and console turns use the sandbox")
        return None
    except Exception as e:  # pragma: no cover - defensive
        return f"the host shell gate could not decide ({type(e).__name__}); refused"
