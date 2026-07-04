from agents.task.agent.core.construction import resolve_profile_overrides


def test_override_present_wins():
    out = resolve_profile_overrides(
        {"tool_calling_method": "json", "max_actions_per_step": 3},
        tool_calling_method="auto",
        max_actions_per_step=10,
        max_input_tokens=None,
        max_failures=5,
    )
    assert out == ("json", 3, None, 5)


def test_absent_keys_keep_current():
    out = resolve_profile_overrides(
        {},
        tool_calling_method="auto",
        max_actions_per_step=10,
        max_input_tokens=8000,
        max_failures=5,
    )
    assert out == ("auto", 10, 8000, 5)


def test_all_four_overridden():
    out = resolve_profile_overrides(
        {"tool_calling_method": "json", "max_actions_per_step": 2,
         "max_input_tokens": 4096, "max_failures": 9},
        tool_calling_method="auto", max_actions_per_step=10,
        max_input_tokens=None, max_failures=5,
    )
    assert out == ("json", 2, 4096, 9)
