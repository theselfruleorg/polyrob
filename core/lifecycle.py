"""lifecycle.py — the ONE record of "restart / update the running agent" (077).

Three callers ask for a restart — the owner's ``/restart`` / ``/update`` chat
verbs, ``polyrob service restart`` / ``polyrob update --apply`` on the CLI, and
(through an owner card) the agent's own ``self_env_restart_service`` — and one
place acts on it: the process that runs the chat surfaces (``polyrob gateway``
or a single-surface command). That process drains — stops taking new messages,
lets the live turn, goal run or cron job finish — and exits with
:data:`RESTART_EXIT_CODE`; the supervisor that already owns it (systemd
``RestartForceExitStatus=75``, launchd ``KeepAlive``) starts it again. The
agent therefore never holds a right over its own supervisor.

Everything lives under ``<data_home>/lifecycle/`` (``core.runtime_paths.
resolve_data_home`` — the same rule every reader uses):

- ``request.json`` — the one open request (kind, who asked, the chat to answer,
  the drain timeout, ``state`` open → draining → exiting). A second request
  returns the open one; it never queues a second restart.
- ``boot.json`` — each boot of a surface process: pid, version, sha, the
  request it answers, start attempts since the last healthy start, and
  ``healthy_at`` once every surface is up.
- ``history.jsonl`` — one line per finished request (``back_online``,
  ``updated``, ``rolled_back``, ``boot_failed``, ``drain_timeout``,
  ``cancelled``). ``/status`` and ``polyrob doctor`` read the last line.
- ``pending.json`` — settings written while a process was running that only
  apply at its next start (``config_service.set_value``). An entry older than
  the running process's boot is already applied and is not shown.

stdlib + ``core.runtime_paths`` only: the boot step reads this module before
anything else of the (possibly just swapped) tree is imported.

⚠️ An unreadable record is reported as unreadable, never as "nothing pending"
(the repo-wide rule): :func:`read_request` raises :class:`LifecycleUnreadable`.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

#: The exit code that means "restart me" (BSD ``EX_TEMPFAIL``, the usual
#: choice). The unit maps it with ``RestartForceExitStatus=75`` +
#: ``SuccessExitStatus=75`` — restarted, never counted as a failure.
RESTART_EXIT_CODE = 75

KIND_RESTART = "restart"
KIND_UPDATE = "update"
KINDS = (KIND_RESTART, KIND_UPDATE)

#: How long a drain waits for the live work to finish before it gives up
#: (the restart is then NOT done; the requester is told what was busy).
DEFAULT_DRAIN_SEC = 600
#: A start that never reaches a healthy tick this many times in a row after an
#: update rolls the update back.
BOOT_FAILURE_ATTEMPTS = 3

STATE_OPEN = "open"
STATE_DRAINING = "draining"
STATE_EXITING = "exiting"

OUTCOME_BACK_ONLINE = "back_online"
OUTCOME_UPDATED = "updated"
OUTCOME_ROLLED_BACK = "rolled_back"
OUTCOME_BOOT_FAILED = "boot_failed"
OUTCOME_DRAIN_TIMEOUT = "drain_timeout"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_UPDATE_FAILED = "update_failed"


class LifecycleUnreadable(RuntimeError):
    """A lifecycle record exists but cannot be read."""


def lifecycle_dir(data_home: Optional[os.PathLike] = None) -> Path:
    if data_home is None:
        from core.runtime_paths import resolve_data_home
        data_home = resolve_data_home()
    return Path(data_home) / "lifecycle"


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LifecycleUnreadable(f"{path}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict):
        raise LifecycleUnreadable(f"{path}: not a JSON object")
    return data


@contextlib.contextmanager
def _locked(data_home: Optional[os.PathLike]) -> Iterator[Path]:
    """Serialise read-modify-write of the records across processes (flock)."""
    d = lifecycle_dir(data_home)
    d.mkdir(parents=True, exist_ok=True)
    fh = open(d / ".lock", "a+")
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover — no flock (Windows is unsupported)
            pass
        yield d
    finally:
        fh.close()


# --------------------------------------------------------------------------
# The running code
# --------------------------------------------------------------------------

def git_head() -> Tuple[Optional[str], str]:
    """``(full commit, branch)`` of the checkout this code runs from, read
    straight from ``.git`` (no subprocess); ``(None, "")`` for a wheel. A
    detached HEAD (a release tag) has the branch ``"HEAD"``."""
    root = Path(__file__).resolve().parents[1]
    git = root / ".git"
    try:
        if git.is_file():  # a worktree: "gitdir: <path>"
            git = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return (head or None), "HEAD"
        ref = head.split(":", 1)[1].strip()
        branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        p = git / ref
        if p.exists():
            return (p.read_text(encoding="utf-8").strip() or None), branch
        packed = git / "packed-refs"
        if packed.exists():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0], branch
        return None, branch
    except Exception:
        return None, ""


def code_sha() -> Optional[str]:
    """The commit the running code is (12 chars): the deploy stamp, else the
    checkout's HEAD. None for a wheel."""
    try:
        from core.version import deployed_sha
        sha = deployed_sha()
        if sha:
            return sha
    except Exception:
        pass
    sha, _branch = git_head()
    return sha[:12] if sha else None


