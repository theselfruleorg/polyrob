"""Unit tests for DeadTargetStore + classify_dead_error (T1.5, Task 1 — pure/unwired).

Mirrors test_circuit.py's CircuitStore coverage (tmp_path persist-across-instances,
fail-open on store error) plus the mark/is_dead/clear cycle, fail_count bump on
re-mark, and the classifier truth table.
"""
import pytest

from core.surfaces.dead_targets import DeadTargetStore, classify_dead_error


# ---------------------------------------------------------------------------
# DeadTargetStore
# ---------------------------------------------------------------------------

def test_store_persists_across_instances(tmp_path):
    """DeadTargetStore persists marks across separate instances (like CircuitStore)."""
    db = str(tmp_path / "dead_targets.db")
    writer = DeadTargetStore(db)
    writer.mark("telegram", "12345", "blocked")

    reader = DeadTargetStore(db)   # separate instance, same db file
    assert reader.is_dead("telegram", "12345") is True


def test_mark_is_dead_clear_cycle(tmp_path):
    db = str(tmp_path / "dead_targets.db")
    store = DeadTargetStore(db)

    assert store.is_dead("telegram", "999") is False   # never marked

    store.mark("telegram", "999", "blocked")
    assert store.is_dead("telegram", "999") is True

    store.clear("telegram", "999")
    assert store.is_dead("telegram", "999") is False


def test_clear_is_idempotent(tmp_path):
    """clear() on an address never marked (or already cleared) must not raise."""
    db = str(tmp_path / "dead_targets.db")
    store = DeadTargetStore(db)
    store.clear("telegram", "never-marked")   # no raise
    store.mark("telegram", "999", "blocked")
    store.clear("telegram", "999")
    store.clear("telegram", "999")            # second clear, no raise
    assert store.is_dead("telegram", "999") is False


def test_fail_count_bumps_on_remark(tmp_path):
    db = str(tmp_path / "dead_targets.db")
    store = DeadTargetStore(db)

    store.mark("telegram", "42", "blocked")
    rows = store.list_all()
    assert len(rows) == 1
    assert rows[0]["fail_count"] == 1
    assert rows[0]["reason"] == "blocked"

    store.mark("telegram", "42", "blocked")   # re-mark
    rows = store.list_all()
    assert len(rows) == 1
    assert rows[0]["fail_count"] == 2


def test_mark_stamps_marked_at(tmp_path):
    db = str(tmp_path / "dead_targets.db")
    store = DeadTargetStore(db)
    store.mark("telegram", "42", "blocked")
    row = store.list_all()[0]
    assert row["marked_at"] > 0


def test_list_all_multiple_surfaces(tmp_path):
    db = str(tmp_path / "dead_targets.db")
    store = DeadTargetStore(db)
    store.mark("telegram", "1", "blocked")
    store.mark("email", "a@b.com", "deactivated")
    rows = store.list_all()
    assert len(rows) == 2
    surfaces = {r["surface"] for r in rows}
    assert surfaces == {"telegram", "email"}


def test_is_dead_independent_per_surface(tmp_path):
    """The same address on a different surface is a distinct key."""
    db = str(tmp_path / "dead_targets.db")
    store = DeadTargetStore(db)
    store.mark("telegram", "123", "blocked")
    assert store.is_dead("telegram", "123") is True
    assert store.is_dead("email", "123") is False


def test_is_dead_fail_open_on_corrupted_db(tmp_path):
    """A corrupted/raising db must NOT crash is_dead — treat as alive (not dead)."""
    db = str(tmp_path / "dead_targets.db")
    store = DeadTargetStore(db)
    store.mark("telegram", "123", "blocked")

    # Corrupt the underlying file after construction so any subsequent query raises.
    with open(db, "wb") as f:
        f.write(b"not a sqlite database at all" * 50)

    assert store.is_dead("telegram", "123") is False   # no raise; degrades to alive


def test_address_normalized_case_insensitive(tmp_path):
    """Mirrors correspondents.py::_norm_addr — keying is case/space-insensitive."""
    db = str(tmp_path / "dead_targets.db")
    store = DeadTargetStore(db)
    store.mark("email", "  John@Example.com  ", "deactivated")
    assert store.is_dead("email", "john@example.com") is True
    assert store.is_dead("email", "JOHN@EXAMPLE.COM") is True


# ---------------------------------------------------------------------------
# classify_dead_error — pattern truth table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "error_text,expected_reason",
    [
        ("Forbidden: bot was blocked by the user", "blocked"),
        ("BOT WAS BLOCKED", "blocked"),                              # case-insensitive
        ("Forbidden: user is deactivated", "deactivated"),
        ("User Is Deactivated", "deactivated"),                      # case-insensitive
        ("Bad Request: chat not found", "chat_not_found"),
        ("Chat Not Found", "chat_not_found"),                        # case-insensitive
        ("PEER_ID_INVALID", "chat_not_found"),
        ("peer_id_invalid", "chat_not_found"),                       # case-insensitive
        ("Forbidden: bot was kicked from the group chat", "kicked"),
        ("Bot Was Kicked", "kicked"),                                # case-insensitive
    ],
)
def test_classify_dead_error_matches_known_patterns(error_text, expected_reason):
    assert classify_dead_error("telegram", error_text) == expected_reason


@pytest.mark.parametrize(
    "error_text",
    [
        "timeout",
        "Connection timeout after 30s",
        "rate limit",
        "Rate limit exceeded, retry later",
        "connection reset",
        "Connection reset by peer",
        "",
        None,
    ],
)
def test_classify_dead_error_returns_none_for_transient_or_unknown(error_text):
    assert classify_dead_error("telegram", error_text) is None
