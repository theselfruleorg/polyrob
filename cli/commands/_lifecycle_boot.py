"""The boot step of a surface process (077): runs after the env layers load and
BEFORE the container builds — nothing is live yet.

1. Stamp ``boot.json`` (pid, version, sha, attempts since the last healthy start).
2. A request in state ``exiting`` of kind ``update`` with no result yet: the
   previous process drained and exited FOR this update. Apply it now
   (``cli.commands.update.apply_for_boot`` — snapshot → install → migrate →
   verify, auto-rollback), store the result on the request, and on success
   ``exec`` the same command line so the NEW code loads.
3. A request whose update applied but whose new code has now failed to reach a
   healthy start :data:`core.lifecycle.BOOT_FAILURE_ATTEMPTS` times in a row:
   roll the code and the data back and ``exec`` again on the old code.

The healthy start (``announce_boot``) closes the request and tells the owner.
Residual risk, named: new code so broken that THIS module cannot import never
reaches step 3; the supervisor's start limit then stops the unit and ``polyrob
doctor`` / ``polyrob service status`` show the open request.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Callable, Optional

from core import lifecycle as lc

logger = logging.getLogger(__name__)


def _reexec() -> None:  # pragma: no cover — replaces the process
    sys.stdout.flush()
    sys.stderr.flush()
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])
    os.execv(sys.executable, argv)


def _apply_shielded(apply: Callable[[str], dict], channel: str,
                    save: Callable[[dict], None] = lambda _r: None) -> dict:
    """Run the apply with SIGTERM deferred: a ``systemctl stop`` or a deploy
    mid-``git checkout``/``pip install`` would leave a torn tree. The stop is
    honoured as soon as the apply (and its own rollback) returns — AFTER its
    result is saved, or the next start reads ``in_progress`` and rolls back a
    good update."""
    import signal
    got = []
    try:
        old = signal.signal(signal.SIGTERM, lambda *_a: got.append(True))
    except (ValueError, OSError):  # not the main thread
        res = apply(channel)
        save(res)
        return res
    res: dict = {}
    try:
        res = apply(channel)
        save(res)
        return res
    finally:
        signal.signal(signal.SIGTERM, old)
        if got:
            raise SystemExit(0)


def boot_step(command: str, *, data_home: Optional[Path] = None,
              apply: Optional[Callable[[str], dict]] = None,
              rollback: Optional[Callable[[dict], dict]] = None,
              reexec: Callable[[], None] = _reexec,
              echo: Callable[[str], None] = print,
              role: str = "") -> Optional[str]:
    """Run the boot step. Returns what it did (for tests/logs) or None.
    Never raises (except the deferred stop): a broken lifecycle record must not
    stop the agent starting.

    ``role`` is "" for the PRIMARY surface process (the gateway, prod's agent
    unit). A secondary one (prod's email unit) stamps its own boot record and
    does nothing else — it never applies an update and never settles a request
    meant for the agent."""
    try:
        if data_home is None:
            from core.runtime_paths import resolve_data_home
            data_home = resolve_data_home()
        boot = lc.record_boot(command=command, data_home=data_home, role=role)
        if role:
            return None
        # A request whose process stopped or crashed mid-drain was answered by
        # this very start: never drain and restart a second time for it.
        lc.settle_stale_request(float(boot.get("started_at") or 0), data_home=data_home)
        req = lc.read_request(data_home=data_home)
    except Exception:
        logger.warning("lifecycle boot step: record unreadable — starting normally",
                       exc_info=True)
        return None
    if req is None or req.state != lc.STATE_EXITING or req.kind != lc.KIND_UPDATE:
        return None
    res = dict(req.update_result or {})
    if res.get("in_progress"):
        # The previous start was killed in the middle of the apply: the tree may
        # be torn. Roll back to the commit recorded before anything moved.
        if rollback is None:
            from cli.commands.update import rollback_for_boot as rollback
        echo("[polyrob] the last update was interrupted — rolling it back")
        rb = rollback(res)
        res.update(in_progress=False, ok=False, boot_failed=True, rolled_back=bool(rb.get("ok")),
                   rollback=rb)
        lc.set_request_state(lc.STATE_EXITING, data_home=data_home, update_result=res)
        lc.reset_boot_attempts(data_home=data_home)
        reexec()
        return "rolled_back"
    if not res:
        if apply is None:
            from cli.commands.update import apply_for_boot as apply
        echo(f"[polyrob] applying the update the owner asked for ({req.describe()})…")
        sha, branch = lc.git_head()
        import time as _time
        lc.set_request_state(lc.STATE_EXITING, data_home=data_home,
                             update_result={"in_progress": True, "old_sha": sha or "",
                                            "old_branch": branch or "HEAD",
                                            "started_at": _time.time()})
        # The version the owner confirmed: the boot's own release check may come
        # back empty (a transient GitHub reply) and must not cancel it.
        target = getattr(req, "target_version", "") or ""
        res = _apply_shielded(
            (lambda ch: apply(ch, target_version=target)) if target else apply,
            req.channel or "stable",
            lambda r: lc.set_request_state(lc.STATE_EXITING, data_home=data_home,
                                           update_result=r))
        if res.get("ok"):
            echo(f"[polyrob] updated {res.get('from_version')} → {res.get('to_version')}; "
                 "starting the new code")
            lc.reset_boot_attempts(data_home=data_home)
            reexec()
            return "applied"
        echo(f"[polyrob] update not applied ({res.get('failed_step') or 'no update'}): "
             f"{res.get('error') or 'already up to date'} — starting the current code")
        return "not_applied"
    if (res.get("ok") and not res.get("boot_failed")
            and int(boot.get("attempts") or 0) >= lc.BOOT_FAILURE_ATTEMPTS):
        if rollback is None:
            from cli.commands.update import rollback_for_boot as rollback
        echo(f"[polyrob] the new code failed to start {boot.get('attempts')} times — "
             "rolling the update back")
        rb = rollback(res)
        res.update(boot_failed=True, rollback=rb, ok=False,
                   rolled_back=bool(rb.get("ok")))
        lc.set_request_state(lc.STATE_EXITING, data_home=data_home, update_result=res)
        lc.reset_boot_attempts(data_home=data_home)
        reexec()
        return "rolled_back"
    return None


__all__ = ["boot_step"]
