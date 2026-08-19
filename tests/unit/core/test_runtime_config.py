"""core.runtime_config.resolve_runtime_config — the one provider/model resolver
both the CLI and the server consume (Seam 2, Phase 2).

Ladder: explicit arg > pinned (CHAT_/DEFAULT_ env, caller-supplied) > cli_store_default
(intersected) > first provider with a key (canonical order) > last_resort.
Intersection (skip keyless) applies ONLY to cli_store_default + first-key — never to
explicit or pinned.
"""
from core.runtime_config import resolve_runtime_config


def test_explicit_provider_wins_even_keyless():
    assert resolve_runtime_config("anthropic", "claude-x", available_keys=set()) == (
        "anthropic", "claude-x",
    )


def test_pinned_provider_exempt_from_intersection():
    # A CHAT_/DEFAULT_ pin is operator intent — used even with no key present.
    assert resolve_runtime_config(
        None, None, pinned_provider="anthropic", pinned_model="claude-y",
        available_keys=set(),
    ) == ("anthropic", "claude-y")


def test_cli_store_default_skipped_when_keyless():
    # The stale ~/.rob/cli.json bug: a keyless stored provider falls through.
    assert resolve_runtime_config(
        None, None, cli_store_default=("nvidia", "kimi"),
        available_keys={"OPENROUTER_API_KEY"},
    ) == ("openrouter", None)


def test_cli_store_default_used_when_keyed():
    assert resolve_runtime_config(
        None, None, cli_store_default=("openai", "gpt-5"),
        available_keys={"OPENAI_API_KEY", "ANTHROPIC_API_KEY"},
    ) == ("openai", "gpt-5")


def test_first_key_canonical_order():
    assert resolve_runtime_config(
        None, None, available_keys={"OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"},
    ) == ("openrouter", None)


def test_last_resort_default_is_openai():
    assert resolve_runtime_config(None, None, available_keys=set()) == ("openai", None)


def test_deepseek_only_falls_to_last_resort():
    # A DEEPSEEK_API_KEY alone must NOT auto-resolve to provider=deepseek (the manager
    # can't bootstrap it → hard crash). It is not initializable, so auto-select skips
    # it and falls to the last resort.
    assert resolve_runtime_config(
        None, None, available_keys={"DEEPSEEK_API_KEY"},
    ) == ("openai", None)


def test_explicit_deepseek_still_returned():
    # An EXPLICIT --provider deepseek is exempt from the initializable filter — it
    # reaches the manager and errors honestly (documented clean-error contract).
    assert resolve_runtime_config(
        "deepseek", "deepseek-chat", available_keys=set(),
    ) == ("deepseek", "deepseek-chat")


def test_last_resort_overridable():
    assert resolve_runtime_config(
        None, None, available_keys=set(), last_resort=("gemini", None),
    ) == ("gemini", None)


def test_server_path_never_reads_cli_store(monkeypatch):
    # When cli_store_default is None (server), there is no ~/.rob/cli.json read at all.
    import core.runtime_config as rc
    # If the function tried to read a stored default itself, this guard would be moot;
    # the contract is that the caller injects it. With None + a key present -> first-key.
    assert resolve_runtime_config(
        None, None, cli_store_default=None, available_keys={"ANTHROPIC_API_KEY"},
    ) == ("anthropic", None)


# --- Provider-alias canonicalization -----------------------------------------
# A ProviderSpec alias (`glm` -> `zai`, `kimi` -> `moonshot`) is accepted by the
# CLI's known-provider validation but the LLM manager only registers clients
# under SPEC names — so an un-canonicalized alias dies downstream as
# "No client found for provider glm" even with a valid key present.
# The resolver is the one seam both surfaces share, so it canonicalizes.


def test_explicit_alias_canonicalized_to_spec_name():
    assert resolve_runtime_config("glm", None, available_keys=set()) == ("zai", None)


def test_pinned_alias_canonicalized_to_spec_name():
    # DEFAULT_PROVIDER=kimi must reach the manager as `moonshot`.
    assert resolve_runtime_config(
        None, None, pinned_provider="kimi", pinned_model=None, available_keys=set(),
    ) == ("moonshot", None)


def test_stored_alias_canonicalized_and_intersected():
    # A stored alias with its provider's key present is honored under the SPEC name.
    assert resolve_runtime_config(
        None, None, cli_store_default=("glm", "glm-5.2"),
        available_keys={"GLM_API_KEY"},
    ) == ("zai", "glm-5.2")


def test_unknown_provider_passes_through_unchanged():
    # A providers.yaml/custom name the registry doesn't know is NOT rewritten —
    # it reaches the manager and errors honestly there.
    assert resolve_runtime_config("myrouter", "m1", available_keys=set()) == (
        "myrouter", "m1",
    )
