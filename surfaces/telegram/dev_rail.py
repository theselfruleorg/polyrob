"""Owner ↔ dev-loop rail (proposal 027 WS-1).

`/dev <free text>` forwards the owner's message to the on-host Claude dev loop
(the tmux maintenance session): durably into the ops inbox file and, when the
loop is up, live into its tmux input. Bare `/dev` reports rail status.

This is host-side plumbing, not an agent feature: the verb routes as an
owner-admin COMMAND (it never reaches the agent) and delegates the
inbox/tmux mechanics to `scripts/dev_inject.sh`. Installs without that script
(the public package does not ship `scripts/`) get an honest "not available"
reply. `DEV_RAIL_SCRIPT` overrides the script path.
"""
import asyncio
import os
from pathlib import Path
from typing import Optional

_TIMEOUT_SEC = 30


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


async def perform_dev_command(text: str) -> str:
    """Run the dev-rail script: deliver `text`, or `--status` when empty."""
    script = _inject_script()
    if script is None:
        return ("Dev rail not available on this install — scripts/dev_inject.sh "
                "not found (set DEV_RAIL_SCRIPT to enable).")
    msg = (text or "").strip()
    argv = [script, "--status"] if not msg else [script]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
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
    tail = err.decode(errors="replace").strip()[-300:]
    return f"Dev rail error (exit {proc.returncode})" + (f": {tail}" if tail else ".")
