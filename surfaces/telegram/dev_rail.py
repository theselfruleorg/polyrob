"""Owner ↔ dev-loop rail (proposal 027 WS-1).

`/dev <free text>` forwards the owner's message to the on-host Claude dev loop
(the tmux maintenance session): durably into the ops inbox file and, when the
loop is up, live into its tmux input. Bare `/dev` reports rail status.

This is host-side plumbing, not an agent feature: the verb routes as an
owner-admin COMMAND (it never reaches the agent) and delegates the
inbox/tmux mechanics to `scripts/dev_inject.sh`. Installs without that script
(the public package does not ship `scripts/`) get an honest "not available"
reply. `DEV_RAIL_SCRIPT` overrides the script path.

H08 — the hardened caller cannot reach the loop, so the message is parked on a
group-writable host spool that ROOT replays into the Claude dev session. That
replay only accepts a message carrying an HMAC minted HERE, in the owner-turn
path, with a key (``/etc/polyrob/dev_rail.key``) that only the agent identity
and root can read: ``#mac v1 <nonce> <epoch> <hex>`` over
``(nonce, epoch, stamp, message)``. The drainer (``scripts/dev_rail_drain.sh``)
rejects an unauthenticated, stale or replayed file. The inject script runs with a
SCRUBBED env (``build_child_env``), never the agent's full environment.
"""
import asyncio
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Dict, Optional

_TIMEOUT_SEC = 30

#: Readable only by the agent identity + root (deployment/hardening/
#: install-service-identities.sh). Module constant, not a flag: tests patch it.
DEV_RAIL_KEY_PATH = "/etc/polyrob/dev_rail.key"
_MAC_DOMAIN = b"polyrob-dev-rail-v1"

#: The host vars dev_inject.sh (tmux, git push, the spool) needs; everything
#: else — every provider key, the wallet seed, the bot token — stays behind.
_CHILD_ENV_ALLOW = (
    "MAINT_CLONE", "MAINT_SESSION", "INBOX_REL", "DEV_RAIL_SPOOL", "DEV_RAIL_STATE",
    "POLYROB_DATA_DIR", "TMUX", "TMUX_TMPDIR", "SSH_AUTH_SOCK", "GIT_SSH_COMMAND",
)


def strip_dev_prefix(text: str) -> str:
    """Return the raw free-text remainder after the /dev token.

    Preserves internal spacing and newlines — the loop should see exactly what
    the owner typed, not a re-joined token list.
    """
    t = (text or "").strip()
    if t.lower().startswith("/dev"):
        t = t[len("/dev"):]
    return t.strip()


def _inject_script() -> Optional[str]:
    override = (os.getenv("DEV_RAIL_SCRIPT") or "").strip()
    if override:
        return override if os.path.isfile(override) else None
    default = Path(__file__).resolve().parents[2] / "scripts" / "dev_inject.sh"
    return str(default) if default.is_file() else None


def _load_key() -> Optional[bytes]:
    try:
        fd = os.open(DEV_RAIL_KEY_PATH, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        key = os.read(fd, 4096).strip()
    finally:
        os.close(fd)
    return key if len(key) >= 32 else None


def mac_hex(key: bytes, nonce: str, epoch: str, stamp: str, msg: str) -> str:
    """The spool MAC — mirrored byte for byte by the drainer's verifier."""
    body = b"\x1f".join((_MAC_DOMAIN, nonce.encode(), epoch.encode(),
                         stamp.encode("utf-8"), msg.encode("utf-8")))
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def spool_auth_env(msg: str, *, now: Optional[float] = None) -> Dict[str, str]:
    """``DEV_RAIL_STAMP`` + ``DEV_RAIL_MAC`` for one owner message, or just the
    stamp when this process cannot read the key (the drainer then rejects a
    spooled copy; a live/inbox delivery does not need it)."""
    t = time.time() if now is None else float(now)
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))
    out = {"DEV_RAIL_STAMP": stamp}
    key = _load_key()
    if key is not None:
        nonce, epoch = secrets.token_hex(16), str(int(t))
        out["DEV_RAIL_MAC"] = f"v1 {nonce} {epoch} {mac_hex(key, nonce, epoch, stamp, msg)}"
    return out


def _child_env(extra: Dict[str, str]) -> Dict[str, str]:
    from tools.code_exec.env_policy import build_child_env
    return build_child_env(extra, extra_allowlist=_CHILD_ENV_ALLOW)


async def perform_dev_command(text: str) -> str:
    """Run the dev-rail script: deliver `text`, or `--status` when empty."""
    script = _inject_script()
    if script is None:
        return ("Dev rail not available on this install — scripts/dev_inject.sh "
                "not found (set DEV_RAIL_SCRIPT to enable).")
    msg = (text or "").strip()
    argv = [script, "--status"] if not msg else [script]
    env = _child_env(spool_auth_env(msg) if msg else {})
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(
            proc.communicate(input=msg.encode("utf-8", errors="replace")),
            _TIMEOUT_SEC)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "Dev rail timed out — the host side is stuck; check the loop by hand."
    except Exception as e:
        return f"Dev rail failed to run: {e}"

    if not msg:  # status query — relay the script's own line
        return out.decode(errors="replace").strip() or "Dev rail status: (no output)."
    if proc.returncode == 0:
        return "→ dev loop (live) — also queued durably in the ops inbox."
    if proc.returncode == 3:
        return "→ ops inbox (dev loop is down; the watchdog will revive it)."
    if proc.returncode == 5:
        # The hardened service identity (ProtectHome) can reach neither the
        # maintenance clone nor root's tmux socket; the message is parked on
        # the host spool and the root-side relay delivers it.
        return ("→ dev loop via the host relay (queued on the spool; delivered "
                "live within a minute and kept durably in the ops inbox).")
    tail = err.decode(errors="replace").strip()[-300:]
    return f"Dev rail error (exit {proc.returncode})" + (f": {tail}" if tail else ".")
