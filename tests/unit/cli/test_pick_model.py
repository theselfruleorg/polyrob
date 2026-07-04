"""Behavioral tests for cli/ui/pick.py::pick_model — the TTY-safe interactive model
picker. All tests inject input_fn/isatty_fn (no real TTY needed) and assert on the
returned (provider, model) tuple — never on source text/formatting.
"""
from cli.ui.pick import pick_model

ENV = {"OPENROUTER_API_KEY": "sk-or-" + "x" * 24}


def test_non_tty_returns_default_without_prompting():
    called = {"n": 0}

    def boom(*a):
        called["n"] += 1
        return "1"

    res = pick_model(ENV, non_tty_default=("openrouter", "z-ai/glm-5.2"),
                      input_fn=boom, isatty_fn=lambda: False)
    assert res == ("openrouter", "z-ai/glm-5.2")
    assert called["n"] == 0, "must not prompt when non-TTY"


def test_tty_number_selects_that_model():
    res = pick_model(ENV, input_fn=lambda *_: "1", isatty_fn=lambda: True)
    assert res and res[0] == "openrouter"


def test_blank_enter_keeps_default():
    res = pick_model(ENV, preselect=("openrouter", "z-ai/glm-5.2"),
                      input_fn=lambda *_: "", isatty_fn=lambda: True)
    assert res == ("openrouter", "z-ai/glm-5.2")


def test_q_cancels():
    assert pick_model(ENV, input_fn=lambda *_: "q", isatty_fn=lambda: True) is None


def test_no_models_returns_none_without_prompting():
    """No usable key at all -> None, and never even reaches the input_fn (regardless
    of isatty_fn) since there's nothing to pick from."""
    called = {"n": 0}

    def boom(*a):
        called["n"] += 1
        return "1"

    res = pick_model({}, input_fn=boom, isatty_fn=lambda: True)
    assert res is None
    assert called["n"] == 0


def test_custom_string_escape_returns_typed_values():
    responses = iter(["c", "my-provider", "my-model"])
    res = pick_model(ENV, input_fn=lambda *_: next(responses), isatty_fn=lambda: True)
    assert res == ("my-provider", "my-model")


def test_invalid_selection_returns_none():
    res = pick_model(ENV, input_fn=lambda *_: "not-a-number", isatty_fn=lambda: True)
    assert res is None


def test_blank_enter_uses_true_registry_default_not_registration_order():
    """Regression guard: for an OpenAI-only env, the registry's first-REGISTERED
    model ('gpt-5.1') is NOT the registry's flagged default ('gpt-5'). Blank-Enter
    (and by extension the star marker it mirrors) must resolve to the actual
    default, not silently fall back to whatever is first in registration order.
    """
    env = {"OPENAI_API_KEY": "sk-" + "x" * 24}
    res = pick_model(env, input_fn=lambda *_: "", isatty_fn=lambda: True)
    assert res == ("openai", "gpt-5")
