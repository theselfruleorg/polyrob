"""VALID_TOOL_IDS must cover every registrable tool id (audit T5, 2026-07-16).

The literal went stale (missing shell/self_env/hf_deploy/github/x402_pay/process/
alchemy/collabland) so genuinely-registrable skill/session tool_ids were rejected
as invalid — e.g. a skill declaring ``triggers.tool_ids=['shell']`` was warned
away even though ``default_goal_tools()`` itself injects ``shell`` at posture>=1.

The registry side is partly env-gated (register_optional_tool runs behind flags),
so we assert BOTH directions we can: (a) whatever is registered under the current
env is covered, and (b) the full enumerated optional set is present explicitly.
"""


def test_registered_descriptors_covered():
    import tools  # noqa: F401 — triggers register_optional_tool side effects
    from tools.descriptors import TOOL_DESCRIPTORS, get_tool_display_name
    from agents.task.agent.skill_manager import VALID_TOOL_IDS

    registered = {get_tool_display_name(n) for n in TOOL_DESCRIPTORS}
    missing = registered - VALID_TOOL_IDS
    assert missing == set(), (
        f"registered tool ids missing from VALID_TOOL_IDS: {sorted(missing)}")


def test_known_optional_ids_present():
    """Flag-gated optional tools (not registered under the default test env) —
    enumerated from every register_optional_tool()/x402 register site."""
    from agents.task.agent.skill_manager import VALID_TOOL_IDS

    expected = {
        # tools/{shell,self_env,hf_deploy,github,coding,git,code_exec}/__init__.py,
        # tools/{goal,cronjob}_tools.py, tools/knowledge_ingest.py, tools/x402/__init__.py
        "shell", "process", "self_env", "hf_deploy", "github", "coding",
        "code_execution", "git", "goal", "cronjob", "knowledge",
        "x402_pay", "x402_invoice",
    }
    missing = expected - VALID_TOOL_IDS
    assert missing == set(), f"known optional tool ids missing: {sorted(missing)}"
