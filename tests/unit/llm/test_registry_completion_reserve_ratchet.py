"""P6 ratchet (context-usage audit 2026-08-15) — no registry row may declare a
max_completion_tokens that consumes (almost) its whole context window.

Five rows used to set reserve == window ("Can match context"), which clamped the
input budget to the 1,000-token floor and made those models thrash the >=95%
emergency prune permanently. The budget formula now caps the reserve (P1), but
the registry data itself must stay honest: max output is what the vendor
supports, never a copy of the window.
"""
from modules.llm.model_registry import get_all_models

# A row whose declared max output eats >=95% of its window leaves (at most) a
# floor-clamped input budget — always a data bug, never a real vendor spec.
_CEILING_RATIO = 0.95


def test_no_row_reserves_the_whole_window():
    offenders = []
    for cfg in get_all_models():
        window = cfg.context_window or 0
        reserve = cfg.max_completion_tokens or 0
        if window and reserve >= int(window * _CEILING_RATIO):
            offenders.append(f"{cfg.name}: reserve={reserve} window={window}")
    assert not offenders, (
        "registry rows with max_completion_tokens >= 95% of context_window "
        "(set the vendor's real max output — models.dev is the oracle):\n  "
        + "\n  ".join(offenders)
    )
