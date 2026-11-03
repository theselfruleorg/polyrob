"""098 TS-20: inbound email is keyed by identity, on its own connection, with a
durable per-message state.

The defect: the poll fetched SEQUENCE numbers and marked ``\\Seen`` later on the
tool's SHARED connection. An expunge or a folder switch by the agent's legacy
verbs during the turn moved the mark onto another message; that message went
``\\Seen`` without a route and the UNSEEN poll never saw it again.
"""
import os

import pytest

from surfaces.email.dedup import MessageDedup
from surfaces.email.fetchers import ImapFetcher, ImapHandle
from surfaces.email.harness import EmailHarness
from surfaces.email.inbound_state import INBOUND, InboundState


def _raw(mid: str) -> bytes:
    return (f"Message-ID: <{mid}@x>\r\nFrom: a@b.c\r\nSubject: s-{mid}\r\n"
            f"Content-Type: text/plain\r\n\r\nbody {mid}\r\n").encode()


class FakeServer:
    """One IMAP account: folders with UID-keyed messages, sequence numbers are
    positions, ``expunge`` renumbers. Several connections share it."""

    def __init__(self):
        self.folders = {"INBOX": {"validity": "100", "msgs": []},
                        "[Gmail]/Sent Mail": {"validity": "200", "msgs": []}}
        self.next_uid = 1

    def add(self, folder, raw, seen=False):
        self.folders[folder]["msgs"].append(
            {"uid": self.next_uid, "raw": raw, "flags": {"\\Seen"} if seen else set()})
        self.next_uid += 1
        return self.next_uid - 1

    def flags(self, folder, uid):
        return next(m["flags"] for m in self.folders[folder]["msgs"] if m["uid"] == uid)


class FakeConn:
    def __init__(self, server):
        self.server = server
        self.selected = None

    def _box(self):
        return self.server.folders[self.selected]

    def select(self, folder):
        self.selected = folder
        return "OK", [str(len(self._box()["msgs"])).encode()]

    def response(self, code):
        return code, [self._box()["validity"].encode()]

    def search(self, charset, crit):
        nums = [str(i + 1).encode() for i, m in enumerate(self._box()["msgs"])
                if "\\Seen" not in m["flags"]]
        return "OK", [b" ".join(nums)]

    def store(self, num, op, flag):  # sequence-number STORE (legacy path)
        self._box()["msgs"][int(num) - 1]["flags"].add("\\Seen")
        return "OK", []

    def expunge(self):
        box = self._box()
        box["msgs"] = [m for m in box["msgs"] if "\\Deleted" not in m["flags"]]
        return "OK", []

    def uid(self, cmd, *args):
        msgs = self._box()["msgs"]
        if cmd == "SEARCH":
            return "OK", [b" ".join(str(m["uid"]).encode() for m in msgs
                                    if "\\Seen" not in m["flags"])]
        uid = int(args[0])
        m = next((m for m in msgs if m["uid"] == uid), None)
        if cmd == "FETCH":
            if m is None:
                return "OK", [None]
            if m["raw"] is None:
                raise ValueError("malformed MIME")
            return "OK", [(b"1 (BODY[] {n}", m["raw"]), b")"]
        if cmd == "STORE":
            if m is not None:
                m["flags"].add("\\Seen")
            return "OK", []
        raise AssertionError(cmd)

    def logout(self):
        pass


class _Tool:
    def __init__(self, server):
        self.server = server
        self.imap_connection = FakeConn(server)  # the agent's legacy verbs' socket

    async def ensure_imap(self):
        return None

    def _mbox_open(self):
        return FakeConn(self.server)


@pytest.mark.asyncio
async def test_mark_uses_uid_not_sequence():
    srv = FakeServer()
    spam = srv.add("INBOX", _raw("spam"), seen=True)
    u5 = srv.add("INBOX", _raw("m5"))
    u6 = srv.add("INBOX", _raw("m6"))
    tool = _Tool(srv)
    f = ImapFetcher(tool)
    out = await f.fetch_unread()
    assert [h.uid for h, _ in out] == [str(u5).encode(), str(u6).encode()]
    # during the turn the agent deletes the spam on ITS connection: expunge
    legacy = tool.imap_connection
    legacy.select("INBOX")
    srv.folders["INBOX"]["msgs"][0]["flags"].add("\\Deleted")
    legacy.expunge()
    await f.mark_handled_async(out[0][0])
    assert "\\Seen" in srv.flags("INBOX", u5)
    assert "\\Seen" not in srv.flags("INBOX", u6)   # the neighbour stays unread
    assert spam  # (it was the one expunged)


@pytest.mark.asyncio
async def test_mark_after_folder_switch_still_flags_inbox_message():
    srv = FakeServer()
    srv.add("[Gmail]/Sent Mail", _raw("sent1"))
    u = srv.add("INBOX", _raw("m1"))
    tool = _Tool(srv)
    f = ImapFetcher(tool)
    (handle, _), = await f.fetch_unread()
    tool.imap_connection.select("[Gmail]/Sent Mail")   # the shared socket moves
    f._conn.select("[Gmail]/Sent Mail")                # even our own one moves
    await f.mark_handled_async(handle)
    assert "\\Seen" in srv.flags("INBOX", u)
    assert srv.folders["[Gmail]/Sent Mail"]["msgs"][0]["flags"] == set()


