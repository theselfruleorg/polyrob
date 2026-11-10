"""113 W1: the one chat model — the store columns and ``core/surfaces/chats.py``."""
import sqlite3
import time

import pytest

from core.surfaces import chats
from core.surfaces.session_chat_registry import (
    SessionChatRegistry, chat_session_key, split_chat_key)

BASE_A = "agent:main:telegram:dm:100"
BASE_B = "agent:main:telegram:dm:200"


@pytest.fixture
def reg(tmp_path):
    return SessionChatRegistry(str(tmp_path / "surfaces.db"))


def _verb(reg, verb, *args, base=BASE_A, user="u_a", chat="100"):
    return chats.chat_verb(reg, verb, list(args), base_key=base, user_id=user,
                           surface_id="telegram", chat_id=chat)


def _focused(reg, base):
    return [r["session_key"] for r in reg.chat_rows_for_base(base)
            if r["focused"] and r["closed_at"] is None]


def test_chat_part_sits_before_the_thread_part():
    key = chat_session_key("agent:main:telegram:group:-5:thread:9", "c2")
    assert key == "agent:main:telegram:group:-5:chat:c2:thread:9"
    assert split_chat_key(key) == ("agent:main:telegram:group:-5:thread:9", "c2")
    assert chat_session_key(BASE_A, "main") == BASE_A
    assert split_chat_key(BASE_A) == (BASE_A, "main")


def test_a_chat_id_that_reads_chat_is_not_a_slug():
    key = "agent:main:slack:dm:chat:u_1"
    assert split_chat_key(key) == (key, "main")


def test_rows_written_before_the_columns_become_focused_main_chats(tmp_path):
    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE session_chat_map (session_key TEXT PRIMARY KEY, "
                 "session_id TEXT NOT NULL, user_id TEXT, surface_id TEXT, chat_id TEXT, "
                 "owner_pid INTEGER, updated_at REAL DEFAULT (strftime('%s','now')))")
    conn.execute("INSERT INTO session_chat_map (session_key, session_id, user_id, "
                 "surface_id, chat_id, updated_at) VALUES (?, 's1', 'u_a', 'telegram', '100', 5)",
                 (BASE_A,))
    conn.commit()
    conn.close()
    reg = SessionChatRegistry(db)
    SessionChatRegistry(db)   # a second start changes nothing
    row = reg.chat_row(BASE_A)
    assert (row["base_key"], row["slug"], row["focused"], row["created_at"]) == (BASE_A, "main", 1, 5)
    assert reg.resolve(BASE_A)["session_id"] == "s1"


def test_new_opens_a_focused_chat_with_no_session(reg):
    reg.bind(BASE_A, "s1", "u_a", "telegram", "100")
    out = _verb(reg, "/new", "tax", "letter")
    key = BASE_A + ":chat:c2"
    assert out["changed"] and out["focused_key"] == key
    assert _focused(reg, BASE_A) == [key]
    assert reg.chat_row(key)["label"] == "tax letter"
    assert reg.resolve(key) is None            # its first line starts the session
    assert chats.focused_key(reg, BASE_A, "u_a") == key
    reg.bind(key, "s2", "u_a", "telegram", "100")
    assert reg.resolve(key)["session_id"] == "s2"
    assert reg.chat_row(key)["label"] == "tax letter" and _focused(reg, BASE_A) == [key]


def test_one_focused_chat_per_seat_through_every_verb(reg):
    reg.bind(BASE_A, "s1", "u_a", "telegram", "100")
    _verb(reg, "/new", "one")
    _verb(reg, "/new", "two")
    assert _focused(reg, BASE_A) == [BASE_A + ":chat:c3"]
    assert _verb(reg, "/chat", "main")["focused_key"] == BASE_A
    assert _focused(reg, BASE_A) == [BASE_A]
    assert _verb(reg, "/chat", "tw")["focused_key"] == BASE_A + ":chat:c3"
    out = _verb(reg, "/close")
    assert out["closed_key"] == BASE_A + ":chat:c3" and out["focused_key"] == BASE_A
    assert _focused(reg, BASE_A) == [BASE_A]
    assert "stays open" in _verb(reg, "/close")["text"]      # main is never closed


def test_main_is_the_default_before_any_row_exists(reg):
    assert chats.focused_key(reg, BASE_A, "u_a") == BASE_A
    text = _verb(reg, "/chats")["text"]
    assert "1. main" in text and "you are here" in text
    _verb(reg, "/new")
    assert _verb(reg, "/chat", "main")["focused_key"] == BASE_A
    reg.bind(BASE_A, "s1", "u_a", "telegram", "100")
    assert _focused(reg, BASE_A) == [BASE_A]


def test_a_number_is_the_position_in_the_list(reg):
    reg.bind(BASE_A, "s1", "u_a", "telegram", "100")
    _verb(reg, "/new", "alpha")
    assert _verb(reg, "/chat", "1")["focused_key"] == BASE_A
    assert _verb(reg, "/chat", "2")["focused_key"] == BASE_A + ":chat:c2"
    assert "No chat number 9" in _verb(reg, "/chat", "9")["text"]
    assert "No chat named" in _verb(reg, "/chat", "zzz")["text"]


def test_rename_names_the_focused_chat(reg):
    reg.bind(BASE_A, "s1", "u_a", "telegram", "100")
    assert _verb(reg, "/rename", "home\nbase")["changed"]
    assert reg.chat_row(BASE_A)["label"] == "home base"
    assert not _verb(reg, "/rename")["changed"]


