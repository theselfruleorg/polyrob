"""An unknown `claude*` id must keep its FAMILY and its window — F31 (2026-09-22).

The registry used to fall every unrecognised Claude id back to a single target,
`claude-sonnet-4-5`, a 200K row. So a model newer than this tree — and any id
carrying the `[1m]` long-context suffix — was sized, budgeted and truncated
against a 200K context it does not have, while an opus request silently became
a sonnet. The chain now mirrors the GLM/DeepSeek families.
"""

import pytest

from modules.llm.model_registry import get_model_config


@pytest.mark.parametrize(
    "requested,expected_name,expected_window",
    [
        # Newer than this tree: keep the tier, keep the 1M window.
        # (`claude-opus-5` itself became a REAL row on 2026-09-23 — F9 needed a
        # capability flag to hang on it — so the unknown-opus case is now 6.)
        ("claude-opus-6", "claude-opus-5", 1_000_000),
        ("claude-opus-5[1m]", "claude-opus-5", 1_000_000),
        ("claude-sonnet-4-7", "claude-sonnet-4-6", 1_000_000),
        ("claude-sonnet-9[1m]", "claude-sonnet-4-6", 1_000_000),
        ("claude-fable-6", "claude-fable-5", 1_000_000),
        # Haiku is a 200K tier — sizing it at 1M would be the same bug mirrored.
        ("claude-haiku-9", "claude-haiku-4-5", 200_000),
    ],
)
def test_unknown_claude_id_keeps_its_family(requested, expected_name, expected_window):
    config = get_model_config(requested)
    assert config is not None
    assert config.name == expected_name
    assert config.context_window == expected_window


@pytest.mark.parametrize("requested", ["claude", "claude-next", "claude-x[1m]"])
def test_bare_claude_defaults_to_a_1m_row(requested):
    config = get_model_config(requested)
    assert config is not None
    assert config.context_window >= 1_000_000


def test_known_ids_are_untouched():
    """The fallback must never shadow a real row or a real alias."""
    assert get_model_config("claude-sonnet-4-5").context_window == 200_000
    assert get_model_config("claude-opus-4-8").name == "claude-opus-4-8"
    assert get_model_config("claude-opus-5").name == "claude-opus-5"
    assert get_model_config("claude-3-5-haiku").name == "claude-haiku-4-5"
