from api.openai_compat.models import ChatCompletionRequest, ChatCompletionResponse


def test_request_parses_minimal_openai_body():
    req = ChatCompletionRequest(model="gpt-4", messages=[{"role": "user", "content": "hi"}])
    assert req.model == "gpt-4"
    assert req.messages[-1].content == "hi"
    assert req.stream is False
    # B18: an unsent temperature is None ("the caller did not ask"), not a
    # fabricated 0.7. The old default was never forwarded anywhere, so it
    # described nothing; an explicit value is now threaded to the agent turn.
    assert req.temperature is None


def test_request_accepts_stream_flag():
    req = ChatCompletionRequest(model="x", messages=[{"role": "user", "content": "y"}], stream=True)
    assert req.stream is True


def test_response_shape_matches_openai():
    resp = ChatCompletionResponse.build(id="chatcmpl-1", created=123, model="gpt-4",
                                        reply="hello", prompt_tokens=3, completion_tokens=2)
    d = resp.model_dump()
    assert d["object"] == "chat.completion"
    assert d["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
    assert d["choices"][0]["finish_reason"] == "stop"
    assert d["usage"]["total_tokens"] == 5
