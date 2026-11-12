"""116 W10.1: the audit recounts what the snapshot prints, with its own reads."""
import os
import sqlite3
import time

from core.status_audit import DIFFERS, NOT_CHECKED, OK, audit_status, render_audit
from tests.unit.core import test_status_snapshot as seeded

degraded = seeded.degraded


def test_a_seeded_home_agrees_with_its_stores(degraded):
    rows = {r["check"]: r for r in audit_status(seeded.OWNER, degraded)}
    assert rows["goals by status"]["result"] == OK, rows["goals by status"]
    assert rows["job runs by outcome (24h)"]["result"] == OK, rows["job runs by outcome (24h)"]
    assert rows["goal runs by outcome (24h)"]["result"] == OK
    assert rows["enabled jobs"]["result"] == OK
    assert rows["open positions"]["result"] == OK
    assert not [r for r in rows.values() if r["result"] == DIFFERS], rows
    # A store this seat does not have is named, never counted as agreement.
    assert rows["outbound pending rows and the oldest"]["result"] == NOT_CHECKED
    assert render_audit(list(rows.values()))[0].startswith("Status audit: 0 differ(s)")


def test_a_count_the_snapshot_gets_wrong_is_a_difference(degraded):
    from core.status_snapshot import build_status_snapshot
    snap = build_status_snapshot(seeded.OWNER, data_dir=degraded)
    snap.sections["work"].data["counts"] = {"blocked": 7}
    rows = audit_status(seeded.OWNER, degraded, snapshot=snap)
    bad = [r for r in rows if r["result"] == DIFFERS]
    assert [r["check"] for r in bad] == ["goals by status"]
    text = "\n".join(render_audit(rows))
    assert "DIFFERS  goals by status: status says {'blocked': 7}" in text


def test_the_audit_writes_nothing(degraded):
    before = {f: os.path.getmtime(os.path.join(degraded, f)) for f in os.listdir(degraded)
              if f.endswith(".db")}
    time.sleep(0.01)
    audit_status(seeded.OWNER, degraded)
    after = {f: os.path.getmtime(os.path.join(degraded, f)) for f in before}
    assert before == after
    for f in before:                                   # and it left no lock behind
        sqlite3.connect(os.path.join(degraded, f)).close()


def test_doctor_audit_status_prints_the_pairs_and_the_last_status(degraded, monkeypatch):
    from click.testing import CliRunner
    from cli.commands import doctor as doctor_mod
    from core.surfaces.owner_verb_log import save_last_status
    monkeypatch.setattr(doctor_mod, "_DOCTOR_SNAPSHOT_CACHE", None, raising=False)
    tenant = doctor_mod.build_doctor_snapshot()[0].user_id     # the admin seat's tenant
    save_last_status(tenant, "telegram", "Status:\nHealth: OK", data_dir=degraded)
    out = CliRunner().invoke(doctor_mod.doctor, ["--audit-status"])
    assert out.exit_code == 0, out.output
    assert "Status audit: 0 differ(s)" in out.output
    assert "Last /status sent on telegram" in out.output and "  | Health: OK" in out.output
