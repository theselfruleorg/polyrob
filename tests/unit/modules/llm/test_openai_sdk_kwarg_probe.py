"""An optional SDK parameter must never be able to kill the call.

`requirements.txt` pins `openai>=1.26.0`, but `prompt_cache_key` (a pure
prefix-cache OPTIMISATION) was added to the SDK much later. On an install that
resolves to an older-but-permitted version, every OpenAI request raised
`TypeError: AsyncCompletions.create() got an unexpected keyword argument
'prompt_cache_key'` and the agent reported "All LLM providers failed" — a total
outage caused by a cost optimisation, on a version range we ourselves allow.

Reproduced locally on openai 1.68.2 (`tests/unit/cli/test_repl_pipe_rig.py`).

The rule: a parameter we send to SAVE money is dropped when the installed SDK
does not know it. A parameter that changes the ANSWER is not in scope here.
"""
import pytest


def test_probe_reports_a_known_parameter():
    from modules.llm.openai_client import sdk_supports
    assert sdk_supports("messages") is True
    assert sdk_supports("model") is True


def test_probe_reports_an_unknown_parameter():
    from modules.llm.openai_client import sdk_supports
    assert sdk_supports("definitely_not_an_openai_parameter") is False


def test_probe_is_fail_open_when_the_signature_is_unreadable(monkeypatch):
    """An SDK we cannot introspect must not lose the optimisation for everyone —
    the call itself is still protected by the same TypeError path it always was."""
    import inspect as _inspect
    import modules.llm.openai_client as oc
    oc.sdk_supports.cache_clear()
    monkeypatch.setattr(_inspect, "signature",
                        lambda *a, **kw: (_ for _ in ()).throw(ValueError("no sig")))
    assert oc.sdk_supports("prompt_cache_key") is True
    oc.sdk_supports.cache_clear()


def test_prompt_cache_key_is_only_sent_when_supported(monkeypatch):
    """The actual guard: the client asks the probe before setting the kwarg."""
    import inspect
    import modules.llm.openai_client as oc
    src = inspect.getsource(oc)
    for line in src.splitlines():
        if "request_params['prompt_cache_key']" in line:
            break
    else:
        pytest.fail("prompt_cache_key is no longer set — update this test")
    assert src.count("sdk_supports('prompt_cache_key')") + \
        src.count('sdk_supports("prompt_cache_key")') >= 2, \
        "both the sync and the streaming path must ask before sending"
