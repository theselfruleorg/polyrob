"""The LLM request time limit, read by the clients and the agent tier."""
from core.env import int_env

DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS = 120


def llm_request_timeout() -> int:
    """``LLM_REQUEST_TIMEOUT_SECONDS``: the limit of one LLM request."""
    return int_env("LLM_REQUEST_TIMEOUT_SECONDS", DEFAULT_LLM_REQUEST_TIMEOUT_SECONDS)
