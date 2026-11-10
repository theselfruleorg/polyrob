"""Agent › Settings: the model a new chat starts on (113 W6, G-4).

``GET  /api/webgate/model`` — the owner's choice for new chats
(``core.model_pins.chat_runtime_for``), the aliases, and names to pick from.
``POST /api/webgate/model`` — ``{"name": …}`` or ``{"reset": true}``: runs
``agents.task.model_switch_service.model_command`` with the words of
``/model <name> default`` / ``/model reset default`` — the one ``/model``
pipeline every seat calls (parse, resolve, validate, guard, pin). With no chat
named it pins no chat: it writes the owner's default for every NEW chat, and a
chat already open keeps its model (``/model`` in that chat changes it).

A write: ``webgate.MUTATION_DEPS`` (refused under read-only, CSRF), the owner
console only (``pages._owner_console_required``, the model choice spends the
instance's money), the tenant from ``pages._effective_user_id``, recorded as
``console_model_set``. An unreadable choice is ``readable: false``, never the
operator default dressed as yours. Contributed to the Agent destination
(``webview.contributions``).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.event_kinds import CONSOLE_MODEL_SET
from webview import webgate
from webview.audit import console_write

router = APIRouter()
logger = logging.getLogger(__name__)

#: One model word: a name, an alias or ``provider/model`` (no spaces, no verb word).
_WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,119}$")
_RESERVED = frozenset({"default", "reset", "aliases", "alias", "go", "confirm", "execute"})


def _choice(user_id: str) -> Dict[str, Any]:
    from core import model_pins
    provider, model, reasoning, source = model_pins.chat_runtime_for(user_id)
    _data, err = model_pins.load(user_id)
    out = {"readable": not err, "provider": provider, "model": model,
           "reasoning": reasoning, "source": source,
           "mine": model_pins.chat_pin_for(user_id) is not None}
    if err:
        out["error"] = str(err)
    return out


def _suggestions(user_id: str) -> List[str]:
    """Names to pick from: your aliases, then each keyed provider's default model."""
    names: List[str] = []
    try:
        from core import model_pins
        names.extend(sorted(model_pins.aliases_read(user_id)[0]))
    except Exception:
        logger.debug("model: aliases not read", exc_info=True)
    try:
        from modules.llm.llm_client_registry import get_default_model
        from modules.llm.profiles import providers_with_keys
        for provider in providers_with_keys():
            model = get_default_model(provider)
            if model and model not in names:
                names.append(model)
    except Exception:
        logger.debug("model: provider defaults not read", exc_info=True)
    return names


@router.get("/api/webgate/model")
async def api_model(request: Request):
    from webview.pages import _effective_user_id
    user_id = str(_effective_user_id(request))
    try:
        body = _choice(user_id)
    except Exception as e:  # noqa: BLE001 — named, never the operator default as yours
        logger.warning("model: the choice for new chats could not be read", exc_info=True)
        body = {"readable": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
    body["suggestions"] = _suggestions(user_id)
    body["can_set"] = webgate.is_owner_console() and not webgate.read_only()
    return JSONResponse(body)


@router.post("/api/webgate/model", dependencies=webgate.MUTATION_DEPS)
async def api_model_set(request: Request):
    from agents.task.model_switch_service import model_command
    from webview.pages import _effective_user_id, _owner_console_required
    _owner_console_required("the model choice")
    user_id = str(_effective_user_id(request))
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    if body.get("reset") is True:
        words = ["reset", "default"]
    else:
        words = str(body.get("name") or "").split()
        if not 1 <= len(words) <= 2 or any(
                not _WORD.match(w) or w.lower() in _RESERVED or "=" in w for w in words):
            return JSONResponse({"ok": False, "error": "bad_name"}, status_code=400)
        words = words + ["default"]
    from webview.server import _in_process_task_agent
    try:
        reply = await model_command(_in_process_task_agent(), user_id=user_id, words=words,
                                    chat_key=None, session_id=None, seat="console")
    except Exception as e:  # noqa: BLE001 — the answer is shown, never dropped
        logger.warning("model: the /model pipeline raised", exc_info=True)
        reply = f"❌ {type(e).__name__}: {str(e)[:160]}"
    ok = str(reply or "").lstrip().startswith("✅")
    console_write(CONSOLE_MODEL_SET, user_id=user_id,
                  attrs={"words": " ".join(words), "ok": ok})
    try:
        now = _choice(user_id)
    except Exception:
        now = {"readable": False}
    return JSONResponse({"ok": ok, "message": str(reply or ""), "now": now},
                        status_code=200 if ok else 409)


from webview.contributions import register_console_router  # noqa: E402

register_console_router(router, destination="agent", source="webview.model_routes")
