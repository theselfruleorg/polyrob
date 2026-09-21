"""OpenAI-compatible request/response schemas for POLYROB's /v1 endpoint."""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class ChatMessage(BaseModel):
    role: str
    content: str = ""


#: OpenAI request fields POLYROB CANNOT honour, mapped to the reason (B18).
#:
#: The model used to be implicitly permissive — pydantic dropped every field it
#: did not declare — so a caller sending `tools=[...]` or
#: `response_format={"type":"json_object"}` got a plain prose answer and no
#: indication the instruction had been discarded. Silently ignoring a request
#: parameter is the worst failure mode for a compatibility surface: the client
#: cannot tell a refusal from a bad answer.
UNSUPPORTED_PARAMS = {
    "tools": "POLYROB runs its own toolset; caller-supplied tools are not executed",
    "tool_choice": "caller-supplied tools are not executed",
    "functions": "deprecated OpenAI function-calling is not supported",
    "function_call": "deprecated OpenAI function-calling is not supported",
    "response_format": "structured-output modes are not supported",
    "stop": "stop sequences are not applied by the agent loop",
    "logprobs": "log probabilities are not exposed",
    "top_logprobs": "log probabilities are not exposed",
    "seed": "deterministic sampling is not exposed",
    "logit_bias": "logit bias is not exposed",
}

#: Accepted, but they do NOT do what OpenAI does — see the per-field note.
ACCEPTED_NO_OP_PARAMS = {
    "max_tokens": ("accepted and ignored: the agent's output length is governed "
                   "by its own step budget, not by this value"),
    "max_completion_tokens": ("accepted and ignored: see max_tokens"),
    "presence_penalty": "accepted and ignored: not forwarded to the provider",
    "frequency_penalty": "accepted and ignored: not forwarded to the provider",
    "top_p": "accepted and ignored: not forwarded to the provider",
    "n": "accepted only when 1 — POLYROB returns a single choice",
}


class ChatCompletionRequest(BaseModel):
    """One turn against POLYROB's agent, in OpenAI's request shape.

    ⚠️ SINGLE-TURN: only the LAST user message is executed. POLYROB keeps its
    own per-session history, so replaying a client-side transcript would
    duplicate it; `system` messages are not applied (the agent's system prompt
    is built from its own identity + skills). See `docs/guide/api.md`.
    """
    model: str
    messages: List[ChatMessage]
    #: None = the caller did not ask for a temperature, so the agent's own
    #: configured sampling applies. An EXPLICIT value is threaded to the agent
    #: turn when the chat seam accepts one, and refused with the reason when it
    #: does not (B18) — never accepted and dropped.
    temperature: Optional[float] = None
    stream: bool = False
    user: Optional[str] = None  # OpenAI's optional end-user id; used as chat_id hint
    n: int = 1
    # Declared so an explicit value is VISIBLE to the validator below rather
    # than silently dropped. None = not sent.
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    top_p: Optional[float] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None

    model_config = ConfigDict(extra="allow")

    def unsupported_fields(self) -> List[str]:
        """Names of UNSUPPORTED fields this request actually carries (B18).

        A field present but null/empty is not a request for the behaviour, so
        it does not trip the refusal.
        """
        extra = self.model_extra or {}
        found = [name for name in UNSUPPORTED_PARAMS
                 if extra.get(name) not in (None, [], {}, "")]
        if self.n is not None and self.n != 1:
            found.append("n")
        return sorted(found)

    def unsupported_reason(self) -> str:
        """A caller-facing sentence naming every unsupported field and why."""
        parts = []
        for name in self.unsupported_fields():
            if name == "n":
                parts.append(f"'n': {ACCEPTED_NO_OP_PARAMS['n']}")
            else:
                parts.append(f"'{name}': {UNSUPPORTED_PARAMS[name]}")
        return "; ".join(parts)


class _Choice(BaseModel):
    index: int = 0
    message: dict
    finish_reason: str = "stop"


class _Usage(BaseModel):
    """Token usage.

    ⚠️ B18: these numbers are an ESTIMATE, computed by re-counting the request
    text and the reply — not the provider's billed counts. They exclude the
    system prompt, the skills block, memory recall, tool schemas and every
    intermediate agent step, so they are a FLOOR, often far below the real
    usage. `estimated: true` says so on every response; a client that budgets
    on this field must treat it as a lower bound. Authoritative per-call cost
    lives in `bot.db::usage_records` (see `core/status_economics.py`).
    """
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = True


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
    owned_by: str = "polyrob"


class ModelsListResponse(BaseModel):
    object: str = "list"
    data: List[_ModelCard]
