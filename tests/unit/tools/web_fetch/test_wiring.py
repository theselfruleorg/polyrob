def test_descriptor_registered():
	from tools.descriptors import TOOL_DESCRIPTORS
	assert "web_fetch" in TOOL_DESCRIPTORS
	assert TOOL_DESCRIPTORS["web_fetch"].tool_class is not None


def test_valid_tool_id():
	from agents.task.agent.skill_manager import VALID_TOOL_IDS
	assert "web_fetch" in VALID_TOOL_IDS


def test_cli_registerable():
	from core.bootstrap import cli_unavailable_tools
	assert cli_unavailable_tools(["web_fetch"]) == []
