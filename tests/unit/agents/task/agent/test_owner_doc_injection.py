"""Owner-facts doc joins the frozen SELF_CONTEXT foundation message when present."""
from core.instance import load_owner_doc
from core.owner_doc_writer import OwnerDocWriter


def test_load_owner_doc_active_roundtrip(tmp_path):
    w = OwnerDocWriter(tmp_path)
    w.propose("Owner name: Alex. Timezone: America/Toronto.",
              user_id="u1", created_by="user", pending=False)
    text = load_owner_doc(tmp_path, "u1")
    assert "Alex" in text and "America/Toronto" in text


def test_construction_combines_owner_doc(tmp_path):
    # Mirror construction.py's _combined join to prove owner facts land between
    # SOUL and SELF, with the "Owner facts" header, only when present.
    w = OwnerDocWriter(tmp_path)
    w.propose("Owner prefers metric units.", user_id="u1", created_by="user", pending=False)
    _soul = "SOUL: I am Rob."
    _self_doc = "SELF: I learned X."
    _owner_doc = load_owner_doc(tmp_path, "u1")
    if _owner_doc:
        _owner_doc = "## Owner facts\n\n" + _owner_doc
    combined = "\n\n".join(p for p in ("", _soul, _owner_doc, _self_doc) if p)
    assert combined.index(_soul) < combined.index("Owner facts")
    assert combined.index("Owner facts") < combined.index(_self_doc)
    assert "metric units" in combined


def test_absent_owner_doc_is_omitted(tmp_path):
    _owner_doc = load_owner_doc(tmp_path, "u1")  # no doc written
    assert _owner_doc == ""
    combined = "\n\n".join(p for p in ("", "SOUL", _owner_doc, "SELF") if p)
    assert combined == "SOUL\n\nSELF"  # owner slot contributes nothing
