"""Tests for cli.ui.secrets — display-only secret scrubbing for tool rendering.

This is a best-effort DISPLAY backstop (so a read_file of .env / an MCP result
doesn't paint a live token into scrollback that gets screen-shared or pasted into
a bug report), NOT a security boundary. Tests pin the high-value shapes + the
no-false-mangle of ordinary prose, and the scrub-then-truncate contract.
"""

from cli.ui.secrets import REDACTED, scrub_secrets, scrub_then_cap


# ---------------------------------------------------------------------------
# Positive: known secret shapes are redacted
# ---------------------------------------------------------------------------

def test_openai_style_key_redacted():
    out = scrub_secrets("key is sk-abcdEFGH1234567890abcdEFGH done")
    assert "sk-abcdEFGH1234567890abcdEFGH" not in out
    assert REDACTED in out


def test_anthropic_style_key_redacted():
    out = scrub_secrets("sk-ant-api03-abc123DEF456ghi789JKL012mno345PQR")
    assert "abc123DEF456" not in out
    assert REDACTED in out


def test_rob_api_key_redacted():
    out = scrub_secrets("X-API-KEY: rob_aBcd1234EFgh5678ijKL90mnOP")
    assert "rob_aBcd1234EFgh5678ijKL90mnOP" not in out
    assert REDACTED in out


def test_aws_access_key_redacted():
    out = scrub_secrets("AKIAIOSFODNN7EXAMPLE in config")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert REDACTED in out


def test_bearer_token_redacted():
    out = scrub_secrets("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6")
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6" not in out
    assert REDACTED in out


def test_key_value_pairs_redacted_but_key_kept():
    for pair in (
        'api_key="supersecretvalue123"',
        "password=hunter2hunter2",
        "token: abcdef123456ghijkl",
        "client_secret=zzz9999yyy8888",
    ):
        out = scrub_secrets(pair)
        assert REDACTED in out, pair
        # the key name stays so the line is still readable
        key = pair.split("=")[0].split(":")[0].strip()
        assert key.split()[-1].lower() in out.lower(), pair


def test_uppercase_env_api_key_redacted():
    # The module's own motivating case: a read_file of a .env line. The credential
    # word is preceded by an identifier prefix ("GEMINI_"), which the old \b broke.
    out = scrub_secrets("GEMINI_API_KEY=AIzaSyD3aBcDeFgHiJkLmNoPqRsTuVwXyZ012")
    assert "AIzaSyD3aBcDeFgHiJkLmNoPqRsTuVwXyZ012" not in out
    assert REDACTED in out
    assert "GEMINI_API_KEY" in out  # key name preserved


def test_underscore_prefixed_secret_redacted():
    out = scrub_secrets("MY_SECRET=hunter2000abcdef")
    assert "hunter2000abcdef" not in out
    assert REDACTED in out


def test_env_token_shapes_redacted():
    for pair in ("OPENAI_ACCESS_TOKEN=abcdef123456ghijkl",
                 "SLACK_AUTH_TOKEN=xyz-987654321-abc"):
        out = scrub_secrets(pair)
        assert REDACTED in out, pair


def test_google_api_key_bare_redacted():
    # A bare Gemini key (not in a key=value pair) — 39 chars, under the base64 rule.
    key = "AIzaSyD3aBcDeFgHiJkLmNoPqRsTuVwXyZ01234"  # AIza + 35 chars
    out = scrub_secrets(f"here is {key} ok")
    assert key not in out
    assert REDACTED in out


def test_slack_token_redacted():
    # Assembled from fragments so secret scanners (gitleaks / GitHub push protection)
    # don't flag this synthetic fixture as a real Slack token.
    tok = "xoxb-" + "1234567890-abcdefABCDEFghij"
    out = scrub_secrets(f"token {tok} done")
    assert tok not in out
    assert REDACTED in out


def test_github_tokens_redacted():
    for tok in ("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
                "github_pat_11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz"):
        out = scrub_secrets(f"key {tok} end")
        assert tok not in out, tok
        assert REDACTED in out, tok


def test_env_config_values_not_over_redacted():
    # Prefix-allowing must NOT catch non-credential keys that merely contain a
    # credential substring not at the end (regression guard for the wider regex).
    assert scrub_secrets("MAX_TOKENS=128000") == "MAX_TOKENS=128000"
    assert scrub_secrets("SESSION_TOKEN_LIMIT=50") == "SESSION_TOKEN_LIMIT=50"


def test_pem_private_key_block_redacted():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890abcdef\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = scrub_secrets(f"here: {pem}")
    assert "MIIEowIBAAKCAQEA" not in out
    assert REDACTED in out


def test_long_hex_blob_redacted():
    out = scrub_secrets("digest " + "a1" * 24)
    assert "a1a1a1a1" not in out
    assert REDACTED in out


# ---------------------------------------------------------------------------
# Negative: ordinary content is not mangled
# ---------------------------------------------------------------------------

def test_ordinary_prose_unchanged():
    text = "Read the file config.py and summarise the top 3 functions."
    assert scrub_secrets(text) == text


def test_short_values_not_redacted():
    # An obviously-non-secret short value isn't touched.
    assert scrub_secrets("limit=20") == "limit=20"
    assert scrub_secrets("path=config.py") == "path=config.py"


def test_empty_and_none_safe():
    assert scrub_secrets("") == ""
    assert scrub_secrets(None) == ""


def test_filepath_not_redacted():
    text = "/Users/me/project/src/main.py"
    assert scrub_secrets(text) == text


# ---------------------------------------------------------------------------
# scrub_then_cap: scrub BEFORE truncation (so a secret can't survive half-cut)
# ---------------------------------------------------------------------------

def test_scrub_then_cap_order():
    # A secret near the cap boundary must be redacted, not half-shown.
    raw = "prefix " + "sk-" + "Z" * 40 + " suffixsuffixsuffix"
    out = scrub_then_cap(raw, limit=20)
    assert "ZZZZ" not in out
    assert len(out) <= 21  # limit + ellipsis


def test_scrub_then_cap_caps_long_clean_text():
    # Long, clearly-non-secret prose (spaced words — not a base64-looking blob).
    out = scrub_then_cap("the quick brown fox " * 30, limit=50)
    assert len(out) <= 51
    assert out.endswith("…")
    assert REDACTED not in out


def test_scrub_then_cap_none():
    assert scrub_then_cap(None, limit=10) == ""
