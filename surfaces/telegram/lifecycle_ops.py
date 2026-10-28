"""``/restart`` and ``/update`` — the owner restarts or updates the agent from chat (077).

Both quote first and act on a trailing ``go``; every seat turns the quote into
an action card (``core.surfaces.cards`` — both verbs are confirmable even when
typed bare). The confirm writes ONE request (``core.lifecycle``); the process
that runs the chat surfaces drains for it (finishes the live turn, goal run or
cron job), exits with 75 and is started again by its supervisor, then answers
in the chat that asked: "back online on <version>" — or, for an update, how it
went (an update that fails, or whose new code does not start, is rolled back).

⚠️ What these verbs never do:

* run without a supervisor — a process nobody would start again refuses here,
  before the owner confirms anything;
* swap code under a live process — the update is applied by the NEXT start,
  before it builds anything (``cli/commands/_lifecycle_boot.py``);
* reach the CLI package — the update check runs ``polyrob update --check
  --json`` in a subprocess (the entry-point tier may not import itself in a
  cycle; ``tests/test_layering_ratchet.py``).

``/update now`` (093 D6) is a different act: it starts nothing here. It records
that the owner wants the operator's waiting deploy (``scripts/deploy_when_idle.sh``)
to skip its owner-quiet window once (:func:`update_now_reply`).

The REPL twin (``cli/ui/commands/h_lifecycle.py``) calls the same functions.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from core.surfaces.cards import GO_WORDS as _GO

from core import lifecycle as lc

RESTART_USAGE = ("Usage: /restart — restart me once my current work is done\n"
                 "/restart now — restart at once, even in the middle of work")
UPDATE_USAGE = ("Usage: /update — update me to the latest release\n"
                "/update pre — include pre-releases\n"
                "/update now — the operator's deploy stops waiting for your chat to go quiet")

_UPDATE_CHANNELS = ("stable", "pre")
#: The install methods whose code ``polyrob update --apply`` can move itself.
_SELF_UPDATING = ("git", "editable_git")
CHECK_TIMEOUT_SEC = 60.0


def origin_of(result: Any, user_id: str) -> Dict[str, str]:
    """The chat a lifecycle message answers: surface, chat id and the owner."""
    out = {"user_id": str(user_id or "")}
    try:
        src = result.inbound.identity.source
        out["surface"] = str(getattr(src, "surface_id", "") or "")
        out["chat_id"] = str(getattr(src, "chat_id", "") or "")
        out["chat_type"] = str(getattr(src, "chat_type", "") or "")
    except AttributeError:
        pass
    return out


def _running_service(data_dir: str) -> Tuple[Optional[dict], str]:
    """``(boot record, "")`` of a live supervised surface process, or
    ``(None, why)``. An unreadable record is said, never read as "none"."""
    try:
        boot = lc.read_boot(data_home=data_dir)
    except lc.LifecycleUnreadable as exc:
        return None, f"I could not read my own start record ({exc})."
    if not boot:
        return None, ("I can't restart myself here: I am not running as a background "
                      "service, so nothing would start me again.")
    pid = int(boot.get("pid") or 0)
    alive = True
    try:
        os.kill(pid, 0)
    except PermissionError:
        alive = True
    except (OSError, ValueError):
        alive = False
    if not alive:
        return None, "I can't restart myself here: no background service of mine is running."
    if not boot.get("supervisor"):
        return None, ("I can't restart myself here: I run in a terminal, not as a background "
                      "service, so nothing would start me again.")
    return boot, ""


def _parse(args: List[str], allowed: Tuple[str, ...]) -> Tuple[List[str], bool, Optional[str]]:
    words = [str(a).lower() for a in (args or []) if str(a).strip()]
    go = any(w in _GO for w in words)
    rest = [w for w in words if w not in _GO]
    bad = [w for w in rest if w not in allowed]
    return rest, go, (bad[0] if bad else None)


def restart_reply(user_id: str, data_dir: str, args: List[str],
                  origin: Dict[str, str]) -> str:
    rest, go, bad = _parse(args, ("now",))
    if bad:
        return RESTART_USAGE
    now = "now" in rest
    boot, why = _running_service(data_dir)
    if boot is None:
        return why
    label = lc.version_label(boot.get("version"), boot.get("sha"))
    if not go:
        if now:
            body = (f"Restart now — this cuts any work in progress (a reply I am writing, a "
                    f"background run). I tell you here when I am back. Running: {label}.")
        else:
            body = (f"Restart — I finish my current work first (up to "
                    f"{lc.DEFAULT_DRAIN_SEC // 60} min), restart, and tell you here when I am "
                    f"back. Running: {label}.")
        line = "/restart now go" if now else "/restart go"
        return f"{body}\n\nTo restart: {line}"
    req, created = lc.request_restart(
        requested_by="owner", origin=origin, reason="/restart", force=now,
        data_home=data_dir)
    if not created:
        return f"Already asked: {req.describe()} — {req.state}. I tell you here when I am back."
    return ("OK — restarting " + ("now." if now else "once my current work is done.")
            + " I tell you here when I am back.")


async def _update_check(channel: str) -> Tuple[Optional[dict], str]:
    """``polyrob update --check --json`` in a subprocess: ``(payload, "")`` or
    ``(None, why)``."""
    cmd = [sys.executable, "-I", "-m", "cli.polyrob", "update", "--check", "--json",
           "--channel", channel]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=CHECK_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        return None, f"the update check did not answer within {int(CHECK_TIMEOUT_SEC)} s"
    except OSError as exc:
        return None, f"the update check could not start ({exc})"
    text = (out or b"").decode("utf-8", "replace").strip()
    try:
        start = text.index("{")
        return json.loads(text[start:]), ""
    except ValueError:
        tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        return None, f"the update check failed ({tail[0][:200] or 'no output'})"


def update_now_reply(data_dir: str, go: bool, origin: Dict[str, str]) -> str:
    """``/update now`` (093 D6): the operator's deploy (``scripts/deploy_when_idle.sh``)
    skips its owner-quiet window once — the owner's own chat no longer blocks the
    owner's own fix. Every other idle check stays. Reached only from an owner seat
    (``/update`` is an owner admin verb, refused in a room)."""
    mins = lc.OWNER_DEPLOY_NOW_TTL_SEC // 60
    if not go:
        return (f"Deploy now — the next deploy started within {mins} min does not wait for "
                f"your chat to be quiet for 10 min. It still waits for a running or due cron "
                f"job, a running goal, a reply I am writing and another deploy. This starts no "
                f"deploy by itself: it lets the one the operator started (or starts) go."
                f"\n\nTo deploy now: /update now go")
    try:
        rec = lc.request_owner_deploy_now(origin=origin, data_home=data_dir)
    except OSError as exc:
        return f"I could not record your request ({exc}); a waiting deploy still waits."
    until = time.strftime("%H:%M UTC", time.gmtime(rec["expires_at"]))
    return (f"OK — a deploy no longer waits for your chat to go quiet (until {until}). "
            f"It still waits for running work.")


async def update_reply(user_id: str, data_dir: str, args: List[str],
                       origin: Dict[str, str], *, check=None) -> str:
    rest, go, bad = _parse(args, _UPDATE_CHANNELS + ("now",))
    if bad or len(rest) > 1:
        return UPDATE_USAGE
    if rest == ["now"]:
        return update_now_reply(data_dir, go, origin)
    channel = rest[0] if rest else "stable"
    boot, why = _running_service(data_dir)
    if boot is None:
        return why.replace("restart myself", "update myself")
    info, err = await (check or _update_check)(channel)
    if info is None:
        return f"I could not check for an update: {err}."
    current, latest = info.get("current") or "?", info.get("latest")
    shown = str(latest or "").lstrip("v")  # a release tag reads "v1.4.0"
    method = str(info.get("method") or "unknown")
    if info.get("error") or not latest:
        return f"I could not check for an update: {info.get('error') or 'no release found'}."
    if not info.get("update_available"):
        return f"You are on the latest version ({current}, {channel} channel)."
    if method not in _SELF_UPDATING:
        return (f"Version {shown} is out (I run {current}), but this install ({method}) "
                "does not update itself — whoever installed me updates it.")
    line = "/update go" if channel == "stable" else f"/update {channel} go"
    if not go:
        return (f"Update {current} → {shown} ({method}). I finish my current work, restart, "
                f"take a snapshot, install, migrate and verify, then start the new version. "
                f"Any failure — or a new version that does not start — rolls back to "
                f"{current}. I am offline for about a minute and tell you here how it went."
                f"\n\nTo update: {line}")
    req, created = lc.request_restart(
        kind=lc.KIND_UPDATE, requested_by="owner", origin=origin, reason="/update",
        channel=channel, target_version=str(latest), data_home=data_dir)
    if not created:
        return f"Already asked: {req.describe()} — {req.state}. I tell you here how it went."
    return (f"OK — updating to {shown} once my current work is done. "
            "I tell you here how it went.")


__all__ = ["restart_reply", "update_reply", "update_now_reply", "origin_of", "RESTART_USAGE", "UPDATE_USAGE"]
