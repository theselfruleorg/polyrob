"""The ONE constructor of the live aiogram ``Bot``."""
from typing import Any

from core.surfaces.telegram_api import local_bot_api_url


def make_bot(token: str) -> Any:
    """A ``Bot`` on the public server, or on the local server in local mode."""
    from aiogram import Bot
    url = local_bot_api_url()
    if not url:
        return Bot(token)
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer
    return Bot(token, session=AiohttpSession(
        api=TelegramAPIServer.from_base(url, is_local=True)))
