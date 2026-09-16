"""The `creations` status section (042b).

The gap it closes is the class the 2026-08-25 ledger incident made famous: the
agent did something DURABLE and no surface could show it back. A token deployed
last week existed only as a transaction hash in a chat message.

It derives from the spend ledger rather than a second store — every creating
verb already calls `gate.record(counterparty=<the address it made>)`.
"""
import json
import os
import sqlite3

import pytest

from core.status_snapshot import (
    CREATION_VERBS, SEVERITY_WARN, STATE_UNAVAILABLE, _creations_section)


def _db(tmp_path, rows):
    path = os.path.join(str(tmp_path), "telemetry_events.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE telemetry_events (id INTEGER PRIMARY KEY, ts REAL, "
                "kind TEXT, user_id TEXT, session_id TEXT, source TEXT, attrs TEXT)")
    for ts, kind, uid, attrs in rows:
        con.execute("INSERT INTO telemetry_events (ts, kind, user_id, session_id, "
                    "source, attrs) VALUES (?,?,?,'','wallet',?)",
                    (ts, kind, uid, attrs))
    con.commit()
    con.close()
    return path


def _spend(action, address, usd=1.0, tx="0xabc"):
    return json.dumps({"venue": "defi", "action": action,
                       "counterparty": address, "amount_usd": usd,
                       "result_ref": tx})


def test_an_absent_store_is_UNAVAILABLE_not_an_empty_list(tmp_path):
    """'I have created nothing' and 'I cannot see what I created' are different
    facts, and only one of them is reassuring."""
    from core.status_snapshot import _guarded
    sec = _guarded("creations", _creations_section, "owner", str(tmp_path))
    assert sec.state == STATE_UNAVAILABLE
    assert "not found" in (sec.reason or "")
    assert sec.lines == []


def test_a_store_with_no_creations_says_so(tmp_path):
    _db(tmp_path, [(1.0, "wallet_spend", "owner", _spend("swap", "0xpool"))])
    sec = _creations_section("owner", str(tmp_path))
    assert sec.lines == ["nothing deployed or launched"]
    assert sec.data["creations"] == []


@pytest.mark.parametrize("action", CREATION_VERBS)
def test_every_creating_verb_is_listed(action, tmp_path):
    _db(tmp_path, [(1.0, "wallet_spend", "owner", _spend(action, "0xmade"))])
    sec = _creations_section("owner", str(tmp_path))
    assert sec.data["creations"][0]["action"] == action
    assert sec.data["creations"][0]["address"] == "0xmade"


def test_ordinary_spends_are_not_creations(tmp_path):
    _db(tmp_path, [
        (1.0, "wallet_spend", "owner", _spend("swap", "0xpool")),
        (2.0, "wallet_spend", "owner", _spend("bridge", "0xdest")),
        (3.0, "wallet_spend", "owner", _spend("deploy_token", "0xtoken")),
    ])
    sec = _creations_section("owner", str(tmp_path))
    assert [c["action"] for c in sec.data["creations"]] == ["deploy_token"]


def test_it_is_tenant_scoped(tmp_path):
    _db(tmp_path, [
        (1.0, "wallet_spend", "owner", _spend("deploy_token", "0xmine")),
        (2.0, "wallet_spend", "someone-else", _spend("deploy_token", "0xtheirs")),
    ])
    sec = _creations_section("owner", str(tmp_path))
    assert [c["address"] for c in sec.data["creations"]] == ["0xmine"]


def test_newest_first_and_the_tail_is_counted(tmp_path):
    rows = [(float(i), "wallet_spend", "owner",
             _spend("deploy_token", f"0x{i:040x}")) for i in range(1, 9)]
    _db(tmp_path, rows)
    sec = _creations_section("owner", str(tmp_path))
    assert len(sec.data["creations"]) == 8
    assert sec.data["creations"][0]["address"] == f"0x{8:040x}"
    assert any("and 3 more" in line for line in sec.lines)


def test_an_unparseable_row_is_COUNTED_not_silently_dropped(tmp_path):
    """A row we cannot parse might be a creation. Dropping it quietly is how
    'nothing deployed' comes to mean 'I could not tell'."""
    _db(tmp_path, [
        (1.0, "wallet_spend", "owner", "{not json at all"),
        (2.0, "wallet_spend", "owner", _spend("deploy_token", "0xtoken")),
    ])
    sec = _creations_section("owner", str(tmp_path))
    assert sec.data["unreadable_rows"] == 1
    assert any("may be incomplete" in line for line in sec.lines)
    assert any(h.key == "creations_unreadable" and h.severity == SEVERITY_WARN
               for h in sec.health)


def test_nothing_readable_does_not_read_as_nothing_created(tmp_path):
    _db(tmp_path, [(1.0, "wallet_spend", "owner", "{broken")])
    sec = _creations_section("owner", str(tmp_path))
    assert "that could be read" in " ".join(sec.lines)


def test_the_section_is_in_the_pinned_order():
    from core.status_snapshot import SECTION_ORDER
    assert "creations" in SECTION_ORDER


def test_no_tenant_renders_unavailable():
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot("", include_money=False)
    assert snap.sections["creations"].state == STATE_UNAVAILABLE
    assert "no tenant" in (snap.sections["creations"].reason or "")


def test_every_creation_verb_is_a_REAL_action_name():
    """The literals here must stay tied to the verbs that own them — a renamed
    verb would otherwise leave this section quietly listing nothing."""
    import inspect

    from tools.defi.trade_tool import DefiTradeTool
    from tools.launchpad.tool import LaunchpadTool

    def _actions(cls, prefix=""):
        out = set()
        for name in dir(cls):
            if name.startswith("_"):
                continue
            member = inspect.getattr_static(cls, name)
            if hasattr(member, "action_info") or hasattr(member, "_description"):
                out.add(name)
        return out

    known = _actions(DefiTradeTool) | _actions(LaunchpadTool)
    missing = [v for v in CREATION_VERBS if v not in known]
    assert not missing, (
        f"CREATION_VERBS names {missing}, which no tool declares. The status "
        f"section would silently list nothing for them.")


def test_the_creating_verbs_are_the_ones_that_make_an_address():
    """A deliberate, readable inventory: these four create something durable
    the owner will later be asked about. `swap`/`bridge`/`wrap` move value that
    already exists and are not creations."""
    assert set(CREATION_VERBS) == {
        "deploy_token", "deploy_contract", "solana_deploy_token",
        "launchpad_launch"}
