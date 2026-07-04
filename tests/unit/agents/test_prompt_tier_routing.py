from agents.task.agent.prompts import SystemPrompt


def test_browser_section_teaches_tier_routing():
	# _get_browser_content uses no instance state — bare instance is fine.
	mgr = SystemPrompt.__new__(SystemPrompt)
	text = mgr._get_browser_content()
	assert "web_fetch" in text
	assert "fetch_url" in text
	assert "browser" in text  # still documents the interaction path
