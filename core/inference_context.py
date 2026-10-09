"""The authenticated inference principal; carries no credentials or store access."""
from contextlib import contextmanager
from contextvars import ContextVar

_USER = ContextVar('llm_inference_user', default=None)


def inference_user_id():
    return _USER.get()


@contextmanager
def inference_user(user_id):
    """Bind a session principal, inherited by its auxiliary tasks and workers."""
    token = _USER.set(user_id)
    try:
        yield
    finally:
        _USER.reset(token)
