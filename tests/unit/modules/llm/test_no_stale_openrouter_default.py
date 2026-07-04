"""P0.7 ratchet: forbid re-introducing the stale grok-4.3 literal as
OpenRouterClient's fallback default.

Deliberately NARROW — this only forbids the one hardcoded literal that had
drifted from the registry (``modules/llm/llm_client_registry.DEFAULT_MODELS``).
It must NOT be widened into a blanket "no gpt-5 literal anywhere" check: several
other ``gpt-5``/model literals in this codebase (e.g.
``agents/task_agent_lite.py``'s ``_resolve_chat_runtime`` last-resort ladder,
``runtime_config.resolve_runtime_config``'s ``last_resort`` default, and the
registry tables themselves) are an INTENTIONAL, commented fallback chain, not
bugs — a broad ratchet would flag those legitimate sites.
"""

import pathlib

# tests/unit/modules/llm/<this file> -> repo root is 4 directory levels up
# (llm -> modules -> unit -> tests -> repo root). Assert-checked below so a
# future file move fails loudly with a clear message instead of a bare
# FileNotFoundError.
REPO = pathlib.Path(__file__).resolve().parents[4]
_TARGET = REPO / "modules/llm/openrouter_client.py"


def test_no_hardcoded_grok43_openrouter_default():
    assert _TARGET.is_file(), (
        f"expected {_TARGET} to exist — REPO root resolution "
        f"(parents[4] from {__file__}) looks wrong"
    )
    src = _TARGET.read_text()
    assert "'x-ai/grok-4.3'" not in src and '"x-ai/grok-4.3"' not in src, (
        "openrouter default must come from get_default_model('openrouter'), "
        "not a stale literal"
    )
