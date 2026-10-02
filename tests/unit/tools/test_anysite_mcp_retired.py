import json
import pathlib


def test_anysite_not_in_mcp_config():
    with open("config/mcp_config.json") as f:
        cfg = json.load(f)
    assert "anysite" not in cfg.get("servers", {})


def test_no_anysite_mcp_tool_name_in_constants():
    src = pathlib.Path("agents/task/constants.py").read_text()
    assert "anysite_duckduckgo_search" not in src