def test_two_tenants_never_see_or_switch_each_others_chats(reg):
    reg.bind(BASE_A, "sa", "u_a", "telegram", "100")
    reg.bind(BASE_B, "sb", "u_b", "telegram", "200")
    _verb(reg, "/new", "secret", base=BASE_A, user="u_a", chat="100")
    _verb(reg, "/new", "secret", base=BASE_B, user="u_b", chat="200")
    a = chats.list_chats(reg, user_id="u_a")["chats"]
    assert {c["base_key"] for c in a} == {BASE_A} and len(a) == 2
    assert "sb" not in {c["session_id"] for c in a}
    # tenant B names tenant A's seat key: nothing resolves, nothing changes
    before = _focused(reg, BASE_A)
    out = _verb(reg, "/chat", "secret", base=BASE_A, user="u_b", chat="200")
    assert not out["changed"] and "No chat named" in out["text"]
    assert not _verb(reg, "/close", "c2", base=BASE_A, user="u_b")["changed"]
    assert not _verb(reg, "/rename", "mine", base=BASE_A, user="u_b")["changed"]
    assert _focused(reg, BASE_A) == before
    assert reg.chat_row(BASE_A + ":chat:c2")["label"] == "secret"
    assert chats.focused_key(reg, BASE_A, "u_b") == BASE_A
    assert "secret" not in _verb(reg, "/chats", base="agent:main:repl:dm:x", user="u_c")["text"]


def test_other_seats_of_the_same_tenant_are_listed_by_seat(reg):
    reg.bind(BASE_A, "sa", "u_a", "telegram", "100")
    reg.bind("agent:main:repl:dm:local", "sr", "u_a", "repl", "local")
    text = _verb(reg, "/chats")["text"]
    assert "On your other seats:" in text and "the terminal" in text


def test_groups_and_admin_chats_are_not_a_persons_chats(reg):
    reg.bind("agent:main:telegram:group:-5", "sg", "u_a", "telegram", "-5")
    assert chats.list_chats(reg, user_id="u_a")["chats"] == []


def test_an_unreadable_store_is_not_an_empty_list():
    class Broken:
        def chat_rows(self, **_):
            raise sqlite3.OperationalError("locked")
        chat_rows_for_base = chat_rows
    listing = chats.list_chats(Broken(), user_id="u_a")
    assert listing["chats"] is None and listing["error"] == "OperationalError"
    assert "could not read" in _verb(Broken(), "/chats")["text"]
    assert "could not read" in _verb(None, "/new")["text"]
    assert chats.focused_key(Broken(), BASE_A) == BASE_A      # a line is never dropped


def test_gc_keeps_named_and_separate_chats(reg):
    reg.bind(BASE_A, "s1", "u_a", "telegram", "100")
    reg.bind(BASE_B, "s2", "u_b", "telegram", "200")
    _verb(reg, "/new", "keep")
    _verb(reg, "/new", "drop")
    _verb(reg, "/close")
    _verb(reg, "/rename", "named main", base=BASE_B, user="u_b")
    conn = sqlite3.connect(reg.db_path)
    conn.execute("UPDATE session_chat_map SET updated_at = ?", (time.time() - 10_000,))
    conn.commit()
    conn.close()
    assert reg.purge_stale(100) == 1          # the unnamed main only
    left = {r["session_key"] for r in reg.chat_rows_for_base(BASE_A)} | {
        r["session_key"] for r in reg.chat_rows_for_base(BASE_B)}
    # the closed chat stays: a freed slug would give the next /new the dead chat's key
    assert left == {BASE_A + ":chat:c2", BASE_A + ":chat:c3", BASE_B}
    assert _verb(reg, "/new")["focused_key"] == BASE_A + ":chat:c4"


def test_the_five_chat_verbs_are_table_rows_and_a_room_runs_only_new():
    from core import verbs
    for name in chats.CHAT_VERBS:
        row = verbs.verb_for(name)
        assert row is not None and row.group == "talk" and not row.seats, name
        assert verbs.room_refused(name) is (name != "/new"), name
    assert verbs.verb_for("/sessions") is verbs.verb_for("/chats")     # its old name
    assert not (set(chats.CHAT_VERBS) & verbs.PALETTE_EXCLUDED)


def test_a_number_means_the_chat_the_list_shows_before_main_has_a_row(tmp_path):
    """Review 2026-11-10: `/new` as the first act leaves main with no row; the list
    shows it as 1, and `/chat 1` / `/close 1` took the NEXT chat."""
    reg = SessionChatRegistry(str(tmp_path / "surfaces.db"))
    new = _verb(reg, "/new", "Main street")
    key = new["focused_key"]
    listing = _verb(reg, "/chats")["text"]
    assert "1. main" in listing and "2. Main street" in listing
    assert "stays open" in _verb(reg, "/close", "1")["text"]        # main, never c2
    assert "stays open" in _verb(reg, "/close", "main")["text"]     # not the name prefix
    assert reg.chat_row(key)["closed_at"] is None
    assert _verb(reg, "/chat", "1")["focused_key"] == BASE_A
    assert _verb(reg, "/chat", "2")["focused_key"] == key
    _verb(reg, "/chat", "main")
    assert "no messages yet" in _verb(reg, "/rename", "home")["text"]   # main has no row
    assert _verb(reg, "/close", "2")["closed_key"] == key
