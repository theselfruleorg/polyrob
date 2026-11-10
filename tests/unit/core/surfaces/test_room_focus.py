"""A corrupt focus or links file must not be rewritten whole: a read of it as empty
followed by a write erased every other principal's pointer (or every other pair)."""
from core import pending_links
from core.surfaces import room_focus
from core.surfaces.room_migrate import _move_focus


def test_a_missing_focus_file_is_empty_and_writable(tmp_path):
    assert room_focus.get(str(tmp_path), "owner1") is None
    assert room_focus.set(str(tmp_path), "owner1", "telegram", "-1")
    assert room_focus.get(str(tmp_path), "owner1") == ("telegram", "-1")


def test_a_corrupt_focus_file_is_read_as_unset_then_set_aside_on_a_write(tmp_path):
    """A write over a corrupt file moves it aside as .corrupt-<epoch> (its pointers
    kept for a person to read) and writes the new document: a write that failed for
    ever answered "unwritable data home?", the wrong reason."""
    path = tmp_path / "room_focus.json"
    path.write_text('{"admin2": {"surface": "telegram", "chat_id": "-5"', encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    assert room_focus.get(str(tmp_path), "admin2") is None
    assert room_focus.set(str(tmp_path), "owner1", "telegram", "-1") is True
    assert room_focus.get(str(tmp_path), "owner1") == ("telegram", "-1")
    kept = list(tmp_path.glob("room_focus.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == before


def test_an_unreadable_focus_file_is_never_rewritten(tmp_path, monkeypatch):
    """An OSError is not corruption: the file is neither moved nor rewritten."""
    path = tmp_path / "room_focus.json"
    path.write_text('{"admin2": {"surface": "telegram", "chat_id": "-5"}}', encoding="utf-8")
    real = type(path).read_text

    def _denied(self, *a, **k):
        if self.name == "room_focus.json":
            raise PermissionError("denied")
        return real(self, *a, **k)
    monkeypatch.setattr(type(path), "read_text", _denied)
    assert room_focus.set(str(tmp_path), "owner1", "telegram", "-1") is False
    assert room_focus.clear(str(tmp_path), "owner1") is False
    _move_focus(str(tmp_path), "telegram", "-5", "-6")
    monkeypatch.undo()
    assert room_focus.get(str(tmp_path), "admin2") == ("telegram", "-5")
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_a_corrupt_links_file_is_set_aside_and_the_new_pair_written(tmp_path, monkeypatch):
    path = tmp_path / ".pending_links.json"
    path.write_text("[not json", encoding="utf-8")
    monkeypatch.setattr(pending_links, "_path", lambda *a, **k: path)
    assert pending_links.partner("owner1", "skill", "1", home_dir=tmp_path,
                                 instance_id="i") is None
    pending_links.link("owner1", ("skill", "1"), ("owner_md", "2"), home_dir=tmp_path,
                       instance_id="i")
    assert pending_links.partner("owner1", "skill", "1", home_dir=tmp_path,
                                 instance_id="i") == ("owner_md", "2")
    kept = list(tmp_path.glob(".pending_links.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "[not json"
