"""A cron job whose pinned provider is credit-dead re-routes to a live one.

The 3-hourly digest job carried `"provider":"zai-coding"` in its stored payload.
From 2026-08-17 19:35Z every tick logged
`cron job 1555aa03a738: provider-credit sentinel active for zai-coding — $0 skip`
and nothing ran — for 37 hours, with a second credentialed provider configured.
A $0 skip is correct only when NOTHING can serve.
"""
import pytest

pytest.importorskip("cron.runner")


def _payload(provider="zai-coding"):
    return {"provider": provider, "model": "glm-4.6", "tools": ["filesystem"]}


def test_dead_pin_is_replaced_by_a_live_provider(monkeypatch):
    import core.runtime_config as rc
    monkeypatch.setattr(rc, "usable_providers_with_credentials",
                        lambda env=None: ["zai-coding", "openrouter"], raising=False)
    monkeypatch.setattr(rc, "_sentinel_active",
                        lambda provider=None: provider == "zai-coding", raising=False)

    from cron.runner import resolve_job_provider
    provider, skipped = resolve_job_provider(_payload())

    assert provider == "openrouter"
    assert skipped is False


def test_a_live_pin_is_left_alone(monkeypatch):
    import core.runtime_config as rc
    monkeypatch.setattr(rc, "usable_providers_with_credentials",
                        lambda env=None: ["zai-coding", "openrouter"], raising=False)
    monkeypatch.setattr(rc, "_sentinel_active", lambda provider=None: False, raising=False)

    from cron.runner import resolve_job_provider
    provider, skipped = resolve_job_provider(_payload())

    assert provider == "zai-coding"
    assert skipped is False


def test_everything_dead_still_skips_the_paid_tick(monkeypatch):
    import core.credit_sentinel as cs
    import core.runtime_config as rc
    monkeypatch.setattr(rc, "usable_providers_with_credentials",
                        lambda env=None: ["zai-coding", "openrouter"], raising=False)
    monkeypatch.setattr(rc, "_sentinel_active", lambda provider=None: True, raising=False)
    # The skip decision consults the real sentinel: "nothing resolved live" alone
    # is not evidence of credit death, so the latch must actually be tripped.
    monkeypatch.setattr(cs, "credit_sentinel_active", lambda provider=None: True,
                        raising=False)

    from cron.runner import resolve_job_provider
    provider, skipped = resolve_job_provider(_payload())

    assert skipped is True
    assert provider is None


# --- 2026-08-28 log forensics: an UNPINNED job (payload={}) went to the
# canonical-first provider, not the operator's DEFAULT_PROVIDER. On prod that
# was credit-dead OpenRouter: every status/exit-monitor tick 402'd first, fell
# back in-session, and (after 08-27) re-tripped the credit sentinel every 6h —
# 1,323 402 lines and five false credit alerts to the owner in four days. ----

def test_unpinned_job_prefers_the_operator_pin_over_canonical_order(monkeypatch):
    import core.runtime_config as rc
    monkeypatch.setattr(rc, "usable_providers_with_credentials",
                        lambda env=None: ["openrouter", "zai-coding"], raising=False)
    monkeypatch.setattr(rc, "_sentinel_active", lambda provider=None: False, raising=False)
    monkeypatch.delenv("CHAT_PROVIDER", raising=False)
    monkeypatch.setenv("DEFAULT_PROVIDER", "zai-coding")

    from cron.runner import resolve_job_provider
    assert resolve_job_provider({}) == ("zai-coding", False)
    assert resolve_job_provider(None) == ("zai-coding", False)


def test_a_stored_pin_still_beats_the_operator_pin(monkeypatch):
    import core.runtime_config as rc
    monkeypatch.setattr(rc, "usable_providers_with_credentials",
                        lambda env=None: ["openrouter", "zai-coding"], raising=False)
    monkeypatch.setattr(rc, "_sentinel_active", lambda provider=None: False, raising=False)
    monkeypatch.setenv("DEFAULT_PROVIDER", "zai-coding")

    from cron.runner import resolve_job_provider
    assert resolve_job_provider({"provider": "openrouter"}) == ("openrouter", False)


def test_unpinned_job_with_a_dead_operator_pin_reroutes(monkeypatch):
    import core.runtime_config as rc
    monkeypatch.setattr(rc, "usable_providers_with_credentials",
                        lambda env=None: ["openrouter", "zai-coding"], raising=False)
    monkeypatch.setattr(rc, "_sentinel_active",
                        lambda provider=None: provider == "zai-coding", raising=False)
    monkeypatch.setenv("DEFAULT_PROVIDER", "zai-coding")

    from cron.runner import resolve_job_provider
    assert resolve_job_provider({}) == ("openrouter", False)
