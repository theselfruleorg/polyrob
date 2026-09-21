"""Regression (P4 finalization → 2026-08-09 battery consolidation): the
high-confidence credential shapes are applied by ONE ordered battery,
``core.secret_patterns.apply_ssot_shapes``, and BOTH scrubbers delegate to it —
a stronger guarantee than the old shared-pattern-object identity check (which
still allowed a consumer to drop a rung, exactly how the logging filter lost
its JWT redaction).
"""
import core.secret_patterns as shared
import core.secret_scrub as persisted
import cli.ui.secrets as display


def test_both_scrubbers_delegate_to_the_shared_battery():
    assert persisted.apply_ssot_shapes is shared.apply_ssot_shapes
    assert display.apply_ssot_shapes is shared.apply_ssot_shapes


def test_logging_filter_delegates_to_the_shared_battery():
    import core.security_logging_filter as filt
    assert filt.apply_ssot_shapes is shared.apply_ssot_shapes


def test_battery_covers_every_documented_shape():
    samples = {
        "pem": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        "bearer": "Authorization: Bearer abcdef123456789",
        "kv": "OPENAI_API_KEY=supersecretvalue123",
        "provider": "sk-abcdefghijklmnop123456",
        "rob": "rob_abcdefghijklmnop1234",
        "aws": "AKIAABCDEFGHIJKLMNOP",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJyb2IifQ.c2ln",
        # S10: the API key is the URL PATH — no `=`, no prefix, no Bearer.
        "rpc_path_key": "https://base-mainnet.g.alchemy.com/v2/Kx8Jd92abcdefghijklmnopqrstuv",
    }
    for name, sample in samples.items():
        out = shared.apply_ssot_shapes(f"x {sample} y")
        assert shared.REDACTED in out, f"battery missed the {name} shape: {out}"


def test_scrubbers_keep_distinct_redaction_markers():
    # They share the battery but not the marker (persisted uses a tagged shape).
    assert persisted.REDACTED != display.REDACTED


def test_wallet_shaped_kv_keys_are_claimed():
    """2026-09-21 revalidation: private_key / mnemonic / seed_phrase /
    passphrase / keystore under a KV shape are credentials, prefixed or not."""
    from core.secret_scrub import scrub_secret_shapes
    key = "0x" + "ab" * 32
    words = "abandon ability able about above absent absorb abstract absurd abuse access accident"
    for line in (f"private_key={key}", f"PRIVATE_KEY: {key}", f"WALLET_PRIVATE_KEY={key}",
                 f"mnemonic={words.replace(' ', '-')}", f"seed_phrase='{words.replace(' ', '_')}'",
                 "passphrase=correct-horse-battery-staple", "keystore=eyJ2ZXJzaW9uIjozfQ"):
        out = scrub_secret_shapes(line)
        assert key not in out and "abandon-ability" not in out and "correct-horse" not in out \
            and "eyJ2ZXJz" not in out, (line, out)
    # a public fact under the bare key `token` stays a public fact
    addr = "0x" + "ab" * 20
    assert addr in scrub_secret_shapes(f"token: {addr}")
