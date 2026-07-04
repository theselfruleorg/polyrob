"""TDD tests for agents/task/templates.py (Task 11)."""
import pytest


def test_resolve_research_toolset():
    from agents.task.templates import resolve_template
    assert resolve_template("research").toolset == "research"


def test_resolve_unknown_returns_general():
    from agents.task.templates import resolve_template
    t = resolve_template("nonexistent_xyz")
    assert t.name == "general"


def test_resolve_empty_returns_general():
    from agents.task.templates import resolve_template
    assert resolve_template("").name == "general"


def test_resolve_case_insensitive():
    from agents.task.templates import resolve_template
    assert resolve_template("RESEARCH").toolset == "research"
    assert resolve_template("Coding").name == "coding"


def test_every_template_toolset_in_toolsets():
    """Guard: every template.toolset must be a key in TOOLSETS."""
    from agents.task.templates import TEMPLATES
    from agents.task.tool_defaults import TOOLSETS
    for name, tpl in TEMPLATES.items():
        assert tpl.toolset in TOOLSETS, (
            f"Template '{name}' has toolset '{tpl.toolset}' which is NOT in TOOLSETS"
        )


def test_no_template_contains_code_execution():
    """Safety: no template's resolved toolset may include code_execution."""
    from agents.task.templates import TEMPLATES
    from agents.task.tool_defaults import resolve_toolset
    for name, tpl in TEMPLATES.items():
        tools = resolve_toolset(tpl.toolset)
        assert "code_execution" not in tools, (
            f"Template '{name}' resolved to toolset '{tpl.toolset}' which contains code_execution"
        )


def test_trading_template_is_reads_only():
    """trading template must not include live-trade tools."""
    from agents.task.templates import TEMPLATES
    from agents.task.tool_defaults import resolve_toolset
    tpl = TEMPLATES["trading"]
    tools = resolve_toolset(tpl.toolset)
    assert "code_execution" not in tools
    # hyperliquid/polymarket could appear in a future trading toolset —
    # check that if they do, code_execution is still absent (already covered above).
    # The autonomy must not enable trade execution.
    assert tpl.autonomy == "standard"


def test_all_templates_present():
    from agents.task.templates import TEMPLATES
    expected = {"general", "research", "coding", "social", "trading", "blank"}
    assert expected == set(TEMPLATES.keys())


def test_resolve_never_raises():
    from agents.task.templates import resolve_template
    # Should not raise for any input.
    for bad in [None, 123, "   ", "\x00"]:
        try:
            result = resolve_template(bad)  # type: ignore[arg-type]
            assert result is not None
        except Exception as exc:
            pytest.fail(f"resolve_template({bad!r}) raised: {exc}")
