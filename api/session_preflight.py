"""Preflight checks for ``POST /api/task/sessions`` (070 W0.13).

Kept out of ``api/task_http_api.py``, which is at its file-size ceiling.
"""
import logging

logger = logging.getLogger(__name__)


def no_model(agent) -> bool:
    """True only when this process's container answers that it has NO ``llm``.

    The console process on prod (and at local posture) has no language model, so
    an auto-started session would be an empty chat that nothing answers. Any
    failure of the probe answers False: a failed probe never blocks a run.
    """
    try:
        container = getattr(agent, "container", None)
        get_service = getattr(container, "get_service", None)
        return container is not None and callable(get_service) and get_service("llm") is None
    except Exception:
        logger.debug("no_model probe failed; not refusing", exc_info=True)
        return False
