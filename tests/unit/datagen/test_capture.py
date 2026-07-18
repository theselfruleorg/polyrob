"""Wave 1 Task 6 — opt-in run-end trajectory capture (TRAJECTORY_CAPTURE)."""
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from datagen.capture import maybe_capture, outcome_labels


@dataclass
class _FakeOutcome:
    session_id: str = "sess-c1"
    refusal: bool = False
    done_called: object = True
    blocked: bool = False
    steps: int = 2
    spend_usd: float = 0.1
    verified: str = "verified"
    all_actions_errored: bool = False


class _FakePM:
    def __init__(self, root):
        self.data_root = Path(root)

    def get_session_root(self, session_id, user_id=None):
        return self.data_root / (user_id or "anon") / "sessions" / session_id


def _mk_session(pm, user_id, session_id, *, origin="USER"):
    sdir = pm.get_session_root(session_id, user_id)
    hist = sdir / "memory" / "message_history.json"
    hist.parent.mkdir(parents=True, exist_ok=True)
    hist.write_text(json.dumps({
        "session_id": session_id, "saved_at": "t",
        "messages": [{"type": "HumanMessage", "content": "go",
                      "origin": origin}]}))
    return sdir


def test_outcome_labels_mapping():
    assert outcome_labels(_FakeOutcome())["outcome"] == "done"
    assert outcome_labels(_FakeOutcome(done_called=False,
                                       refusal=True))["outcome"] == "failed"
    assert outcome_labels(_FakeOutcome(done_called=None,
                                       blocked=True))["outcome"] == "partial"
    assert outcome_labels(_FakeOutcome(done_called=None))["outcome"] == "unknown"
    labels = outcome_labels(_FakeOutcome())
    assert labels["verified"] == "verified"
    assert labels["steps"] == 2
    assert labels["spend_usd"] == 0.1


def test_capture_disabled_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("TRAJECTORY_CAPTURE", raising=False)
    assert maybe_capture(None, _FakeOutcome(), user_id="u1") is None


def test_capture_writes_record(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_CAPTURE", "true")
    fake = _FakePM(tmp_path)
    monkeypatch.setattr("datagen.capture.pm", lambda: fake)
    _mk_session(fake, "u1", "sess-c1")

    path = maybe_capture(None, _FakeOutcome(), user_id="u1")
    assert path is not None and path.exists()
    payload = json.loads(path.read_text())
    assert payload["labels"]["outcome"] == "done"
    assert payload["provenance"]["source"] == "run"
    assert str(path).startswith(str(tmp_path / "datagen" / "captured" / "u1"))


def test_capture_skips_correspondent_taint(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_CAPTURE", "true")
    fake = _FakePM(tmp_path)
    monkeypatch.setattr("datagen.capture.pm", lambda: fake)
    _mk_session(fake, "u1", "sess-c1", origin="CORRESPONDENT")
    assert maybe_capture(None, _FakeOutcome(), user_id="u1") is None


def test_capture_is_fail_open(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_CAPTURE", "true")
    fake = _FakePM(tmp_path)
    monkeypatch.setattr("datagen.capture.pm", lambda: fake)
    _mk_session(fake, "u1", "sess-c1")

    def boom(*a, **k):
        raise RuntimeError("assemble exploded")

    monkeypatch.setattr("datagen.capture.assemble_record", boom)
    assert maybe_capture(None, _FakeOutcome(), user_id="u1") is None


def test_capture_no_session_id(monkeypatch):
    monkeypatch.setenv("TRAJECTORY_CAPTURE", "true")
    assert maybe_capture(None, _FakeOutcome(session_id=None),
                         user_id="u1") is None
