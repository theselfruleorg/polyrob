"""H7: ActionExecutionContext.role defaulted to 'orchestrator' (most-privileged). Any
caller that builds a context without an explicit role (e.g. the two context-less
fallback constructors in multi_act / execute_action) inherited full delegation
privilege — the opposite of secure-by-default. The default must be 'leaf'; delegation
is granted only when the role is set explicitly.
"""
from tools.controller.execution_context import ActionExecutionContext


def test_default_role_is_leaf():
    assert ActionExecutionContext().role == "leaf"


def test_orchestrator_role_is_still_explicitly_settable():
    assert ActionExecutionContext(role="orchestrator").role == "orchestrator"


def test_clone_preserves_explicit_role():
    ctx = ActionExecutionContext(role="orchestrator")
    assert ctx.clone().role == "orchestrator"
    assert ctx.clone(role="leaf").role == "leaf"
