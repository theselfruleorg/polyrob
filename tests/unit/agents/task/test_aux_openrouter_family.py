"""B-2 (2026-10-14 review): AUX_AUTO resolves on openrouter by the MAIN model's
family, and H-MEM reflection never silently goes off for want of an aux model."""
import logging

import pytest

from agents.task.constants import resolve_aux_chain, resolve_aux_model


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("AUX_AUTO", "COMPACTION_AUTO_AUX", "DEFAULT_PROVIDER", "DEFAULT_MODEL",
                 "CHAT_PROVIDER", "CHAT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    for slot in ("COMPACTION", "JUDGE", "REFLECTION"):
        for prefix in ("AUX_MODEL_", "AUX_PROVIDER_", "AUX_FALLBACK_"):
            monkeypatch.delenv(prefix + slot, raising=False)


@pytest.mark.parametrize("main,expected", [
    ("z-ai/glm-5.3", "z-ai/glm-5.3-flash"),
    ("z-ai/glm-5.3-turbo", "z-ai/glm-5.3-flash"),
    ("z-ai/glm-5.3-flash", None),                 # already the cheap tier
    ("deepseek/deepseek-v4.1-flash", None),       # deepseek: aux = main
    ("x-ai/grok-4.3", None),
])
def test_openrouter_aux_keys_on_the_main_family(monkeypatch, main, expected):
    monkeypatch.setenv("AUX_AUTO", "true")
    assert resolve_aux_model("judge", provider="openrouter", main_model=main) == expected


def test_openrouter_cheap_model_stays_on_openrouter(monkeypatch):
    monkeypatch.setenv("AUX_AUTO", "true")
    chain = resolve_aux_chain("compaction", "openrouter", main_model="z-ai/glm-5.3")
    assert chain[0] == {"model": "z-ai/glm-5.3-flash", "provider": "openrouter"}


def test_no_main_model_reads_the_operator_pin(monkeypatch):
    monkeypatch.setenv("AUX_AUTO", "true")
    monkeypatch.setenv("DEFAULT_PROVIDER", "openrouter")
    monkeypatch.setenv("DEFAULT_MODEL", "z-ai/glm-5.3")
    assert resolve_aux_model("judge", provider="openrouter") == "z-ai/glm-5.3-flash"


def test_aux_auto_off_is_unchanged():
    assert resolve_aux_chain("judge", "openrouter", main_model="z-ai/glm-5.3") == []


def test_reflection_falls_back_to_the_main_model():
    chain = resolve_aux_chain("reflection", "openrouter",
                              main_model="deepseek/deepseek-v4.1-flash")
    assert chain == [{"model": "deepseek/deepseek-v4.1-flash", "provider": "openrouter"}]
    # the other slots keep "empty => the caller uses the main model"
    assert resolve_aux_chain("compaction", "openrouter",
                             main_model="deepseek/deepseek-v4.1-flash") == []


def test_reflection_with_no_model_logs_off_once(caplog):
    from modules.memory.task import reflection_service as rs
    rs._OFF_LOGGED = False
    svc = rs.ReflectionService(enabled=True, llm=None)
    with caplog.at_level(logging.WARNING, logger=rs.__name__):
        assert svc.consolidate(["a", "b"]) is None
        assert svc.consolidate(["c"]) is None
    assert sum("event=reflection_off" in r.message for r in caplog.records) == 1
