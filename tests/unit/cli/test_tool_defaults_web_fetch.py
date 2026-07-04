from agents.task.tool_defaults import server_default_tools, TOOLSETS


def test_server_default_has_web_fetch_not_browser():
	d = server_default_tools()
	assert "web_fetch" in d and "browser" not in d


def test_research_set_swapped():
	assert "web_fetch" in TOOLSETS["research"] and "browser" not in TOOLSETS["research"]


def test_full_set_swapped():
	assert "web_fetch" in TOOLSETS["full"] and "browser" not in TOOLSETS["full"]


def test_browser_set_keeps_browser():
	# interaction-oriented sets still offer the real browser
	assert "browser" in TOOLSETS["browser"]
	assert "browser" in TOOLSETS["development"]
