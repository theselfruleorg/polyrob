"""Golden tests for the shared plain-text view grammar helpers."""
from cli.ui import candy


def test_kv_lines_aligns_labels():
    out = candy.kv_lines([("model", "glm-5.2"), ("ctx", "42%")])
    assert out.splitlines() == ["  model  glm-5.2", "  ctx    42%"]


def test_table_lines_grid():
    out = candy.table_lines(["id", "state"], [["g1", "open"], ["g2", "done"]])
    assert out.splitlines() == ["  id  state", "  g1  open", "  g2  done"]


def test_table_lines_pads_short_rows():
    out = candy.table_lines(["a", "b"], [["only"]])
    assert "only" in out  # no crash; missing cell → ""


def test_status_line_uses_state_glyphs():
    assert candy.status_line("running", "fetch data") == "  ● fetch data"
    assert candy.status_line("done", "write file") == "  ✓ write file"
    assert candy.status_line("weird", "x") == "  · x"


def test_bullet_uses_theme_bullet():
    assert candy.bullet("alpha") == "  · alpha"


def test_empty_grammar():
    assert candy.empty("goals") == "  no goals yet"
    assert candy.empty("goals", "/autonomy shows loop state") == (
        "  no goals yet — /autonomy shows loop state"
    )
    assert candy.empty("skills match 'x'", yet=False) == "  no skills match 'x'"


def test_section():
    assert candy.section("cron") == "── cron"


def test_helpers_stringify_values():
    assert candy.kv_lines([("n", 3)]).endswith("3")
    assert "7" in candy.table_lines(["c"], [[7]])
