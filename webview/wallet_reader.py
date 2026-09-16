"""Owner-scoped wallet view; no signing endpoint in the console process."""
import asyncio
from dataclasses import asdict
from fastapi import Request
from fastapi.responses import JSONResponse


async def api_wallet(request: Request):
    from webview.pages import _wallet_owner_id, _data_dir
    from core.wallet.view import wallet_view
    uid = _wallet_owner_id(request)
    view = await asyncio.to_thread(wallet_view, uid, data_dir=_data_dir())
    return JSONResponse(asdict(view))
