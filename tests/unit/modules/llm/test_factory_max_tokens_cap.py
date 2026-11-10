"""connectivity F8 (prod review 2026-10-29): the factory handed every client the
model's whole completion ceiling (``max_tokens=384000`` for a flash model) and
logged it on every session. The request was still clamped per call by
``_adjust_max_tokens`` (``output_budget.output_token_cap``, prod cap 8,192), but
the client attribute and the log said 384000. The factory now resolves the SAME
per-request cap at the source, so the value it stores and logs is the value a
request can carry."""
from types import SimpleNamespace

import pytest

from modules.llm import llm_factory


@pytest.fixture
def registry(monkeypatch):
    models = {"big": 384000, "small": 4096}
    monkeypatch.setattr(llm_factory, "get_model_config",
                        lambda m: SimpleNamespace(max_completion_tokens=models[m])
                        if m in models else None)
    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)


def test_a_huge_model_ceiling_is_capped_at_the_per_request_cap(registry):
    from modules.llm.llm_client import _DEFAULT_OUTPUT_TOKEN_CAP
    assert llm_factory._get_model_max_tokens_from_registry("big") == \
        _DEFAULT_OUTPUT_TOKEN_CAP


def test_the_env_cap_is_the_same_one_the_request_uses(registry, monkeypatch):
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "8192")
    assert llm_factory._get_model_max_tokens_from_registry("big") == 8192


def test_a_small_model_keeps_its_own_ceiling(registry):
    assert llm_factory._get_model_max_tokens_from_registry("small") == 4096


def test_no_model_stays_unset():
    assert llm_factory._get_model_max_tokens_from_registry("") is None
