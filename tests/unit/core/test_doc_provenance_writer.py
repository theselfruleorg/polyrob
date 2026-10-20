"""057 WS-D — the identity-doc writers stamp provenance and never refuse a
write for its wording. Covers BOTH docs (owner.md + self.md) and both write paths
(update via propose/apply_now, and patch)."""
import pytest

from core.doc_claims import stamp_of
from core.owner_doc_writer import OwnerDocWriter
from core.self_context_writer import PROVENANCE_USER, SelfContextWriter


@pytest.fixture
def home(tmp_path, monkeypatch):
    return tmp_path


def _owner(home):
    return OwnerDocWriter(home, instance_id="polyrob")


def test_no_source_is_byte_identical(home):
    w = _owner(home)
    assert w.propose("I live in Berlin", user_id="u1",
                     created_by=PROVENANCE_USER, pending=False).ok
    assert w.read("u1") == "I live in Berlin"


def test_update_stamps_only_new_lines(home):
    w = _owner(home)
    w.propose("I live in Berlin", user_id="u1", created_by=PROVENANCE_USER,
              pending=False, source="owner said", observed_at="2026-07-01")
    w.propose("I live in Berlin\nI ship on Fridays", user_id="u1",
              created_by=PROVENANCE_USER, pending=False,
              source="measured", observed_at="2026-09-19")
    lines = w.read("u1").splitlines()
    assert stamp_of(lines[0]) == "2026-07-01"   # unchanged line keeps its date
    assert stamp_of(lines[1]) == "2026-09-19"
    assert "measured" in lines[1]


def test_patch_stamps_only_the_patched_line(home):
    w = _owner(home)
    w.propose("a fact\nanother fact", user_id="u1", created_by=PROVENANCE_USER,
              pending=False)
    res = w.patch(user_id="u1", old_string="another fact",
                  new_string="a corrected fact", created_by=PROVENANCE_USER,
                  pending=False, source="measured", observed_at="2026-09-19")
    assert res.ok
    lines = w.read("u1").splitlines()
    assert lines[0] == "a fact"                      # untouched, unstamped
    assert stamp_of(lines[1]) == "2026-09-19"


def test_self_doc_gets_the_same_treatment(home):
    w = SelfContextWriter(home, instance_id="polyrob")
    w.propose("I prefer terse notes", user_id="u1", created_by=PROVENANCE_USER,
              pending=False, source="measured", observed_at="2026-09-19")
    assert stamp_of(w.read("u1").splitlines()[0]) == "2026-09-19"


def test_stamp_counts_toward_the_cap(home):
    w = _owner(home)
    body = "x" * (w._MAX_CHARS - 5)      # fits bare, cannot fit with a stamp
    assert w.propose(body, user_id="u1", created_by=PROVENANCE_USER,
                     pending=False).ok
    res = w.propose(body, user_id="u2", created_by=PROVENANCE_USER,
                    pending=False, source="measured", observed_at="2026-09-19")
    assert not res.ok
    assert "chars" in res.errors[0]


def test_an_unsourced_claim_is_written_as_given(home):
    w = _owner(home)
    res = w.propose("X posting is broken since July", user_id="u1",
                    created_by=PROVENANCE_USER, pending=False)
    assert res.ok
    assert w.read("u1").strip() == "X posting is broken since July"


def test_a_sourced_claim_is_stamped(home):
    w = _owner(home)
    res = w.propose("X posting is broken", user_id="u1",
                    created_by=PROVENANCE_USER, pending=False,
                    source="room_read", observed_at="2026-09-19")
    assert res.ok
    assert stamp_of(w.read("u1")) == "2026-09-19"


def test_pending_write_diffs_against_the_pending_draft(home):
    w = _owner(home)
    w.propose("draft line", user_id="u1", created_by=PROVENANCE_USER, pending=True)
    w.propose("draft line\nsecond line", user_id="u1",
              created_by=PROVENANCE_USER, pending=True,
              source="measured", observed_at="2026-09-19")
    pending = (home / "identity" / "polyrob" / "user_u1" / ".pending" / "owner.md")
    lines = pending.read_text().splitlines()
    assert stamp_of(lines[0]) is None
    assert stamp_of(lines[1]) == "2026-09-19"
