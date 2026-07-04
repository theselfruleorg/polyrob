import dataclasses

from modules.llm.token_counter import TokenUsage
from core.seams import LLMUsage


def test_token_usage_fields_frozen():
    # Platform billing reads these names; freezing them is a contract.
    names = {f.name for f in dataclasses.fields(TokenUsage)}
    assert {"prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"} <= names


def test_llm_usage_shape_frozen():
    expected = {
        "user_id", "session_id", "agent_id", "model", "provider",
        "input_tokens", "output_tokens", "cached_tokens",
        "duration_seconds", "component", "purpose",
    }
    names = {f.name for f in dataclasses.fields(LLMUsage)}
    assert names == expected, f"LLMUsage shape changed: {names ^ expected}"


def test_llm_usage_constructs():
    u = LLMUsage(
        user_id="u", session_id="s", agent_id="a", model="m", provider="p",
        input_tokens=10, output_tokens=20, cached_tokens=0,
        duration_seconds=1.5, component="agent", purpose="next_action",
    )
    assert u.input_tokens == 10 and u.purpose == "next_action"


def test_core_seams_imports_no_platform_billing():
    # Run in a FRESH process: sys.modules is process-global, so a sibling test that
    # legitimately imports platform billing would otherwise false-trip this check.
    # Intent preserved: "importing core.seams must not pull in platform billing".
    import subprocess
    import sys
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    code = (
        "import core.seams, sys; "
        "leaked=[m for m in sys.modules "
        "if m.startswith(('modules.credits','modules.payments'))]; "
        "assert not leaked, 'core.seams pulled in platform billing: %r' % leaked"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=str(repo_root),
    )
    assert result.returncode == 0, result.stderr
