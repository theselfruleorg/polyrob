"""avatar/studio.html must load the SHARED engine, never inline a copy — otherwise
the studio could drift from avatar/mindprint.js (the SSOT for pixels)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_studio_loads_shared_engine_not_an_inline_copy():
    html = (ROOT / "avatar" / "studio.html").read_text(encoding="utf-8")
    assert 'src="mindprint.js"' in html          # loads the SSOT engine
    assert "class Mindprint" not in html          # no inline engine duplicate
    assert "Copy config JSON" in html             # the picker export the pipeline consumes
