"""Agent.__init__ must reference the module-level `os`, never a function-local import.

Regression: a nested `import os` inside __init__ made `os` a local everywhere in the
function (Python decides this at compile time), so the early `os.getenv("PROFILE_MEM")`
raised UnboundLocalError — crashing EVERY Agent construction (interactive + autonomous).
The block was gated on local_mode_enabled(), so it only fired in real (server/CLI) runs,
not in mocked unit tests."""
from agents.task.agent.service import Agent


def test_agent_init_does_not_shadow_os():
    code = Agent.__init__.__code__
    # If any `import os` / `os = ...` appears inside __init__, 'os' lands in co_varnames
    # (locals). The module-level `os` must be read as a global (co_names) instead.
    assert "os" not in code.co_varnames, (
        "Agent.__init__ has a function-local `os` (shadowing the module import) — "
        "this makes early os.* references raise UnboundLocalError."
    )
