"""ops_alert.py refuses to re-send IDENTICAL content within a short window.

The 2026-08-30 self-inflicted double digest: the same `--send` command was
re-run "to check the exit code" and the owner got the report twice. The
scripts had no memory of what they had just sent. Now the last send is
recorded (sha of message + docs) in the caller's OWN state dir — never the
shared data home, which the service identities own — and a byte-identical
alert inside ``OPS_ALERT_DEDUP_SEC`` is refused unless ``--force``.
"""
import importlib.util
import json
import pathlib
import time


def _load():
    p = pathlib.Path("scripts/ops_alert.py")
    spec = importlib.util.spec_from_file_location("ops_alert_dedup_t", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPS_ALERT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()


def test_identical_resend_within_window_is_refused(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, tmp_path)
    m = _load()
    sent = []
    monkeypatch.setattr(m, "send", lambda text: sent.append(text) or True)
    assert m.main(["hello", "owner"]) == 0
    assert m.main(["hello", "owner"]) == 0          # exit 0: not an error, a held duplicate
    assert len(sent) == 1
    out = capsys.readouterr().out
    assert "duplicate" in out and "--force" in out
    # the state dir was created by the script; the data home was NOT written
    assert (tmp_path / "state" / "ops_alert.last").exists()
    assert not list((tmp_path / "data").iterdir())


def test_different_content_or_force_sends(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    m = _load()
    sent = []
    monkeypatch.setattr(m, "send", lambda text: sent.append(text) or True)
    assert m.main(["hello", "owner"]) == 0
    assert m.main(["hello", "again"]) == 0
    assert m.main(["hello", "again", "--force"]) == 0
    assert len(sent) == 3


def test_window_expiry_and_docs_participate(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("OPS_ALERT_DEDUP_SEC", "60")
    m = _load()
    sent = []
    monkeypatch.setattr(m, "send", lambda text: sent.append(text) or True)
    monkeypatch.setattr(m, "_attach_one", lambda path, render, caption: sent.append(("doc", path, caption)) or True)
    doc = tmp_path / "r.md"
    doc.write_text("# r")
    assert m.main(["report", "--doc", str(doc), "--render"]) == 0
    assert m.main(["report", "--doc", str(doc), "--render"]) == 0   # duplicate held
    assert len(sent) == 1
    assert m.main(["report"]) == 0                    # same gist, no doc → different content
    assert len(sent) == 2
    # only the LAST delivery is remembered (one slot): the doc send is now sendable again
    assert m.main(["report", "--doc", str(doc), "--render"]) == 0
    assert len(sent) == 3
    # age the marker past the window → sends again
    marker = tmp_path / "state" / "ops_alert.last"
    rec = json.loads(marker.read_text())
    rec["ts"] = time.time() - 61
    marker.write_text(json.dumps(rec))
    assert m.main(["report", "--doc", str(doc), "--render"]) == 0
    assert len(sent) == 4


def test_unreadable_marker_fails_open(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    m = _load()
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "ops_alert.last").write_text("{broken")
    sent = []
    monkeypatch.setattr(m, "send", lambda text: sent.append(text) or True)
    assert m.main(["hello"]) == 0
    assert len(sent) == 1                             # a broken marker never silences an alert


def test_failed_send_is_not_remembered(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    m = _load()
    calls = []
    monkeypatch.setattr(m, "send", lambda text: calls.append(text) or False)
    assert m.main(["hello"]) == 1
    monkeypatch.setattr(m, "send", lambda text: calls.append(text) or True)
    assert m.main(["hello"]) == 0                     # the retry of a FAILED send goes through
    assert len(calls) == 2


def test_paused_suppression_does_not_mark_sent(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    (tmp_path / "data" / "AUTONOMY_PAUSE.json").write_text(json.dumps({"paused": True, "scopes": ["all"]}))
    m = _load()
    assert m.main(["quiet"]) == 0
    assert not (tmp_path / "state" / "ops_alert.last").exists()


def test_same_doc_bytes_at_a_different_path_is_a_duplicate(tmp_path, monkeypatch):
    """The 2026-09-21 double digest: ``ops_digest.py --send`` writes its report
    to a FRESH tempfile every run, so a path-keyed marker never matched and a
    re-run re-sent the owner the same report. The key is the doc's BYTES; the
    path is only the fallback for a doc that cannot be read."""
    _env(monkeypatch, tmp_path)
    m = _load()
    sent = []
    monkeypatch.setattr(m, "send", lambda text: sent.append(text) or True)
    monkeypatch.setattr(m, "_attach_one", lambda path, render, caption: sent.append(("doc", path, caption)) or True)
    a = tmp_path / "a-digest.md"
    b = tmp_path / "b-digest.md"
    a.write_text("# same report")
    b.write_text("# same report")
    assert m.main(["Daily update", "--doc", str(a), "--render"]) == 0
    assert m.main(["Daily update", "--doc", str(b), "--render"]) == 0   # held: same bytes
    assert len(sent) == 1
    b.write_text("# a different report")
    assert m.main(["Daily update", "--doc", str(b), "--render"]) == 0   # sends: bytes differ
    assert len(sent) == 2
