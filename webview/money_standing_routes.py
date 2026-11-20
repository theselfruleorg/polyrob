"""Money › Limits: the owner's standing money rules.

``GET /api/webgate/standing`` — the status snapshot's ``income_rule`` and
``mandates`` sections (``core.status_standing``), built through the ONE section
registration (``core.status_sections.build_slot``), so the console shows the lines
Telegram ``/status all`` and the terminal show. A read: owner-scoped through
``pages._wallet_owner_id``. A store that cannot be read is the section's own
``unavailable`` state with its reason, never "none". Contributed to the Money
destination (``webview.contributions``).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

#: The sections drawn, in this order (each is a ``SECTION_ORDER`` slot).
STANDING_SECTIONS = ("income_rule", "mandates")


def standing_body(data_dir: str) -> dict:
    from core.status_sections import SectionContext, build_slot
    ctx = SectionContext(uid="", data_dir=data_dir, now=time.time(), window_sec=86400,
                         include_money=False)
    return {"sections": [asdict(build_slot(name, ctx)) for name in STANDING_SECTIONS]}


@router.get("/api/webgate/standing")
async def api_standing(request: Request):
    from webview.pages import _data_dir, _wallet_owner_id
    _wallet_owner_id(request)  # 403 outside the owner, never an empty list
    return JSONResponse(await asyncio.to_thread(standing_body, _data_dir()))


from webview.contributions import register_console_router  # noqa: E402

register_console_router(router, destination="money", source="webview.money_standing_routes")
