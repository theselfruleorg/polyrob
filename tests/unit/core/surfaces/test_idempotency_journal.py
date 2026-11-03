"""098 CO-18: a processing row names its claimant; recover hands back only
rows whose claimant is dead; complete/release act on the caller's own claim."""
import sqlite3

from core.state.proc import ProcIdentity, process_identity
from core.surfaces.idempotency import ACCEPTED_EVENT, AcceptedEventJournal


def _set_owner(j, key, owner):
    c = sqlite3.connect(j.db_path)
    c.execute("UPDATE accepted_events SET claimed_by=? WHERE event_key=?", (owner, key))
    c.commit()
    c.close()


def _state(j, key):
    c = sqlite3.connect(j.db_path)
    row = c.execute("SELECT state FROM accepted_events WHERE event_key=?", (key,)).fetchone()
    c.close()
    return row[0] if row else None


def test_claim_records_the_claimant(tmp_path):
    j = AcceptedEventJournal(str(tmp_path / "j.db"))
    j.accept("e1", "wecom", b"x")
    assert j.claim("e1") == b"x"
    assert j.claim("e1") is None
    c = sqlite3.connect(j.db_path)
    assert c.execute("SELECT claimed_by FROM accepted_events").fetchone()[0] == \
        process_identity().encode()
    c.close()


def test_recover_keeps_live_claim(tmp_path, monkeypatch):
    j = AcceptedEventJournal(str(tmp_path / "j.db"))
    j.accept("e1", "wecom", b"x")
    j.claim("e1")
    other = ProcIdentity(999999, "live", "boot")
    _set_owner(j, "e1", other.encode())
    import core.state.proc as proc
    monkeypatch.setattr(proc, "is_alive", lambda ident: ident == other)
    assert j.recover("wecom") == []
    assert _state(j, "e1") == "processing"


def test_recover_resets_dead_claim(tmp_path):
    j = AcceptedEventJournal(str(tmp_path / "j.db"))
    j.accept("e1", "wecom", b"x")
    j.claim("e1")
    _set_owner(j, "e1", ProcIdentity(999999, "gone", "other-boot").encode())
    assert j.recover("wecom") == ["e1"]
    assert _state(j, "e1") == "accepted"


def test_complete_and_release_act_only_on_own_claim(tmp_path):
    j = AcceptedEventJournal(str(tmp_path / "j.db"))
    j.accept("e1", "wecom", b"x")
    j.claim("e1")
    _set_owner(j, "e1", ProcIdentity(1, "x", "y").encode())
    j.release("e1")
    assert _state(j, "e1") == "processing"
    j.complete("e1")
    assert _state(j, "e1") == "processing"


def test_release_fails_past_the_cap(tmp_path):
    j = AcceptedEventJournal(str(tmp_path / "j.db"), max_attempts=2)
    j.accept("e1", "wecom", b"x")
    j.claim("e1"); j.release("e1")
    assert _state(j, "e1") == "accepted"
    j.claim("e1"); j.release("e1")
    assert _state(j, "e1") == "failed"


def test_machine_well_formed():
    assert ACCEPTED_EVENT.validate() == []
