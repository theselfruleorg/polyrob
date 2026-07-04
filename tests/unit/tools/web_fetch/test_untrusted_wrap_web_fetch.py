from agents.task.agent.core.untrusted_wrap import is_untrusted_tool


def test_fetch_url_action_is_untrusted():
	# action-name match — lock it so a rename can't silently drop injection framing
	assert is_untrusted_tool("fetch_url", "web_fetch") is True


def test_web_fetch_namespace_is_untrusted():
	# namespace match (belt-and-suspenders for any future action under this tool)
	assert is_untrusted_tool("anything", "web_fetch") is True
