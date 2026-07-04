def test_anysite_tool_registered():
    import tools  # triggers register_tool_class calls
    from tools.descriptors import get_tool_class
    cls = get_tool_class("anysite")
    assert cls is not None
    assert cls.__name__ == "AnysiteTool"


def test_anysite_in_cli_registerable():
    from core.bootstrap import cli_unavailable_tools
    # anysite must NOT be reported unavailable in the CLI
    assert "anysite" not in cli_unavailable_tools(["anysite"])
