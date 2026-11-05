"""One default for the browser context limit, in one place (core/config.py).

About 200 MB per context was measured, so 25 could not fit the unit's memory limit.
Three different defaults existed: the config field, the pool's dataclass, and a
getattr fallback.
"""
import inspect
from types import SimpleNamespace

from core.config import DEFAULT_MAX_BROWSER_CONTEXTS, ServerConfig
from tools.browser import browser_manager as bm
from tools.browser.browser_manager import BrowserManager, BrowserManagerConfig


def test_the_default_is_six():
    assert DEFAULT_MAX_BROWSER_CONTEXTS == 6


def test_every_default_reads_the_one_constant():
    assert ServerConfig.model_fields["max_browser_contexts"].default == DEFAULT_MAX_BROWSER_CONTEXTS
    assert BrowserManagerConfig().max_contexts == DEFAULT_MAX_BROWSER_CONTEXTS


def test_a_config_with_no_value_takes_the_one_default():
    manager = BrowserManager(config=SimpleNamespace())
    assert manager.browser_config.max_contexts == DEFAULT_MAX_BROWSER_CONTEXTS


def test_no_literal_default_stays_in_the_pool():
    source = inspect.getsource(bm)
    assert "'max_browser_contexts', 3" not in source
    assert "max_contexts: int = 25" not in source
