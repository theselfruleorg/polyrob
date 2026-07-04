"""P9 pass-13 — ErrorRecoveryMixin split out of llm_runner.py."""


def test_agent_composes_error_recovery_mixin():
    from agents.task.agent.service import Agent
    from agents.task.agent.core.error_recovery import ErrorRecoveryMixin
    assert issubclass(Agent, ErrorRecoveryMixin)
    for m in ("_recover_from_error", "_handle_step_error", "_attempt_llm_fallback_in_handler",
              "_get_fallback_llm", "_emit_provider_failure_telemetry",
              "_emit_fallback_success_telemetry"):
        assert getattr(Agent, m).__qualname__.startswith("ErrorRecoveryMixin")


def test_error_recovery_module_imports_cleanly():
    import agents.task.agent.core.error_recovery as er
    assert er.ErrorRecoveryMixin is not None


def test_llm_runner_still_owns_invocation():
    from agents.task.agent.core.llm_runner import LLMRunnerMixin
    # invocation + validation stay in llm_runner
    for m in ("get_next_action", "_validate_model_output"):
        assert hasattr(LLMRunnerMixin, m)
