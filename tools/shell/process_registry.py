"""Background-job registry shared by the `shell` (launch) and `process` (manage) tools.

A process-global, session-keyed record of background jobs started via
`shell_run(background=True)`. The registry holds lightweight job METADATA (id,
command, start time, last-known status, executor); the actual pid/log live in container
files under `/tmp/polyrob-jobs/<id>.*` in the session container (or, for a posture-3
host job, under the session `logs/jobs/` dir) (so they survive a
host-process restart within the container's life). Finished jobs are retained for a TTL so `process poll/log` can
still report them, then reaped. 073 W4: a finished job also leaves a durable
receipt (``tools/shell/receipts.py``) that outlives this in-memory view.

Tenant/session scoping: a session only ever sees jobs it started (keyed by
session_id). A leaf/forged turn can't reach these tools at all (posture gate), so no
cross-session read is possible here.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from tools.shell.job_state import JOB, KILLED, LOST, RUNNING, UNKNOWN, UNKNOWN_LIMIT

@dataclass
class Job:
    """One background shell job's metadata (output/pid live in the container)."""

    id: str
    session_id: str
    command: str
    created_at: float
    status: str = "running"  # running | done | killed | lost (098 TS-10: shell.job)
    finished_at: Optional[float] = None
    #: 073 W1: which executor launched it ("docker" | "host"). The `process` tool
    #: manages a job only through that same executor.
    backend: str = "docker"
    #: 073 W4: the job runs on a pseudo-terminal (host only).
    pty: bool = False
    #: 073 W4: "exit" | "pattern" | None — what the watcher reports back.
    notify: Optional[str] = None
    #: consecutive "unknown" probes (098 TS-10); reset by any real observation.
    unobserved: int = 0


class ProcessRegistry:
    """In-memory, thread-safe, session-keyed job store."""

    def __init__(self, *, finished_ttl_sec: int = 3600, max_per_session: int = 64):
        self._by_session: Dict[str, Dict[str, Job]] = {}
        self._lock = threading.Lock()
        self._finished_ttl_sec = finished_ttl_sec
        self._max_per_session = max_per_session
        self._counter = 0
        import secrets
        self._suffix = secrets.token_hex(3)

    def _new_id(self) -> str:
        # A per-process counter plus a per-process random suffix: readable, and
        # unique ACROSS restarts — 073 W4 durable receipts outlive the process, so
        # a fresh process's job-0001 must never collide with the last one's.
        self._counter += 1
        return f"job-{self._counter:04d}-{self._suffix}"

    def create(self, session_id: str, command: str, *, now: float,
               backend: str = "docker", pty: bool = False,
               notify: Optional[str] = None) -> Job:
        """Register a new running job for ``session_id`` and return it."""
        with self._lock:
            jobs = self._by_session.setdefault(session_id, {})
            self._reap_locked(session_id, now)
            if len(jobs) >= self._max_per_session:
                # drop the oldest finished job to make room; never evict a running one
                finished = sorted(
                    (j for j in jobs.values() if j.status != "running"),
                    key=lambda j: j.finished_at or 0.0,
                )
                if finished:
                    jobs.pop(finished[0].id, None)
            job = Job(id=self._new_id(), session_id=session_id, command=command,
                      created_at=now, backend=backend, pty=pty, notify=notify)
            jobs[job.id] = job
            return job

    def get(self, session_id: str, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._by_session.get(session_id, {}).get(job_id)

    def list(self, session_id: str, *, now: Optional[float] = None) -> List[Job]:
        with self._lock:
            if now is not None:
                self._reap_locked(session_id, now)
            return sorted(
                self._by_session.get(session_id, {}).values(),
                key=lambda j: j.created_at,
            )

    def mark(self, session_id: str, job_id: str, status: str, *, now: float) -> None:
        """Move a job along the ``shell.job`` table; a repeat is a no-op and a
        forbidden edge (e.g. done -> running) is refused + logged."""
        with self._lock:
            job = self._by_session.get(session_id, {}).get(job_id)
            if job is None or status == job.status:
                return
            if not JOB.check(job.status, status, ctx={"job": job_id}):
                return
            job.status = status
            if job.finished_at is None:
                job.finished_at = now

    def observe(self, session_id: str, job_id: str, probe: str, *, now: float) -> str:
        """Fold one executor probe into the job. Returns ``running`` | ``done`` |
        ``lost`` | ``unknown`` (unknown = not observed this time; still running)."""
        lost = False
        with self._lock:
            job = self._by_session.get(session_id, {}).get(job_id)
            if job is None or probe != UNKNOWN:
                if job is not None:
                    job.unobserved = 0
                return probe
            job.unobserved += 1
            lost = job.unobserved >= UNKNOWN_LIMIT
        if lost:
            self.mark(session_id, job_id, LOST, now=now)
            return LOST
        return UNKNOWN

    def close_session(self, session_id: str, *, now: float) -> List[Job]:
        """The session's sandbox is gone: every running job is ``killed``.
        Returns those jobs (the caller writes their receipts)."""
        with self._lock:
            running = [j for j in self._by_session.get(session_id, {}).values()
                       if j.status == RUNNING]
        for j in running:
            self.mark(session_id, j.id, KILLED, now=now)
        return running

    def _reap_locked(self, session_id: str, now: float) -> None:
        jobs = self._by_session.get(session_id, {})
        stale = [
            jid for jid, j in jobs.items()
            if j.status != "running"
            and j.finished_at is not None
            and (now - j.finished_at) > self._finished_ttl_sec
        ]
        for jid in stale:
            jobs.pop(jid, None)


_REGISTRY: Optional[ProcessRegistry] = None
_REGISTRY_LOCK = threading.Lock()


def get_process_registry() -> ProcessRegistry:
    """Process-global singleton shared by the shell + process tools."""
    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_LOCK:
            if _REGISTRY is None:
                _REGISTRY = ProcessRegistry()
    return _REGISTRY