def code_version() -> str:
    try:
        from core.version import get_version
        return get_version()
    except Exception:
        return "unknown"


def version_label(version: Optional[str] = None, sha: Optional[str] = None) -> str:
    """``1.4.0 (abc1234)`` — the words every lifecycle message uses."""
    v = version or code_version()
    return f"{v} ({sha})" if sha else v


#: The launchd job label ``polyrob service install`` writes on macOS.
LAUNCHD_LABEL = "dev.polyrob.agent"


def _in_service_cgroup(path: str = "/proc/self/cgroup") -> bool:
    """True unless this process's cgroup is readable and is not a systemd service."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        return True
    leaf = (lines[-1].rsplit("/", 1)[-1] if lines else "")
    return not leaf or leaf.endswith(".service")


def supervisor() -> Optional[str]:
    """What starts this process again after a restart exit, or None.

    systemd sets ``INVOCATION_ID`` for every unit it runs (the user unit, the
    deployment's system units, ``polyrob@<profile>``); launchd sets
    ``XPC_SERVICE_NAME`` to the job's label (a terminal has another value, so
    only OUR label counts). A bare ``polyrob gateway`` in a terminal has
    neither — a restart exit would just stop it. A desktop terminal that runs as a
    user service (GNOME's) hands ``INVOCATION_ID`` to its shells too, so the
    process's own cgroup must end in a ``.service`` — a shell lives in a
    ``.scope``."""
    if (os.environ.get("INVOCATION_ID") or "").strip() and _in_service_cgroup():
        return "systemd"
    if (os.environ.get("XPC_SERVICE_NAME") or "").strip() == LAUNCHD_LABEL:
        return "launchd"
    return None


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------

@dataclass
class LifecycleRequest:
    id: str
    kind: str
    requested_by: str
    origin: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    channel: str = "stable"
    target_version: str = ""
    created_at: float = 0.0
    drain_timeout: float = DEFAULT_DRAIN_SEC
    force: bool = False
    state: str = STATE_OPEN
    claimed_by: int = 0
    from_version: str = ""
    from_sha: str = ""
    #: Set by the boot step after it applied (or failed to apply) an update.
    update_result: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "LifecycleRequest":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)

    def describe(self) -> str:
        who = self.requested_by or "someone"
        when = time.strftime("%H:%M", time.localtime(self.created_at or time.time()))
        what = "update" if self.kind == KIND_UPDATE else "restart"
        return f"{what} asked by {who} at {when}"


def _request_path(d: Path) -> Path:
    return d / "request.json"


def read_request(data_home: Optional[os.PathLike] = None) -> Optional[LifecycleRequest]:
    data = _read_json(_request_path(lifecycle_dir(data_home)))
    return LifecycleRequest.from_dict(data) if data else None


def request_restart(*, kind: str = KIND_RESTART, requested_by: str,
                    origin: Optional[Dict[str, Any]] = None, reason: str = "",
                    channel: str = "stable", target_version: str = "",
                    drain_timeout: float = DEFAULT_DRAIN_SEC, force: bool = False,
                    data_home: Optional[os.PathLike] = None
                    ) -> Tuple[LifecycleRequest, bool]:
    """Open a restart (or update-then-restart) request.

    Returns ``(request, created)``. An open request is returned as it is with
    ``created=False`` — the caller says "already asked" instead of queueing a
    second restart. An unreadable request file is replaced (it can never be
    acted on) and the replacement is created."""
    if kind not in KINDS:
        raise ValueError(f"unknown lifecycle kind: {kind!r}")
    with _locked(data_home) as d:
        try:
            existing = _read_json(_request_path(d))
        except LifecycleUnreadable:
            existing = None
        if existing:
            return LifecycleRequest.from_dict(existing), False
        req = LifecycleRequest(
            id=uuid.uuid4().hex[:10], kind=kind, requested_by=requested_by,
            origin=dict(origin or {}), reason=reason, channel=channel,
            target_version=target_version, created_at=time.time(),
            drain_timeout=float(drain_timeout), force=bool(force),
            from_version=code_version(), from_sha=code_sha() or "")
        _write_json(_request_path(d), asdict(req))
        return req, True


def claim_request(pid: Optional[int] = None,
                  data_home: Optional[os.PathLike] = None, *,
                  serves: Iterable[str] = (), primary: bool = True
                  ) -> Optional[LifecycleRequest]:
    """Atomically take an OPEN request for this process (state → draining).

    Exactly one surface process acts on a request: a box can run more than one
    on one data home (prod runs the agent unit beside an email unit). A PRIMARY
    process (the one that runs the autonomy loops — the gateway, the agent
    unit) takes any request; another takes only a restart that came from a
    surface it serves, never an update (only the primary applies one). A request claimed by a process that is gone (it crashed
    mid-drain) is claimable again."""
    pid = pid or os.getpid()
    serves = frozenset(serves)
    with _locked(data_home) as d:
        try:
            data = _read_json(_request_path(d))
        except LifecycleUnreadable:
            return None
        if not data:
            return None
        req = LifecycleRequest.from_dict(data)
        if not primary and (req.kind == KIND_UPDATE
                            or str((req.origin or {}).get("surface") or "") not in serves):
            return None  # only the primary's boot step applies an update
        if req.state != STATE_OPEN:
            if req.claimed_by and req.claimed_by != pid and _pid_alive(req.claimed_by):
                return None
            if req.state == STATE_EXITING:
                return None  # the next boot answers it
        req.state = STATE_DRAINING
        req.claimed_by = pid
        _write_json(_request_path(d), asdict(req))
        return req


def set_request_state(state: str, data_home: Optional[os.PathLike] = None,
                      **fields: Any) -> Optional[LifecycleRequest]:
    with _locked(data_home) as d:
        try:
            data = _read_json(_request_path(d))
        except LifecycleUnreadable:
            return None
        if not data:
            return None
        data.update(fields)
        data["state"] = state
        _write_json(_request_path(d), data)
        return LifecycleRequest.from_dict(data)


def reopen_request(data_home: Optional[os.PathLike] = None) -> None:
    """Back to OPEN, unclaimed (a drain that was interrupted, not finished)."""
    set_request_state(STATE_OPEN, data_home, claimed_by=0)


def close_request(outcome: str, detail: str = "",
                  data_home: Optional[os.PathLike] = None,
                  **extra: Any) -> Optional[dict]:
    """Remove the open request and append its outcome to the history."""
    with _locked(data_home) as d:
        try:
            data = _read_json(_request_path(d)) or {}
        except LifecycleUnreadable as exc:
            data = {"unreadable": str(exc)}
        return _close_held(d, data, outcome, detail, **extra)


def _close_held(d: Path, data: dict, outcome: str, detail: str, **extra: Any) -> dict:
    """``close_request``'s body; the caller holds ``_locked``."""
    entry = {"at": time.time(), "outcome": outcome, "detail": detail,
             "request": data, **extra}
    with open(d / "history.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    try:
        _request_path(d).unlink()
    except FileNotFoundError:
        pass
    return entry


def withdraw_if_open(request_id: str, detail: str,
                     data_home: Optional[os.PathLike] = None) -> bool:
    """Close the request as cancelled only if it is still OPEN (nobody claimed
    it) — under the lock, so a claim landing at the same moment wins."""
    with _locked(data_home) as d:
        try:
            data = _read_json(_request_path(d))
        except LifecycleUnreadable:
            return False
        if not data or data.get("id") != request_id or data.get("state") != STATE_OPEN:
            return False
        _close_held(d, data, OUTCOME_CANCELLED, detail)
    return True


def settle_stale_request(started_at: float,
                         data_home: Optional[os.PathLike] = None) -> Optional[LifecycleRequest]:
    """At a PRIMARY boot: a request made before this start whose drain never
    finished (its process stopped or crashed) was answered by this very start —
    mark it ``exiting`` so the boot applies an asked-for update and the healthy
    start reports back, instead of draining and restarting a second time."""
    with _locked(data_home) as d:
        try:
            data = _read_json(_request_path(d))
        except LifecycleUnreadable:
            return None
        if not data:
            return None
        req = LifecycleRequest.from_dict(data)
        if req.state == STATE_EXITING or float(req.created_at or 0) >= started_at:
            return None
        if req.state == STATE_DRAINING and _pid_alive(req.claimed_by) \
                and req.claimed_by != os.getpid():
            return None  # another live process is draining for it
        data["state"] = STATE_EXITING
        _write_json(_request_path(d), data)
        return LifecycleRequest.from_dict(data)


def history(limit: int = 5, data_home: Optional[os.PathLike] = None) -> List[dict]:
    p = lifecycle_dir(data_home) / "history.jsonl"
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LifecycleUnreadable(f"{p}: {exc}") from exc
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# The in-process drain flag
# --------------------------------------------------------------------------

_draining = False


def begin_drain() -> None:
    """This process is draining for a restart: the cron tick and the goal
    dispatcher start nothing new (they read :func:`draining`). A running job
    or goal is left to finish — that is what the drain waits for."""
    global _draining
    _draining = True


def end_drain() -> None:
    global _draining
    _draining = False


def draining() -> bool:
    return _draining


# --------------------------------------------------------------------------
# The boot record
# --------------------------------------------------------------------------

def _boot_file(d: Path, role: str = "") -> Path:
    """``boot.json`` is the PRIMARY surface process (it runs the autonomy loops —
    the gateway, prod's agent unit). Another surface process on the same data
    home (prod's email unit) keeps its own ``boot-<role>.json`` so it never
    overwrites the record a restart and the status read."""
    if not role:
        return d / "boot.json"
    safe = "".join(c for c in role if c.isalnum() or c in "-_")[:32] or "surface"
    return d / f"boot-{safe}.json"


def read_boot(data_home: Optional[os.PathLike] = None, role: str = "") -> Optional[dict]:
    return _read_json(_boot_file(lifecycle_dir(data_home), role))


def record_boot(*, command: str = "", data_home: Optional[os.PathLike] = None,
                role: str = "") -> dict:
    """Stamp this process's start. ``attempts`` counts starts since the last
    healthy one (a crash loop shows as a growing number)."""
    with _locked(data_home) as d:
        try:
            prev = _read_json(_boot_file(d, role)) or {}
        except LifecycleUnreadable:
            prev = {}
        attempts = 1 if prev.get("healthy_at") or not prev else int(prev.get("attempts") or 0) + 1
        rec = {"pid": os.getpid(), "started_at": time.time(), "version": code_version(),
               "sha": code_sha() or "", "command": command, "attempts": attempts,
               "healthy_at": None, "stopped_at": None, "supervisor": supervisor() or "",
               "env_files": _env_ladder()}
        _write_json(_boot_file(d, role), rec)
        return rec


def _update_own_boot(data_home, role: str, **fields) -> Optional[dict]:
    with _locked(data_home) as d:
        try:
            rec = _read_json(_boot_file(d, role))
        except LifecycleUnreadable:
            return None
        if not rec or rec.get("pid") != os.getpid():
            return None
        rec.update(fields)
        _write_json(_boot_file(d, role), rec)
        return rec


def mark_healthy(data_home: Optional[os.PathLike] = None, role: str = "") -> Optional[dict]:
    return _update_own_boot(data_home, role, healthy_at=time.time(), attempts=0)


def mark_stopped(data_home: Optional[os.PathLike] = None, role: str = "") -> Optional[dict]:
    """A clean exit: the pid in the record must never read as a live service
    again (after a reboot an unrelated process can hold it)."""
    return _update_own_boot(data_home, role, stopped_at=time.time())


def reset_boot_attempts(data_home: Optional[os.PathLike] = None, role: str = "") -> None:
    """Start the failed-start count again (the boot step, right before it execs
    freshly applied code: that code's own starts are what count)."""
    _update_own_boot(data_home, role, attempts=0, healthy_at=None)


def running_service(data_home: Optional[os.PathLike] = None) -> Optional[dict]:
    """The boot record of the LIVE primary surface process that a supervisor
    would start again — the process a restart request reaches — or None.
    Raises :class:`LifecycleUnreadable` for an unreadable record."""
    boot = read_boot(data_home=data_home)
    if not boot or not boot.get("supervisor") or boot.get("stopped_at"):
        return None
    return boot if _pid_alive(int(boot.get("pid") or 0)) else None


def _env_ladder() -> list:
    """The env files this process's ladder reads (080 WS-1), for the boot record."""
    try:
        from core.paths import env_file_candidates
        return [str(Path(c.path).resolve()) for c in env_file_candidates()]
    except Exception:
        return []


def restart_hint(surface: str = "local",
                 data_home: Optional[os.PathLike] = None,
                 written: Optional[os.PathLike] = None) -> str:
    """One line naming how a written-but-not-live setting reaches the RUNNING
    service, or '' when no service runs (the next start reads it anyway).

    080 WS-1: with *written* (the env file the write landed in) the restart is
    promised only when that file is on the running service's ladder (its boot
    record's ``env_files``); a file it never reads is named instead, and a boot
    record without a ladder (an older process) promises nothing."""
    try:
        boot = running_service(data_home)
    except LifecycleUnreadable:
        return ""
    if boot is None:
        return ""
    if written is not None:
        ladder = boot.get("env_files")
        if not ladder:
            return ""
        if str(Path(written).resolve()) not in ladder:
            return (f"The running service does not read {written}, so a restart "
                    f"does not apply it.")
    if surface == "local":
        return "The running service applies it after `polyrob service restart` (or /restart)."
    return "Send /restart to apply it now."


# --------------------------------------------------------------------------
# Settings that wait for a restart
# --------------------------------------------------------------------------

def record_pending(key: str, scope: str = "",
                   data_home: Optional[os.PathLike] = None) -> None:
    """A setting was written that the running process applies only at its next
    start. Fail-open: a record that cannot be written must not fail the write."""
    try:
        with _locked(data_home) as d:
            try:
                data = _read_json(d / "pending.json") or {}
            except LifecycleUnreadable:
                data = {}
            data[str(key)] = {"scope": scope, "written_at": time.time()}
            _write_json(d / "pending.json", data)
    except OSError:
        pass


def pending_keys(data_home: Optional[os.PathLike] = None) -> List[str]:
    """Keys written AFTER the running surface process started (sorted).

    No running process (no boot record, or its pid is gone) → every key waits.
    """
    d = lifecycle_dir(data_home)
    data = _read_json(d / "pending.json") or {}
    if not data:
        return []
    boot = None
    try:
        boot = _read_json(d / "boot.json")
    except LifecycleUnreadable:
        boot = None
    since = 0.0
    if boot and not boot.get("stopped_at") and _pid_alive(int(boot.get("pid") or 0)):
        since = float(boot.get("started_at") or 0.0)
    return sorted(k for k, v in data.items()
                  if float((v or {}).get("written_at") or 0.0) > since)


#: A failed last restart/update stays a status health item this long.
_RECENT_FAILURE_SEC = 24 * 3600
_FAILED_OUTCOMES = (OUTCOME_BOOT_FAILED, OUTCOME_UPDATE_FAILED, OUTCOME_DRAIN_TIMEOUT)


def status_facts(data_dir: Optional[os.PathLike]) -> dict:
    """The restart/update facts of the status snapshot, as plain data:
    ``{"lines": [...], "health": [(key, text, remedy)], "data": {...}}`` —
    what the running service started as versus the code on disk, the settings
    that wait for a restart, an open request (and one nobody answered), and a
    recent failed outcome. Read-only; an unreadable record is said, never shown
    as nothing. Plain data, so this stdlib module imports no status type (the
    status modules form one import cycle; ``tests/test_layering_ratchet.py``)."""
    out: dict = {"lines": [], "health": [], "data": {}}
    lines, health, data = out["lines"], out["health"], out["data"]
    try:
        boot = running_service(data_dir)
    except LifecycleUnreadable as exc:
        lines.append(f"service: start record unreadable ({exc})")
        return out
    if boot is not None:
        started = version_label(boot.get("version"), boot.get("sha"))
        on_disk = version_label(code_version(), code_sha())
        data["service_started_as"] = started
        if started != on_disk:
            lines.append(f"service: started as {started}; the code on disk is {on_disk} "
                         "— it runs the new code after a restart")
            health.append(("service_code_stale",
                           f"the service still runs {started}; {on_disk} is installed",
                           "/restart"))
    try:
        keys = pending_keys(data_dir) if boot is not None else []
    except LifecycleUnreadable as exc:
        lines.append(f"waiting: unreadable ({exc})")
        keys = []
    if keys:
        data["pending_restart"] = keys
        shown = ", ".join(keys[:6]) + (" …" if len(keys) > 6 else "")
        lines.append(f"waiting: {len(keys)} setting(s) apply at the next restart: {shown}")
        health.append(("settings_wait_for_restart",
                       f"{len(keys)} setting(s) wait for a restart ({shown})", "/restart"))
    try:
        req = read_request(data_home=data_dir)
    except LifecycleUnreadable as exc:
        lines.append(f"restart: request unreadable ({exc})")
        req = None
    if req is not None:
        lines.append(f"restart: {req.describe()} — {req.state}")
        if req.state == STATE_EXITING and boot is None:
            health.append(("restart_not_back",
                           f"{req.describe()}: the service has not come back",
                           "check the service log: `polyrob service logs`"))
    try:
        last = history(limit=1, data_home=data_dir)
    except LifecycleUnreadable as exc:
        lines.append(f"restart: history unreadable ({exc})")
        last = []
    if last:
        outcome = str(last[0].get("outcome") or "")
        at = float(last[0].get("at") or 0)
        data["last_restart"] = {"outcome": outcome, "at": at}
        if outcome in _FAILED_OUTCOMES and time.time() - at < _RECENT_FAILURE_SEC:
            health.append((f"restart_{outcome}",
                           str(last[0].get("detail") or outcome)[:300],
                           "/restart or /update again when it suits you"))
    # 080 WS-1: a posture member set on its own env line is ignored — say so here,
    # not only in the journal.
    try:
        from core.config_policy.members import ignored_member_health
        item = ignored_member_health()
    except Exception:
        item = None
    if item is not None:
        health.append(item)
    return out


__all__ = [
    "RESTART_EXIT_CODE", "KIND_RESTART", "KIND_UPDATE", "DEFAULT_DRAIN_SEC",
    "BOOT_FAILURE_ATTEMPTS", "LifecycleRequest", "LifecycleUnreadable",
    "lifecycle_dir", "code_sha", "code_version", "version_label", "supervisor",
    "read_request", "request_restart", "claim_request", "set_request_state",
    "reopen_request", "close_request", "history", "read_boot", "record_boot",
    "mark_healthy", "mark_stopped", "reset_boot_attempts", "running_service", "restart_hint",
    "git_head", "withdraw_if_open", "settle_stale_request",
    "record_pending", "pending_keys",
    "begin_drain", "end_drain", "draining", "status_facts",
]
