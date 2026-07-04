"""OpenAI-compatible request/response schemas for POLYROB's /v1 endpoint."""
from typing import List, Optional

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str
    content: str = ""


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    temperature: float = 0.7
    stream: bool = False
    user: Optional[str] = None  # OpenAI's optional end-user id; used as chat_id hint


class _Choice(BaseModel):
    index: int = 0
    message: dict
    finish_reason: str = "stop"


class _Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[_Choice]
    usage: _Usage

    @classmethod
    def build(cls, *, id: str, created: int, model: str, reply: str,
              prompt_tokens: int = 0, completion_tokens: int = 0) -> "ChatCompletionResponse":
        return cls(
            id=id, created=created, model=model,
            choices=[_Choice(message={"role": "assistant", "content": reply})],
            usage=_Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                         total_tokens=prompt_tokens + completion_tokens),
        )


class _ModelCard(BaseModel):
    id: str
    object: str = "model"
    owned_by: str = "rob"


class ModelsListResponse(BaseModel):
    object: str = "list"
    data: List[_ModelCard]
