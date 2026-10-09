"""Host execution cannot share a trust domain with signing credentials."""
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


def signer_holds_the_keys() -> bool:
    """066 §5.6: True only when ``WALLET_SIGNER=remote`` is ACTIVE AND VERIFIED.

    Active = the flag says ``remote``. Verified = this process built its
    ``RemoteWallet``, got the signer's identity and it matched the wallet of
    record, AND this process holds no custody secret (env or held copy). Any
    doubt answers False, which keeps the custody refusal.
    """
    try:
        from core.signer import MODE_REMOTE, signer_mode
        if signer_mode() != MODE_REMOTE:
            return False
        from core.security.custody_env import holds_custody_secret
        if holds_custody_secret():
            return False
        from core.signer.remote import remote_verified
        return remote_verified()
    except Exception:
        return False


def host_execution_refusal():
    # The signer authenticates the peer UID. A host child shares that UID and
    # can forge approval provenance over the socket even without holding a key.
    from core.signer import MODE_REMOTE, signer_mode
    if signer_mode() == MODE_REMOTE:
        return ("host execution refused while remote wallet signing is available; "
                "use a sandbox without access to the signer socket")
    if wallet_custody_enabled():
        return ("host execution refused while wallet custody is enabled; "
                "use a sandbox backend or a separate development instance without signing credentials")
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
      3. :func:`host_execution_refusal` is None — no signing key in this process;
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
