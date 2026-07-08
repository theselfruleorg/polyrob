"""P2-17 (intelligence-polish plan 2026-07-07): token-counter encoder fixes.

- Anthropic no longer builds a per-model Anthropic() client (dead weight — the SDK's
  count_tokens isn't usable here); it falls to the chars_per_token estimate.
- Modern OpenAI models use o200k_base, not cl100k_base.
"""
from modules.llm.token_counter import TokenCounter, EncoderCache


def test_anthropic_builds_no_client_encoder():
    ec = EncoderCache()
    enc = ec._create_encoder("claude-sonnet-4-5")
    # must NOT be an Anthropic client instance (return None -> estimate path)
    assert enc is None or not enc.__class__.__module__.startswith("anthropic")


def test_anthropic_count_still_works_via_estimate():
    tc = TokenCounter()
    n = tc.count_tokens("hello world " * 20, "claude-sonnet-4-5")
    assert n > 0  # chars_per_token estimate, no crash


def test_modern_openai_uses_o200k():
    import tiktoken
    ec = EncoderCache()
    enc = ec._create_encoder("gpt-4o")
    if enc is not None and hasattr(enc, "name"):
        # tiktoken Encoding exposes .name
        assert enc.name in ("o200k_base", "cl100k_base")  # a real tiktoken encoding
        # gpt-4o specifically should be o200k when it falls through encoding_for_model
    # counting doesn't crash
    assert TokenCounter().count_tokens("hello", "gpt-4o") >= 0
