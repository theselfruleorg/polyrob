"""Regression: _get_vision_support must not crash on the first multimodal call.

`_supports_vision_cached` was assigned only in dead code (after a `return`) and was
never declared as a PrivateAttr, so reading it raised AttributeError on Pydantic v2 —
crashing the first vision/multimodal request on every provider adapter.
"""
from unittest.mock import MagicMock

import pytest

from modules.llm.adapters import LLMClientAdapter


def _client(model_type="gpt-4o", supports_vision=True):
    c = MagicMock()
    c.model_type = model_type
    c.supports_vision = supports_vision
    return c


def test_get_vision_support_no_attribute_error_cold():
    a = LLMClientAdapter(client=_client())
    # Must not raise AttributeError on the undeclared/unset private attr.
    assert a._get_vision_support() in (True, False)


def test_get_vision_support_caches_value():
    a = LLMClientAdapter(client=_client(model_type="unknown-model", supports_vision=True))
    first = a._get_vision_support()
    assert a._supports_vision_cached is not None
    assert a._get_vision_support() == first


def test_private_attr_declared_default_none():
    a = LLMClientAdapter(client=_client())
    # Reading before any call returns the declared PrivateAttr default, not a crash.
    assert a._supports_vision_cached is None
