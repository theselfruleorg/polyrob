"""OpenAI-compatible /v1 router. Gated; mounted by app.py only
when OPENAI_COMPAT_API_ENABLED is on. Reuses POLYROB auth (request.state.user_id),
per-request billing, and the tool-light chat agent (chat_once)."""
import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from api.openai_compat.model_map import map_model
from api.openai_compat.models import (ChatCompletionRequest, ChatCompletionResponse,
                                       ModelsListResponse, _ModelCard)
from api.payment_verification import verify_payment_for_request

logger = logging.getLogger(__name__)
router = APIRouter(tags=["openai-compatible"])


def _estimate_tokens(text: str, model: str = "gpt-4o") -> int:
    """Estimate token count using model registry when available, with fallback."""
    if not text:
        return 0
    try:
        from modules.llm.token_counter import count_tokens
        return count_tokens(text, model)
    except Exception:
        # Fallback to character-based estimation
        return max(1, len(text) // 4)


def openai_compat_enabled() -> bool:
    """Whether the OpenAI-compatible /v1 surface is mounted (default OFF)."""
    import os
    return (os.getenv("OPENAI_COMPAT_API_ENABLED", "") or "").strip().lower() \
        in ("1", "true", "yes", "on")


def _get_container():
    from core.container import DependencyContainer
    return DependencyContainer.get_instance()


def _last_user_text(req: ChatCompletionRequest) -> str:
    for m in reversed(req.messages):
        if m.role == "user" and (m.content or "").strip():
            return m.content
    return ""


def _sse_data(payload: dict) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


#: 019 P4: seconds between SSE keep-alive comments while chat_once runs.
#: Comment frames (": ...") are SSE-spec-legal and ignored by OpenAI SDK
#: parsers; they stop client/proxy idle timeouts on long agent turns.
STREAM_KEEPALIVE_SEC = 15.0


async def _stream_chat_completion(*, chat_id: str, created: int, model: str, reply: str):
    """Emit OpenAI-compatible SSE chunks.

    The underlying chat_once API returns one complete reply today, so this is basic
    streaming compatibility rather than provider token streaming.
    """
    yield _sse_data({
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    })
    if reply:
        yield _sse_data({
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": reply}, "finish_reason": None}],
        })
    yield _sse_data({
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    yield "data: [DONE]\n\n"


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request):
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing authentication")
    await verify_payment_for_request(request, cost_credits=1)  # raises 402 if unpaid

    text = _last_user_text(body)
    if not text:
        raise HTTPException(status_code=400, detail="no user message provided")

    container = _get_container()
    agent = container.get_agent("task_agent") if container else None
    if not agent or not hasattr(agent, "chat_once"):
        raise HTTPException(status_code=503, detail="agent unavailable")

    chat_id = body.user or user_id
    # B3: route body.model per request. map_model() never raises — an unknown
    # model with no provider slug/prefix falls back to the env-resolved
    # default provider (model string kept verbatim) — so this never 400s.
    provider, model = map_model(body.model) if body.model else (None, None)
    response_id = f"chatcmpl-{int(time.time()*1000)}"
    created = int(time.time())

    async def _run_chat() -> str:
        reply = await agent.chat_once(
            user_id=user_id, text=text, chat_id=chat_id, provider=provider, model=model,
        )
        return str(reply or "")

    if body.stream:
        # 019 P4: chat_once buffers the WHOLE reply (true token streaming is
        # deferred to 019 P5), so the SSE body used to start only after the
        # turn finished — a multi-minute agent turn hit client/proxy idle
        # timeouts. Run chat_once concurrently and emit keep-alive comments
        # while it works; an error after headers surfaces as a final content
        # chunk (the stream already committed a 200).
        async def _stream_with_keepalive():
            chat_task = asyncio.create_task(_run_chat())
            try:
                while True:
                    try:
                        reply = await asyncio.wait_for(
                            asyncio.shield(chat_task), timeout=STREAM_KEEPALIVE_SEC
                        )
                        break
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            except asyncio.CancelledError:
                chat_task.cancel()
                raise
            except Exception as e:
                logger.error(f"openai-compat streamed chat failed: {e}", exc_info=True)
                reply = f"[error] agent turn failed: {type(e).__name__}"
            async for chunk in _stream_chat_completion(
                chat_id=response_id, created=created, model=body.model, reply=reply,
            ):
                yield chunk

        return StreamingResponse(
            _stream_with_keepalive(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    reply = await _run_chat()
    # Use actual token counts when available, with fallback to estimation
    prompt_tokens = _estimate_tokens(text, model=body.model)
    completion_tokens = _estimate_tokens(reply, model=body.model)
    # body.model is echoed back verbatim (OpenAI-compat contract) even though
    # routing above used the mapped (provider, model) pair — see map_model().
    return ChatCompletionResponse.build(
        id=response_id, created=created,
        model=body.model, reply=reply,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
    ).model_dump()


@router.get("/v1/models")
async def list_models():
    from modules.llm.llm_client_registry import DEFAULT_MODELS
    cards = [_ModelCard(id=f"{prov}/{model}") for prov, model in DEFAULT_MODELS.items()]
    return ModelsListResponse(data=cards).model_dump()
