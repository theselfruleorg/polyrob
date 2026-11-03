"""Background shell job states (098 TS-10).

``running -> done | killed | lost``; ``done -> killed`` (a kill that raced the
exit). ``unknown`` is a PROBE result ("could not observe": no pid file, the
backend unreachable or restarted), never a stored state: it keeps the job
``running`` and counts; after :data:`UNKNOWN_LIMIT` probes in a row the job is
``lost``. Nothing writes ``done`` without a probe that saw it finish.
"""
from __future__ import annotations

from core.state.machine import StateMachine

RUNNING, DONE, KILLED, LOST, UNKNOWN = "running", "done", "killed", "lost", "unknown"

JOB = StateMachine(
    "shell.job",
    states={RUNNING, DONE, KILLED, LOST},
    transitions={RUNNING: {DONE, KILLED, LOST}, DONE: {KILLED}},
    terminal={KILLED, LOST},
    initial={RUNNING},
)

#: Consecutive ``unknown`` probes before a job is ``lost`` (~25 s of watcher time).
UNKNOWN_LIMIT = 5

LOST_NOTE = ("could not observe the job (backend unreachable or restarted); "
             "its state is unknown")
KILLED_NOTE = "session ended"

FINISHED = frozenset({DONE, KILLED, LOST})
