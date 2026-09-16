"""A managed-RPC endpoint is a credential (S10, 2026-09-14).

``https://base-mainnet.g.alchemy.com/v2/<key>`` carries its API key in a PATH
SEGMENT. ``core/flags.py`` classified secrets by NAME SUFFIX (``_KEY``,
``_TOKEN``, …) and ``core/secret_patterns.py`` by value SHAPE (``key=value``,
``Bearer …``, ``sk-…``) — an RPC URL matched neither, so prod's live Alchemy key
was returned intact by ``GET /api/webgate/config`` and by the agent's own
``preferences explain`` action.

Two independent nets, both pinned here:

1. the flag is classified secret, so every report renders ``(set, masked)``;
2. the VALUE shape is redacted wherever it appears — a log line, a persisted
   tool result, the CLI display — even under a flag name nobody classified.
"""
import pytest

from core.flags import REGISTRY, is_secret_flag, resolve_flag
from core.secret_patterns import REDACTED as SHARED_REDACTED
from core.secret_scrub import scrub_secret_shapes

ALCHEMY_URL = "https://base-mainnet.g.alchemy.com/v2/Kx8Jd92abcdefghijklmnopqrstuv"
ALCHEMY_KEY = "Kx8Jd92abcdefghijklmnopqrstuv"


# ---------------------------------------------------------------------------
# Net 1 — flag classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "DEFI_EVM_RPC_BASE",
    "DEFI_SOLANA_RPC",
    "X402_SETTLEMENT_RPC",
    "ETHEREUM_RPC_URL",
    "BASE_RPC_URL",
    "POLYGON_RPC_URL",
    "ARBITRUM_RPC_URL",
])
def test_rpc_flag_is_classified_secret(name):
    assert is_secret_flag(name), f"{name} would print its API key UNMASKED"


def test_rpc_flag_value_is_masked_by_resolution():
    r = resolve_flag("DEFI_EVM_RPC_BASE", {"DEFI_EVM_RPC_BASE": ALCHEMY_URL})
    assert ALCHEMY_KEY not in str(r.value)
    assert r.value == "(set, masked)"


def test_set_vs_unset_stays_answerable():
    """Masking hides the VALUE, not the fact that a chain is pinned — 'is my
    money chain pinned?' must stay answerable from `doctor --flags`."""
    pinned = resolve_flag("DEFI_EVM_RPC_BASE", {"DEFI_EVM_RPC_BASE": ALCHEMY_URL})
    unset = resolve_flag("DEFI_EVM_RPC_BASE", {})
    assert pinned.value == "(set, masked)" and pinned.source == "env"
    assert unset.value == "(unset)" and unset.source == "default"


def test_config_service_explain_masks_the_rpc_key(monkeypatch):
    import core.config_service as cs
    monkeypatch.setenv("DEFI_EVM_RPC_BASE", ALCHEMY_URL)
    info = cs.explain("DEFI_EVM_RPC_BASE")
    assert info.secret is True
    assert ALCHEMY_KEY not in str(info.effective)
    # The provenance chain shows WHERE it is set without showing WHAT it is.
    for rung in info.chain:
        assert ALCHEMY_KEY not in str(rung.value), rung


def test_config_service_describe_list_masks_every_rpc_flag(monkeypatch):
    import core.config_service as cs
    monkeypatch.setenv("DEFI_EVM_RPC_BASE", ALCHEMY_URL)
    for key in REGISTRY:
        if "_RPC" not in key.upper():
            continue
        info = cs.describe(key)
        assert info.secret is True, key
        assert ALCHEMY_KEY not in str(info.effective), key


def test_an_rpc_flag_is_console_unwritable(monkeypatch):
    """Secret flags are already refused by the console config write; the new
    classification pulls the RPC pins into that refusal too."""
    import core.config_service as cs
    assert cs.is_console_unwritable("DEFI_EVM_RPC_BASE")


# ---------------------------------------------------------------------------
# Net 2 — value shape
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    ALCHEMY_URL,
    f"DEFI_EVM_RPC_BASE={ALCHEMY_URL}",
    "https://mainnet.infura.io/v3/0123456789abcdef0123456789abcdef",
    "https://my-node.quiknode.pro/0123456789abcdef0123456789abcdef01234567/",
    "https://mainnet.helius-rpc.com/?api-key=abc123def456ghi789",
])
def test_scrub_redacts_a_key_bearing_rpc_url(text):
    out = scrub_secret_shapes(text)
    assert SHARED_REDACTED in out, out
    assert ALCHEMY_KEY not in out
    assert "0123456789abcdef0123456789abcdef" not in out
    assert "abc123def456ghi789" not in out


def test_scrub_keeps_the_host_so_the_operator_can_still_read_it():
    out = scrub_secret_shapes(ALCHEMY_URL)
    assert out.startswith("https://base-mainnet.g.alchemy.com/v2/")
    assert ALCHEMY_KEY not in out


@pytest.mark.parametrize("text", [
    # Unauthenticated public endpoints — nothing to hide, and hiding them would
    # destroy the one signal that says "you are on the shared public RPC".
    "https://mainnet.base.org",
    "https://api.mainnet-beta.solana.com",
    # Ordinary long URLs the agent legitimately holds in persisted history.
    "https://docs.example.com/v2/getting-started-with-the-whole-api",
    "https://github.com/theselfruleorg/polyrob/commit/3a3c341500000000000000000000000000000000",
    "https://openrouter.ai/api/v1/chat/completions",
])
def test_scrub_leaves_a_non_credential_url_intact(text):
    assert scrub_secret_shapes(text) == text


def test_rule_runs_in_the_shared_battery_not_only_the_persisted_scrubber():
    """The battery is the ONE home — the CLI display scrubber and the logging
    filter must gain the rule for free (the JWT rung was once dropped by one
    consumer, which is why this is pinned)."""
    import cli.ui.secrets as display
    import core.security_logging_filter as filt
    import core.secret_patterns as shared
    assert display.apply_ssot_shapes is shared.apply_ssot_shapes
    assert filt.apply_ssot_shapes is shared.apply_ssot_shapes
    assert ALCHEMY_KEY not in shared.apply_ssot_shapes(ALCHEMY_URL)
