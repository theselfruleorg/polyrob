"""Cross-process session controls, acknowledged at agent step boundaries.

One SQLite mailbox lives inside each session directory. A live run owns its
generation; requests cannot affect a later run or another profile's session.
"""
from contextlib import contextmanager
import os
import sqlite3
import uuid
from pathlib import Path


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
        if not row or not row["active"]:
            return False
        try:
            os.kill(row["pid"], 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def start(self):
        generation = uuid.uuid4().hex
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
            conn.execute("UPDATE control SET request=?, acknowledged='pending' WHERE id=1", (command,))
            return row["generation"]

    def acknowledge(self, generation, request, state):
        with self._connect() as conn:
            conn.execute("UPDATE control SET acknowledged=? WHERE id=1 AND generation=? AND request=?", (state, generation, request))

    def finish(self, generation):
        with self._connect() as conn:
            conn.execute("UPDATE control SET active=0, acknowledged=CASE WHEN request='cancel' THEN 'cancelled' ELSE 'finished' END WHERE id=1 AND generation=?", (generation,))
