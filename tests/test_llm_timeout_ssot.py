"""Guard: the LLM request-timeout SSOT must stay consistent across the layer boundary.

llm_factory (L1, modules.llm) reads DEFAULT_REQUEST_TIMEOUT from LLMClient instead of
importing agents.task.constants (L2) — see test_import_layering. This test guards that
LLMClient.DEFAULT_REQUEST_TIMEOUT remains the exact mirror of
TimeoutConfig.LLM_REQUEST_TIMEOUT so the decoupling didn't silently change the timeout.
"""

from agents.task.constants import TimeoutConfig
from modules.llm.llm_client import LLMClient
from modules.llm.llm_factory import DEFAULT_REQUEST_TIMEOUT


def test_llm_request_timeout_mirror_is_consistent():
    assert LLMClient.DEFAULT_REQUEST_TIMEOUT == TimeoutConfig.LLM_REQUEST_TIMEOUT
    assert DEFAULT_REQUEST_TIMEOUT == TimeoutConfig.LLM_REQUEST_TIMEOUT
