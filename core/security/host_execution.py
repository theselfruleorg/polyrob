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

The isolation covers the process memory only. 085 W1: when the key is ALSO in an
env file this UID can read (a local ``polyrob wallet init``), a host child can
open that file, so children that run MODEL-WRITTEN code (``run_tests``,
``run_code``, the host shell) refuse — :func:`model_code_host_refusal`. Git,
LSP, ``rg`` and MCP stdio stay allowed (owner decision D1).
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


#: 085 W1: the fix the readable-file gap names — the two ways out.
CUSTODY_FILE_REMEDY = ("move the seed to the signer (`polyrob signer install`) and remove it "
                       "from that file, or run code in a sandbox backend (CODE_EXEC_BACKEND=docker)")


#: 085 W1: the fix when the only readable copy is in an update snapshot.
SNAPSHOT_COPY_REMEDY = ("delete that old snapshot (`polyrob update --list-snapshots` lists "
                        "them) once you no longer need to roll back to it, or run code in a "
                        "sandbox backend (CODE_EXEC_BACKEND=docker)")


def custody_file_gap():
    """Why a host child could read a wallet key FROM A FILE now, or None (085 W1).

    The in-memory isolation (``custody_isolation_gap``) closes the process env and
    ``/proc``; it cannot close a dotenv file the same UID can open. A local
    ``polyrob wallet init`` writes the seed to ``~/.polyrob/.env`` (0600, same
    UID). The file decides, not what this process holds: a seed left in the file
    under the remote signer is a gap too. None for a seed only in the process
    env (systemd ``EnvironmentFile``) and for no seed in any env file this
    process layered or wrote. Never names the secret or the file."""
    found = _custody_file_finding()
    return found[0] if found else None


def _snapshot_copy_with_key():
    """The first env copy in an old ``polyrob update`` snapshot that this UID can
    read and that holds a wallet key, or None (085 W1). The snapshot is only
    read, never edited: a restore must keep the seed."""
    from core.runtime_paths import update_snapshot_config_files
    from core.security.custody_env import file_holds_custody_secret
    for path in update_snapshot_config_files():
        if file_holds_custody_secret(path):
            return str(path)
    return None


def _custody_file_finding():
    """``(reason, fix)`` for the readable-file gap, or None (085 W1)."""
    try:
        from core.security.custody_env import readable_custody_file
        if readable_custody_file() is None:
            if _snapshot_copy_with_key() is None:
                return None
            return ("an old `polyrob update` snapshot holds a wallet key in an env file "
                    "this user can read, so code that runs on the host could read it",
                    SNAPSHOT_COPY_REMEDY)
        return ("a wallet key is in an env file this user can read, so code that "
                "runs on the host could read that file", CUSTODY_FILE_REMEDY)
    except Exception as e:  # pragma: no cover - defensive
        return (f"the key file could not be checked ({type(e).__name__})", CUSTODY_FILE_REMEDY)


def model_code_host_refusal():
    """None when MODEL-WRITTEN code (``run_tests``, ``run_code``, the host shell)
    may run on the host now; else the refusal (085 W1, owner decision D1).

    :func:`host_execution_refusal` first, then :func:`custody_file_gap`. Git, LSP,
    ``rg``, MCP stdio, installs and snapshots keep :func:`host_execution_refusal`
    alone: they do not run code the model wrote, and their env is an allowlist."""
    refusal = host_execution_refusal()
    if refusal:
        return refusal
    found = _custody_file_finding()
    if found:
        gap, fix = found
        return f"host code execution refused under wallet custody: {gap}. Fix: {fix}"
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
      3. :func:`model_code_host_refusal` is None — key isolation holds, no
         readable env file holds the key (085 W1), and the owner's
         ``host_exec_under_custody`` row allows it;
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
        custody = model_code_host_refusal()
        if custody:
            return custody
        if not host_seat_allowed(execution_context):
            return ("the host shell is only for a turn typed at this machine's terminal "
                    "(polyrob / polyrob run); chat, API and console turns use the sandbox")
        return None
    except Exception as e:  # pragma: no cover - defensive
        return f"the host shell gate could not decide ({type(e).__name__}); refused"
