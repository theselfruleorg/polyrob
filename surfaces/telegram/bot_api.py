"""The ONE constructor of the live aiogram ``Bot``."""
from typing import Any

from core.surfaces.telegram_api import local_bot_api_url


def make_bot(token: str) -> Any:
    """A ``Bot`` on the public server, or on the local server in local mode."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    # No link preview card: status and receipt replies name explorer URLs, and a big
    # preview per reply buries the text (2026-11-20 UX review).
    default = DefaultBotProperties(link_preview_is_disabled=True)
    url = local_bot_api_url()
    if not url:
        return Bot(token, default=default)
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer
    return Bot(token, default=default, session=AiohttpSession(
        api=TelegramAPIServer.from_base(url, is_local=True)))
