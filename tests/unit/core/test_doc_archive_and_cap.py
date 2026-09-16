"""035 P2-12 / P2-14 — a silent truncation and an unexplained archive."""
from pathlib import Path

from core.instance import DEFAULT_INSTANCE_ID, SELF_CONTEXT_PER_DOC_MAX_CHARS
from core.self_context_writer import SelfContextWriter, PROVENANCE_USER


# --- P2-12: an over-cap SOUL doc must not lose its tail quietly -----------

def test_over_cap_load_is_loud_not_silent(tmp_path, caplog):
    """Writes ERROR over the cap, but a LOAD truncated with only a trailing
    marker — so a rule at the bottom of a hand-edited doc vanished and nothing
    said so. Keep the content, make the loss impossible to miss."""
    from core.instance import load_self_context
    d = tmp_path / "identity"
    d.mkdir(parents=True)
    (d / "identity.md").write_text("A" * (SELF_CONTEXT_PER_DOC_MAX_CHARS + 500))
    with caplog.at_level("WARNING"):
        out = load_self_context(tmp_path)
    assert out, "the doc must still load — dropping it entirely is worse"
    assert "TRUNCATED" in out.upper()
    # the warning must be at the TOP, where a reader (and the model) sees it
    assert "TRUNCATED" in out[:400].upper()
    assert any("truncat" in r.message.lower() for r in caplog.records), \
        "an operator must see this in the log too"


def test_under_cap_load_is_byte_identical(tmp_path):
    from core.instance import load_self_context
    d = tmp_path / "identity"
    d.mkdir(parents=True)
    (d / "identity.md").write_text("short and fine")
    assert load_self_context(tmp_path) == "short and fine"


# --- P2-14: an archived doc must name what replaced it -------------------

def test_archive_records_why_and_what_superseded_it(tmp_path):
    w = SelfContextWriter(tmp_path, instance_id=DEFAULT_INSTANCE_ID)
    w.propose("first rule", user_id="alice", created_by=PROVENANCE_USER, pending=False)
    w.propose("second rule", user_id="alice", created_by=PROVENANCE_USER, pending=False)
    index = (tmp_path / "identity" / DEFAULT_INSTANCE_ID / "user_alice"
             / ".archived" / "INDEX.md")
    assert index.is_file(), "no provenance index written"
    body = index.read_text()
    assert "self.0.md" in body
    assert "superseded" in body.lower()


def test_archive_index_appends_never_overwrites(tmp_path):
    w = SelfContextWriter(tmp_path, instance_id=DEFAULT_INSTANCE_ID)
    for n in range(3):
        w.propose(f"rule {n}", user_id="alice", created_by=PROVENANCE_USER, pending=False)
    index = (tmp_path / "identity" / DEFAULT_INSTANCE_ID / "user_alice"
             / ".archived" / "INDEX.md")
    lines = [ln for ln in index.read_text().splitlines() if ln.startswith("- ")]
    assert len(lines) == 2, f"one line per archived version, got: {lines}"


def test_rejected_draft_is_recorded_too(tmp_path):
    w = SelfContextWriter(tmp_path, instance_id=DEFAULT_INSTANCE_ID)
    w.propose("a draft", user_id="alice", created_by="agent", pending=True)
    w.reject(user_id="alice")
    index = (tmp_path / "identity" / DEFAULT_INSTANCE_ID / "user_alice"
             / ".archived" / "INDEX.md")
    assert "rejected.0.md" in index.read_text()


def test_archive_index_failure_never_blocks_a_write(tmp_path, monkeypatch):
    w = SelfContextWriter(tmp_path, instance_id=DEFAULT_INSTANCE_ID)
    w.propose("first", user_id="alice", created_by=PROVENANCE_USER, pending=False)
    monkeypatch.setattr(SelfContextWriter, "_note_archive",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    res = w.propose("second", user_id="alice", created_by=PROVENANCE_USER, pending=False)
    assert res.ok, "provenance is best-effort; it must never block the write"