@pytest.mark.asyncio
async def test_uidvalidity_change_skips_mark():
    srv = FakeServer()
    u = srv.add("INBOX", _raw("m1"))
    f = ImapFetcher(_Tool(srv))
    (handle, _), = await f.fetch_unread()
    srv.folders["INBOX"]["validity"] = "101"     # mailbox rebuilt: UIDs are new
    assert await f.mark_handled_async(handle) is False
    assert "\\Seen" not in srv.flags("INBOX", u)


def _harness(tmp_path, fetcher):
    h = EmailHarness.__new__(EmailHarness)
    h.container = None
    h.task_agent = None
    h.email_tool = None
    h.poll_interval = 60.0
    db = os.path.join(str(tmp_path), "email_dedup.db")
    h.dedup = MessageDedup(db)
    h.inbound_state = InboundState(db)
    h.fetcher = fetcher
    h.user_directory = object()
    h.surface = None
    h._stop = False
    h._attempts = {}
    return h


@pytest.fixture
def notices(monkeypatch):
    sent = []

    async def _deliver(container, uid, text, **kw):
        sent.append((uid, text, kw.get("source")))

    import core.instance
    import core.surfaces.user_delivery as ud
    monkeypatch.setattr(ud, "deliver_user_message", _deliver)
    monkeypatch.setattr(core.instance, "resolve_owner_user_id", lambda: "owner1")
    return sent


@pytest.mark.asyncio
async def test_poison_counter_survives_restart(tmp_path, monkeypatch, notices):
    srv = FakeServer()
    u = srv.add("INBOX", _raw("p1"))

    async def _boom(*a, **kw):
        raise RuntimeError("router down")

    import surfaces.email.harness as eh
    monkeypatch.setattr(eh, "process_email", _boom)
    h1 = _harness(tmp_path, ImapFetcher(_Tool(srv)))
    await h1.poll_once()
    await h1.poll_once()
    assert h1.inbound_state.get("<p1@x>")["attempts"] == 2
    # restart: a new harness on the same DB continues from 2
    h2 = _harness(tmp_path, ImapFetcher(_Tool(srv)))
    await h2.poll_once()
    row = h2.inbound_state.get("<p1@x>")
    assert row["state"] == "quarantined" and row["attempts"] == 3
    assert "\\Seen" in srv.flags("INBOX", u)
    assert len(notices) == 1 and "s-p1" in notices[0][1]
    await h2.poll_once()      # quarantined: no second notice
    assert len(notices) == 1


@pytest.mark.asyncio
async def test_fetch_failure_quarantines_after_n_and_notifies(tmp_path, notices):
    srv = FakeServer()
    bad = srv.add("INBOX", None)     # FETCH raises for this one
    h = _harness(tmp_path, ImapFetcher(_Tool(srv)))
    for _ in range(2):
        await h.poll_once()
        assert "\\Seen" not in srv.flags("INBOX", bad)
    await h.poll_once()
    assert "\\Seen" in srv.flags("INBOX", bad)
    assert h.inbound_state.get(f"uid:100:{bad}")["state"] == "quarantined"
    assert len(notices) == 1 and notices[0][0] == "owner1"


@pytest.mark.asyncio
async def test_routed_message_is_not_routed_twice_after_a_lost_mark(tmp_path, monkeypatch):
    srv = FakeServer()
    u = srv.add("INBOX", _raw("r1"))
    calls = []

    async def _ok(*a, **kw):
        return object()

    async def _act(*a, **kw):
        calls.append(1)

    import surfaces._actor as actor
    import surfaces.email.harness as eh
    monkeypatch.setattr(eh, "process_email", _ok)
    monkeypatch.setattr(actor, "act_on_inbound", _act)
    f = ImapFetcher(_Tool(srv))
    h = _harness(tmp_path, f)

    async def _lost(handle):
        return False
    f.mark_handled_async = _lost        # the mark is lost (crash / socket drop)
    assert await h.poll_once() == 1
    # simulate a crash between routed and handled
    import sqlite3
    c = sqlite3.connect(h.inbound_state.db_path)
    c.execute("UPDATE inbound_state SET state='routed' WHERE key='<r1@x>'")
    c.commit()
    c.close()
    del f.mark_handled_async
    assert await h.poll_once() == 0
    assert calls == [1]
    assert h.inbound_state.get("<r1@x>")["state"] == "handled"
    assert "\\Seen" in srv.flags("INBOX", u)


def test_machine_table_is_well_formed():
    assert INBOUND.validate() == []
    assert not INBOUND.allowed("quarantined", "fetched")
    assert not INBOUND.allowed("handled", "routed")
