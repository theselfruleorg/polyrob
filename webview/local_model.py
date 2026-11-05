"""A console on one person's own computer has a model, so its chat can answer.

Only at ``local`` posture. On a server the console and the agent are separate processes
under separate users, and the console holds no provider key: that shape stays refused
with ``no_model`` until the seat socket (proposal 070, W1.14 to W1.19).
"""
import logging

from webview import webgate

logger = logging.getLogger(__name__)


async def boot_local_model(container, config) -> bool:
    """Register the terminal's model service and tools here. True when chat can answer."""
    from tools.controller.approval import mark_console_process
    mark_console_process()  # every posture: a person here cannot answer a terminal prompt
    if not webgate.is_local():
        return False
    from polyrob.app.bootstrap import register_cli_tools, register_llm_service
    ready = await register_llm_service(container, config, require=False)
    if not ready:
        logger.warning("console chat: no usable provider key, so chat cannot answer")
        return False
    await register_cli_tools(container)  # never raises: a tool that fails is only absent
    return True
