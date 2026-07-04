from agents.task.agent.prompts import _anysite_guidance_block


def test_anysite_guidance_teaches_cli_tool():
    block = _anysite_guidance_block()
    assert "anysite_api" in block
    assert "endpoint" in block
    # teaches breadth, not one endpoint
    assert "sources" in block or "endpoints" in block
    # no stale MCP-only verb
    assert "anysite_duckduckgo_search" not in block


def test_anysite_guidance_routes_data_retrieval_here(monkeypatch):
    """G2: the block must explicitly route Twitter/X + social/web *data retrieval*
    to anysite, leaving the native twitter tool for posting only. Locks the routing
    sentence so a future prompt refactor can't silently drop it."""
    block = _anysite_guidance_block().lower()
    assert "data retrieval" in block
    assert "twitter" in block
    # routes reads here; native twitter is for posting only
    assert "post" in block


def test_anysite_guidance_is_wired_into_tools_section(monkeypatch):
    monkeypatch.delenv("ANYSITE_TOOL_ENABLED", raising=False)  # default ON
    import importlib, tools.anysite
    importlib.reload(tools.anysite)
    from agents.task.agent.prompts import SystemPrompt
    # Block renders only when anysite is actually loaded THIS session.
    sp = SystemPrompt(action_description="", use_native_tools=True,
                      include_browser_tools=False, tool_ids=["anysite"])
    section = sp._get_tools_section()
    assert "<anysite>" in section
    assert "anysite_api" in section


def test_anysite_guidance_absent_when_not_loaded_even_if_flag_on(monkeypatch):
    """The core self-config-awareness fix: the flag being ON is NOT enough — if
    anysite isn't loaded this session, the prompt must NOT advertise it (else the
    agent reaches for a tool it cannot call)."""
    monkeypatch.delenv("ANYSITE_TOOL_ENABLED", raising=False)  # default ON
    import importlib, tools.anysite
    importlib.reload(tools.anysite)
    from agents.task.agent.prompts import SystemPrompt
    sp = SystemPrompt(action_description="", use_native_tools=True,
                      include_browser_tools=False, tool_ids=["twitter", "web_fetch"])
    assert "<anysite>" not in sp._get_tools_section()


def test_anysite_guidance_absent_when_disabled(monkeypatch):
    monkeypatch.setenv("ANYSITE_TOOL_ENABLED", "false")
    import importlib, tools.anysite
    importlib.reload(tools.anysite)
    from agents.task.agent.prompts import SystemPrompt
    sp = SystemPrompt(action_description="", use_native_tools=True,
                      include_browser_tools=False, tool_ids=["anysite"])
    assert "<anysite>" not in sp._get_tools_section()


def test_using_your_tools_principle_always_present():
    """Gap-2: the config-awareness principle is static + always present (no tool_ids
    needed), so the agent reasons from its actual tools instead of the filesystem."""
    from agents.task.agent.prompts import SystemPrompt
    sp = SystemPrompt(action_description="", use_native_tools=True,
                      include_browser_tools=False)
    section = sp._get_tools_section()
    assert "<using-your-tools>" in section
    assert "goal_list" in section  # steers status questions to the tool, not the FS
    assert sp._get_tools_section() == section  # cache-stable, no interpolation


def test_anysite_guidance_forbids_reading_schema_file_from_disk():
    # F4 (live-test): the agent burned steps trying to filesystem_read_file the
    # absolute ~/.anysite/schema.json (confined → refused). The guidance must
    # steer it away from that dead-end.
    from agents.task.agent.prompts import _anysite_guidance_block
    block = _anysite_guidance_block().lower()
    assert ".anysite/schema.json" in block
    assert "never read" in block and "anysite_api" in block
