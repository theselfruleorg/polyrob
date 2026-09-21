"""057 WS-D — a room's LABEL is not its NAME (core/surfaces/room_label.py).

`group_allowlist.note` is what the owner typed after `groups allow` — the CLI
calls it "Label". Three renderers promoted it to the room's quoted TITLE, so a
reminder note came back months later looking like the room's actual name. The
09-19 "fix" was a data edit; this is the code rule.
"""
import time

from core.surfaces.group_allowlist import GroupAllowlist
from core.surfaces.room_label import render_room_label, render_room_line, room_name


class _Pol:
    def __init__(self, name=""):
        self.name = name


def _row(**kw):
    base = {"surface": "telegram", "chat_id": "-100123", "note": "",
            "status": "active", "created_at": None, "updated_at": None}
    base.update(kw)
    return base


def test_name_comes_from_chat_policy_only():
    row = _row(note="the den, ask before posting")
    assert room_name(row, _Pol("The Public Den")) == "The Public Den"
    # no chat.name -> the address, which is always true; NEVER the note
    assert room_name(row, _Pol("")) == "telegram:-100123"
    assert room_name(row, None) == "telegram:-100123"


def test_label_is_labelled_and_dated():
    ts = time.mktime((2026, 9, 19, 12, 0, 0, 0, 0, 0))
    out = render_room_label(_row(note="ask before posting", created_at=ts))
    assert out.startswith('label: "ask before posting" (set 2026-09-')


def test_updated_at_wins_over_created_at():
    old = time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, 0))
    new = time.mktime((2026, 9, 19, 12, 0, 0, 0, 0, 0))
    out = render_room_label(_row(note="n", created_at=old, updated_at=new))
    assert "2026-09-" in out and "2026-01-" not in out


def test_unknown_timestamp_renders_no_date_rather_than_today():
    out = render_room_label(_row(note="n", created_at=None))
    assert out == 'label: "n"'
    assert "set " not in out


def test_no_note_renders_nothing():
    assert render_room_label(_row()) == ""
    assert render_room_label(_row(note="   ")) == ""
    assert " — " not in render_room_line(_row(), _Pol("X"))


def test_room_line_never_puts_the_note_in_title_position():
    ts = time.mktime((2026, 9, 19, 12, 0, 0, 0, 0, 0))
    line = render_room_line(_row(note="the den", created_at=ts), _Pol(""),
                            "mode=mention")
    assert line.startswith('telegram:-100123 "telegram:-100123"')
    assert line.index('label: "the den"') > line.index('"telegram:-100123"')
    assert line.endswith("mode=mention")


def test_allowlist_carries_updated_at(tmp_path):
    db = str(tmp_path / "g.db")
    a = GroupAllowlist(db)
    a.allow("telegram", "-100123", "first label")
    first = a.list_all()[0]
    assert first["updated_at"] is not None
    time.sleep(0.01)
    a.allow("telegram", "-100123", "second label")     # re-allow rewrites the note
    second = a.list_all()[0]
    assert second["note"] == "second label"
    assert second["updated_at"] > first["updated_at"]
    assert second["created_at"] == first["created_at"]


def test_alter_is_idempotent_on_a_reopened_store(tmp_path):
    db = str(tmp_path / "g.db")
    GroupAllowlist(db).allow("telegram", "-1", "n")
    again = GroupAllowlist(db)          # ALTER runs a second time; must not raise
    assert again.is_allowed("telegram", "-1")
