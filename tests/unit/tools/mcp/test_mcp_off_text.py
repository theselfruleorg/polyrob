"""The MCP-off refusal renders the gate row's remedy TEXT, never its key.

Regression: 081 B-1 turned ``ToolGate.remedy`` into a key of
``core.tool_gates.REMEDIES``; the refusal still appended the raw field and the
agent read "...configured); mcp_servers".
"""
from core.tool_gates import REMEDIES
from tools.mcp.mcp_tool import _mcp_off_text


def test_mcp_off_text_is_rendered_prose_not_a_remedy_key():
    text = _mcp_off_text()
    tail = text.rsplit("); ", 1)[-1]
    assert tail not in REMEDIES
    assert "MCP_ENABLED" in tail and "/mcp add" in tail
