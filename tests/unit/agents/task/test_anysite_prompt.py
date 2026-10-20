from agents.task.agent.prompts import SystemPrompt


def test_no_anysite_block_in_the_prompt():
    """The anysite usage lives in the tool's own description, not the core prompt."""
    sp = SystemPrompt(action_description="", use_native_tools=True,
                      include_browser_tools=False, tool_ids=["anysite"])
    assert "<anysite>" not in sp.get_system_message().content


def test_using_your_tools_principle_always_present():
    """Gap-2: the config-awareness principle is static + always present (no tool_ids
    needed), so the agent reasons from its actual tools instead of the filesystem."""
    sp = SystemPrompt(action_description="", use_native_tools=True,
                      include_browser_tools=False)
    section = sp._get_tools_section()
    assert "<using-your-tools>" in section
    assert "goal_list" in section  # steers status questions to the tool, not the FS
    assert sp._get_tools_section() == section  # cache-stable, no interpolation
