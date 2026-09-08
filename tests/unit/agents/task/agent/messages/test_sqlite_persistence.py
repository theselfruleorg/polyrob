"""SqliteMessageStore: append + load round-trip, per-session, durable across instances."""
from agents.task.agent.messages.sqlite_persistence import SqliteMessageStore


def test_append_and_load_roundtrip(tmp_path):
    store = SqliteMessageStore(str(tmp_path / "msgs.db"))
    store.append("s1", {"type": "HumanMessage", "content": "hello"})
    store.append("s1", {"type": "AIMessage", "content": "hi", "tool_calls": []})
    rows = store.load("s1")
    assert [r["type"] for r in rows] == ["HumanMessage", "AIMessage"]
    assert rows[0]["content"] == "hello"


def test_isolation_by_session(tmp_path):
    store = SqliteMessageStore(str(tmp_path / "msgs.db"))
    store.append("s1", {"type": "HumanMessage", "content": "a"})
    store.append("s2", {"type": "HumanMessage", "content": "b"})
    assert len(store.load("s1")) == 1
    assert store.load("s2")[0]["content"] == "b"


def test_durable_across_instances(tmp_path):
    db = str(tmp_path / "msgs.db")
    SqliteMessageStore(db).append("s1", {"type": "HumanMessage", "content": "persisted"})
    rows = SqliteMessageStore(db).load("s1")   # fresh instance, same file
    assert rows[0]["content"] == "persisted"


def test_clear_session(tmp_path):
    store = SqliteMessageStore(str(tmp_path / "msgs.db"))
    store.append("s1", {"type": "HumanMessage", "content": "x"})
    store.clear("s1")
    assert store.load("s1") == []


def test_replace_all_atomic_swap_and_order(tmp_path):
    # MED-4: replace_all does DELETE + executemany in one txn; seq from enumerate.
    store = SqliteMessageStore(str(tmp_path / "m.db"))
    store.replace_all("s1", [{"i": 0}, {"i": 1}, {"i": 2}])
    assert store.load("s1") == [{"i": 0}, {"i": 1}, {"i": 2}]
    # A second replace_all fully supersedes the first (no PK collision, no leftovers).
    store.replace_all("s1", [{"i": 9}])
    assert store.load("s1") == [{"i": 9}]
    # Per-session isolation preserved.
    store.replace_all("s2", [{"x": 1}])
    assert store.load("s1") == [{"i": 9}]
    assert store.load("s2") == [{"x": 1}]


def test_replace_all_empty_clears(tmp_path):
    store = SqliteMessageStore(str(tmp_path / "m.db"))
    store.replace_all("s1", [{"a": 1}])
    store.replace_all("s1", [])
    assert store.load("s1") == []
