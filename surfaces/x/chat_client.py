"""Back-compat re-export: the X Chat client moved to ``tools/x_chat_client.py``.

Its only consumer is the ``twitter`` TOOL (tier 3); a tool importing
``surfaces.*`` (tier 4) is the upward edge ``tests/test_layering_ratchet.py``
forbids, and the client itself depends on nothing in this package.
"""
from tools.x_chat_client import XChatAPIError, XChatClient  # noqa: F401

__all__ = ["XChatAPIError", "XChatClient"]
