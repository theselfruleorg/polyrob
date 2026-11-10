"""Which Bot API server this instance talks to.

⚠️ A bot that runs on a local Bot API server must be logged out of the public one,
and a call to the public server logs it back in there. So every direct Bot API
call reads its base URL here.
"""
import logging
import os
from typing import Mapping, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

PUBLIC_BOT_API = "https://api.telegram.org"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def local_bot_api_url(env: Optional[Mapping] = None) -> Optional[str]:
    """``TELEGRAM_BOT_API_URL`` when it names this machine, else None.

    The bot token travels in every request URL, and local mode returns paths on
    this machine's disk, so another host is refused."""
    raw = ((os.environ if env is None else env).get("TELEGRAM_BOT_API_URL") or "").strip()
    if not raw:
        return None
    parts = urlparse(raw)
    if parts.scheme not in ("http", "https") or parts.hostname not in _LOOPBACK:
        logger.warning("TELEGRAM_BOT_API_URL is ignored: it must be a loopback http(s) URL")
        return None
    return raw.rstrip("/")


def telegram_api_base(env: Optional[Mapping] = None) -> str:
    """The base URL for a direct Bot API call."""
    return local_bot_api_url(env) or PUBLIC_BOT_API
