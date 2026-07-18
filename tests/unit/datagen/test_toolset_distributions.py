"""Wave 2 Task 1 — toolset distributions + Bernoulli sampler."""
import random

import pytest

from datagen.toolset_distributions import (
    DISTRIBUTIONS,
    sample_toolsets,
    validate_distribution,
)


def test_known_distributions_present():
    assert {"default", "web_research", "browser", "minimal",
            "filesystem", "balanced"} <= set(DISTRIBUTIONS)


def test_sampling_is_deterministic_with_seed():
    a = sample_toolsets("balanced", random.Random(7))
    b = sample_toolsets("balanced", random.Random(7))
    assert a == b


def test_sampling_guarantees_at_least_one():
    # rng that always rolls above every percent → nothing sampled → fallback
    class _NeverRng:
        def random(self):
            return 0.9999

    tools = sample_toolsets("balanced", _NeverRng())
    assert len(tools) == 1


def test_hundred_percent_always_included():
    for _ in range(10):
        assert "web_fetch" in sample_toolsets("minimal", random.Random())


def test_unknown_distribution_raises():
    with pytest.raises(KeyError):
        sample_toolsets("nope", random.Random())


def test_validate_distribution_flags_unknown_ids():
    known = {"filesystem", "task", "web_fetch"}
    unknown = validate_distribution("browser", known)
    assert "browser_manager" in unknown
    assert validate_distribution("minimal", known) == []
