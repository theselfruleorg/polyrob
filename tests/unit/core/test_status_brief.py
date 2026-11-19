"""The chat `/status` is short; `/status all` is every section (2026-11-18)."""
from core import status_render as R
from core.status_types import SECTION_ORDER


def _snap(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    from core.status_snapshot import build_status_snapshot
    return build_status_snapshot("owner", data_dir=str(tmp_path / "data"),
                                 include_balances=False)


def test_the_brief_form_leads_with_the_same_health_and_names_the_whole(tmp_path, monkeypatch):
    snap = _snap(tmp_path, monkeypatch)
    full = R.render_status_text(snap).splitlines()
    brief = R.render_status_text(snap, brief=True).splitlines()
    assert brief[-1] == R.BRIEF_FOOTER and "/status all" in brief[-1]
    assert len(brief) < len(full) / 2
    # The pause line and every health ISSUE are the same lines in both forms.
    issues = [ln for ln in full if ln.startswith(("• ⛔", "• ⚠"))]
    assert brief[1] == full[1] and all(ln in brief for ln in issues)
    for name in R.BRIEF_SECTIONS:
        assert R.render_section_lines(snap, name)[0] in brief
    hidden = set(SECTION_ORDER) - set(R.BRIEF_SECTIONS)
    assert hidden and set(R.BRIEF_SECTIONS) <= set(SECTION_ORDER)
    assert R.render_section_lines(snap, "rules")[0] not in brief


def test_an_ok_headline_is_short_on_a_chat_seat_and_whole_elsewhere():
    from types import SimpleNamespace
    from core.status_types import OVERALL_OK
    snap = SimpleNamespace(overall=OVERALL_OK, health=[], unavailable_sources=[], sections={})
    assert R.health_headline(snap, brief=True) == "Health: OK — no issue found"
    assert "(checked:" in R.health_headline(snap)
