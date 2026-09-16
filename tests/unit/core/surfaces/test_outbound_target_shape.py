"""C8 (2026-09-15 prod review): a target of the wrong SHAPE for its surface.

Prod's dead-target registry held exactly four rows, and one of them was
`telegram / rob@theselfrule.org / chat_not_found` — an EMAIL address sent to
the Bot API. The other three were the same channel under three spellings
(`thepublicden`, `t.me/thepublicden`, and the normalized `@thepublicden`),
which is one broken relationship counted three times.

A target that can never succeed should be refused with the reason, the way a
bot→bot send already is, rather than spending a send and a dead-target row.
"""
import pytest

from core.surfaces.outbound_target import (
    normalize_surface_target,
    wrong_surface_target_reason,
)


@pytest.mark.parametrize("target", [
    "rob@theselfrule.org", "glbschv@gmail.com", " owner@example.co.uk ",
])
def test_an_email_address_is_refused_on_telegram(target):
    why = wrong_surface_target_reason("telegram", target)
    assert why and "email" in why.lower()


@pytest.mark.parametrize("target", [
    "@thepublicden", "thepublicden", "28436760", "-1001234567890",
    "t.me/thepublicden",
])
def test_a_real_telegram_target_is_not_refused(target):
    assert wrong_surface_target_reason("telegram", target) is None


@pytest.mark.parametrize("target", ["28436760", "@thepublicden", "t.me/x"])
def test_a_telegram_shaped_target_is_refused_on_email(target):
    why = wrong_surface_target_reason("email", target)
    assert why and "email address" in why.lower()


def test_a_real_email_target_is_not_refused():
    assert wrong_surface_target_reason("email", "glbschv@gmail.com") is None


def test_unknown_surfaces_are_never_refused():
    assert wrong_surface_target_reason("slack", "U123") is None
    assert wrong_surface_target_reason("telegram", None) is None


def test_the_three_spellings_normalize_to_one_address():
    """The registry and the conversation store must not hold one relationship
    three times — that is what prod's dead-target table shows."""
    forms = ["thepublicden", "@thepublicden", "t.me/thepublicden",
             "https://t.me/thepublicden"]
    assert {normalize_surface_target("telegram", f) for f in forms} == {"@thepublicden"}


def test_conversation_store_collapses_the_three_spellings(tmp_path):
    """One relationship, one row — on READ as well as write, so a reply that
    arrives under any spelling resolves to the history we already have."""
    from core.surfaces.conversations import ConversationStore
    store = ConversationStore(str(tmp_path / "c.db"))
    store.record_outbound("rob", "telegram", "@thepublicden", "first")
    store.record_outbound("rob", "telegram", "t.me/thepublicden", "second")
    store.record_outbound("rob", "telegram", "ThePublicDen", "third")
    assert len(store.list("rob")) == 1
    for spelling in ("thepublicden", "@thepublicden", "t.me/thepublicden"):
        conv = store.get("rob", "telegram", spelling)
        assert conv is not None, spelling
    assert store.outbound_count_since("rob", "telegram", "thepublicden", 3600) == 3


def test_an_email_address_is_not_mangled_by_the_normalizer(tmp_path):
    from core.surfaces.conversations import ConversationStore
    store = ConversationStore(str(tmp_path / "c.db"))
    store.record_outbound("rob", "email", "Rob@TheSelfRule.org", "hi")
    assert store.get("rob", "email", "rob@theselfrule.org") is not None
    assert store.list("rob")[0]["address"] == "rob@theselfrule.org"


# --- round-2: the spellings ALREADY on disk must collapse too ---------------
#
# `_norm_addr` stops NEW splits, but prod already holds `thepublicden`,
# `@thepublicden` and `t.me/thepublicden` as three conversations with three
# histories — and the owner-resend cooldown reads this store by address. After
# the normalizer lands, reads resolve to one key and the other two rows become
# orphans still holding real messages. A fix that only helps fresh installs
# leaves the box it was written for exactly as broken.

def _legacy_store(tmp_path):
    """A store written by the PRE-normalizer code: raw addresses, lowercased."""
    import sqlite3
    path = tmp_path / "legacy.db"
    from core.surfaces.conversations import ConversationStore
    ConversationStore(str(path))                      # schema
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 0")   # as a pre-migration file reports
    for i, addr in enumerate(["thepublicden", "@thepublicden", "t.me/thepublicden"]):
        conn.execute("INSERT INTO conversations (user_id, surface, address, created_at,"
                     " updated_at, last_outbound_ts) VALUES (?,?,?,?,?,?)",
                     ("rob", "telegram", addr, 1.0, 1.0 + i, 1.0 + i))
        cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO conversation_messages (conversation_id, direction,"
                     " ts, body) VALUES (?,?,?,?)", (cid, "out", 1.0 + i, f"msg {i}"))
    conn.commit()
    conn.close()
    return path


def test_pre_existing_split_rows_are_merged_on_open(tmp_path):
    from core.surfaces.conversations import ConversationStore
    path = _legacy_store(tmp_path)
    store = ConversationStore(str(path))              # reopen: migration runs
    convs = store.list("rob")
    assert len(convs) == 1, f"three spellings must merge, got {[c['address'] for c in convs]}"
    assert convs[0]["address"] == "thepublicden"
    history = store.history("rob", "telegram", "@thepublicden", limit=10)
    assert len(history) == 3, "no message may be lost in the merge"
    assert store.outbound_count_since("rob", "telegram", "t.me/thepublicden", 10**12) == 3


def test_the_merge_is_idempotent(tmp_path):
    from core.surfaces.conversations import ConversationStore
    path = _legacy_store(tmp_path)
    ConversationStore(str(path))
    store = ConversationStore(str(path))
    assert len(store.list("rob")) == 1
    assert len(store.history("rob", "telegram", "thepublicden", limit=10)) == 3


def test_an_address_needing_no_change_is_untouched(tmp_path):
    from core.surfaces.conversations import ConversationStore
    path = tmp_path / "c.db"
    s1 = ConversationStore(str(path))
    s1.record_outbound("rob", "email", "owner@example.com", "hi")
    s2 = ConversationStore(str(path))
    assert [c["address"] for c in s2.list("rob")] == ["owner@example.com"]
