"""Cross-process session controls, acknowledged at agent step boundaries.

One SQLite mailbox lives inside each session directory. A live run owns its
generation; requests cannot affect a later run or another profile's session.
"""
from contextlib import contextmanager
import os
import sqlite3
import uuid
from pathlib import Path

from core.state.machine import StateMachine

#: 098 CO-05: the ``request`` column is a machine, not a "last command wins"
#: slot. ``cancel`` is final for the run: a later pause or resume is refused.
SESSION_CONTROL = StateMachine(
    "session_control",
    states=frozenset({"run", "pause", "resume", "cancel"}),
    transitions={
        "run": frozenset({"pause", "cancel", "resume"}),
        "pause": frozenset({"resume", "cancel", "pause"}),
        "resume": frozenset({"pause", "cancel"}),
    },
    terminal=frozenset({"cancel"}),
    initial=frozenset({"run"}),
)

#: ``request()`` / ``request_and_wait()`` answer for a pause or resume that
#: lost to an unacknowledged (or acknowledged) cancel: the run is stopping.
CANCELLED_ALREADY = "cancelled_already"


class SessionControl:
    def __init__(self, directory):
        self.path = Path(directory) / "control.sqlite3"

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY, generation TEXT, pid INTEGER, active INTEGER, request TEXT, acknowledged TEXT)")
            if fresh:
                # SQLite births 0644 whatever the umask says; the shared data
                # home needs group-write (the ONE rule, as init_schema applies).
                from core.data_perms import apply_birth_mode
                apply_birth_mode(self.path)
            with conn:
                yield conn
        finally:
            conn.close()

    def read(self):
        if not self.path.exists():
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM control WHERE id=1").fetchone()
            return dict(row) if row else None

    @staticmethod
    def alive(row):
        """A live run holds the row. The generation carries the run's process
        identity (``<uuid>|<pid>|<start>|<boot>``), so a pid reused after a reboot
        is not the holder; an old row (a bare uuid) keeps the bare-pid check."""
        if not row or not row["active"]:
            return False
        gen = str(row["generation"] or "")
        if "|" in gen:
            from core.state.proc import ProcIdentity, is_alive
            ident = ProcIdentity.decode(gen.split("|", 1)[1])
            if ident is not None:
                return is_alive(ident)
        try:
            os.kill(row["pid"], 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def start(self):
        from core.state.proc import process_identity
        generation = f"{uuid.uuid4().hex}|{process_identity().encode()}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM control WHERE id=1").fetchone()
            if self.alive(row):
                raise RuntimeError("This session already has a live run. Use session resume for a paused run, or cancel it and wait for completion before starting another.")
            conn.execute("INSERT OR REPLACE INTO control VALUES (1,?,?,1,'run','running')", (generation, os.getpid()))
        return generation

    def request(self, command):
        if command not in {"pause", "cancel", "resume"}:
            raise ValueError("Unknown session control")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM control WHERE id=1").fetchone()
            if not self.alive(row):
                return None
            current = row["request"]
            if current == "cancel":
                return CANCELLED_ALREADY if command != "cancel" else row["generation"]
            if current == command and command != "pause":
                return row["generation"]  # already the state asked for: nothing to write
            froms = tuple(sorted(SESSION_CONTROL.sources_of(command)))
            cur = conn.execute(
                "UPDATE control SET request=?, acknowledged='pending' WHERE id=1 AND generation=? "
                f"AND request IN ({','.join('?' * len(froms))})",
                (command, row["generation"], *froms))
            if cur.rowcount != 1:  # S3: a lost transition — say what the state is now
                return None
            return row["generation"]

    async def request_and_wait(self, command, timeout=2.0):
        """Request *command* and wait up to *timeout* s for the step-boundary ack.

        Returns the acknowledged state (``paused``/``running``/``cancelled``),
        ``"pending"`` when the run has not reached a step boundary yet,
        :data:`CANCELLED_ALREADY` when a cancel already holds the run (098
        CO-05: a pause/resume never erases it), or ``None`` when no live run
        holds this mailbox. The ONE wait loop the CLI
        ``polyrob session`` verbs and the owner ``/run`` verb share.
        """
        import asyncio
        import time

        generation = self.request(command)
        if generation is None or generation == CANCELLED_ALREADY:
            return generation
        expected = {"pause": "paused", "cancel": "cancelled", "resume": "running"}[command]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.read()
            if row and row["generation"] == generation and row["acknowledged"] == expected:
                return expected
            await asyncio.sleep(0.05)
        return "pending"

    def acknowledge(self, generation, request, state):
        with self._connect() as conn:
            conn.execute("UPDATE control SET acknowledged=? WHERE id=1 AND generation=? AND request=?", (state, generation, request))

    def finish(self, generation):
        with self._connect() as conn:
            conn.execute("UPDATE control SET active=0, acknowledged=CASE WHEN request='cancel' THEN 'cancelled' ELSE 'finished' END WHERE id=1 AND generation=?", (generation,))
