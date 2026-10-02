"""CR-M08: a mistyped BIP-39 seed must not leak through a chained exception,
the logging filter, or the persisted-content scrubber."""
import logging
import traceback

import pytest

from core.wallet import derivation

# Valid words, broken checksum ("abandon" x12 fails the checksum).
TYPO_SEED = ("abandon ability able about above absent absorb abstract "
             "absurd abuse access accident")


def test_bip44_error_chain_does_not_carry_the_seed():
    pytest.importorskip("eth_account")
    with pytest.raises(ValueError) as ei:
        derivation.derive_key(TYPO_SEED, "treasury", "bip44")
    assert ei.value.__cause__ is None
    assert ei.value.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(ei.type, ei.value, ei.tb))
    for word in ("ability", "absorb", "accident"):
        assert word not in rendered


def test_scrub_secret_shapes_redacts_the_loaded_seed(monkeypatch):
    from core.secret_scrub import scrub_secret_shapes
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", TYPO_SEED)
    out = scrub_secret_shapes(f"error: Invalid mnemonic '{TYPO_SEED}' (checksum)")
    assert "absorb" not in out and "accident" not in out
    # A run of 4+ seed words is redacted even out of the full phrase.
    out = scrub_secret_shapes("words: absent absorb abstract absurd were given")
    assert "absorb" not in out
    # Ordinary prose that shares one or two words is left alone.
    assert scrub_secret_shapes("be able to access it") == "be able to access it"


def test_scrub_ignores_a_short_or_absent_seed(monkeypatch):
    from core.secret_scrub import scrub_loaded_seed
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED", raising=False)
    assert scrub_loaded_seed("abandon ability able about above") == \
        "abandon ability able about above"


def test_logging_filter_redacts_a_legacy_seed_without_markers(monkeypatch):
    from core.security_logging_filter import SecretScrubbingFilter
    legacy = "q7Zk2pLm9Xv4Rt8Wn3Yb6Hc1Jd5Fg0Se"  # 32 chars, no marker word
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", legacy)
    rec = logging.LogRecord("t", logging.ERROR, __file__, 1,
                            "derive failed for %s", (legacy,), None)
    SecretScrubbingFilter().filter(rec)
    assert legacy not in rec.getMessage()


def test_logging_filter_redacts_seed_in_a_chained_cause(monkeypatch):
    from core.security_logging_filter import SecretScrubbingFilter
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", TYPO_SEED)
    try:
        try:
            raise ValueError(f"bad words: {TYPO_SEED}")
        except ValueError as inner:
            raise RuntimeError("outer is clean") from inner
    except RuntimeError:
        import sys
        exc_info = sys.exc_info()
    rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, exc_info)
    SecretScrubbingFilter().filter(rec)
    assert "absorb" not in (rec.exc_text or "")
    assert "outer is clean" in rec.exc_text
