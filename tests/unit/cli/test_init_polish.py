"""3.10 (O2/O3/O4, 2026-07-14 review) — init wizard polish regressions."""
import inspect
import re


def test_wizard_section_numbering_is_consistent():
    """O2: sections number 1/7..7/7 — no stale denominator, and every section
    (Owner pairing, and 062's "Reach me") is numbered like the rest."""
    import cli.commands.init as m
    src = inspect.getsource(m)
    denominators = set(re.findall(r"Section \d/(\d)", src))
    assert denominators == {"7"}, f"mixed wizard denominators: {denominators}"
    assert "Section 5/7: Owner pairing" in src


def test_closing_key_check_reads_all_env_layers():
    """O3: the closing 'no usable key' warning must consider every layer
    `polyrob run` honors (load_env), not just the file init wrote."""
    import cli.commands.init as m
    src = inspect.getsource(m.init_cmd.callback)
    assert "load_env" in src


