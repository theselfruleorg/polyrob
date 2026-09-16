"""046 step 4: the agent registers ITSELF on ERC-8004.

`defi_trade.register_agent` — not a new tool. `defi_trade` is already
`{money, high_impact, delegate_blocked}` and already the EVM write surface, so
every gate is inherited by construction rather than re-earned across five lists.

⚠️ THE AGENT URI NEEDS NO HOSTING. The spec says agents SHOULD use a
base64-encoded `data:` URI, so a self-hoster with no domain, no IPFS account and
no public endpoint can still register. That is the whole reason this is
reachable for anyone other than us.

⚠️ REGISTERING TWICE SPLITS THE IDENTITY. `register()` mints a SECOND token; it
is not idempotent. The verb reads the chain for an existing agentId owned by
this address and refuses with it named — a local flag would be lost by a fresh
data dir, and the chain is the only thing that actually knows.
"""
import base64
import json

import pytest


# --- the five lists -------------------------------------------------------

def test_the_verb_is_on_the_owner_approval_lane():
    from core.config_policy.payment_tools import PAYMENT_APPROVAL_TOOLS
    assert "defi_trade_register_agent" in PAYMENT_APPROVAL_TOOLS


def test_the_verb_is_classified_for_a_spend_lane():
    """Its cost is a bounded FEE, so the ceiling is meaningful — unlike an NFT
    transfer, whose value nothing can bound."""
    from core.config_policy.spend_lane import (ALWAYS_OWNER_APPROVED_VERBS,
                                               DEFI_SPEND_VERBS)
    assert "defi_trade_register_agent" in DEFI_SPEND_VERBS
    assert "defi_trade_register_agent" not in ALWAYS_OWNER_APPROVED_VERBS


def test_the_verb_is_blocked_while_correspondent_tainted():
    from agents.task.agent.core.correspondent_gate import is_high_impact
    assert is_high_impact("defi_trade_register_agent")


# --- the agentURI ---------------------------------------------------------

def test_a_data_uri_round_trips_to_the_exact_same_json():
    """⚠️ Byte-exactness matters: the file IS the identity once it is on-chain."""
    from tools.defi.agent_registration import build_agent_uri
    doc = {"name": "acme", "active": True, "services": []}
    uri = build_agent_uri(doc, base_url=None)
    assert uri.startswith("data:application/json;base64,")
    decoded = json.loads(base64.b64decode(uri.split(",", 1)[1]))
    assert decoded == doc


def test_a_public_base_url_produces_a_hosted_uri():
    from tools.defi.agent_registration import build_agent_uri
    uri = build_agent_uri({"name": "acme"}, base_url="https://agent.example.com")
    assert uri == "https://agent.example.com/eip8004/registration.json"


def test_a_localhost_base_url_falls_back_to_the_data_uri():
    """Nobody reading the token can resolve localhost."""
    from tools.defi.agent_registration import build_agent_uri
    uri = build_agent_uri({"name": "acme"}, base_url="http://localhost:9000")
    assert uri.startswith("data:")


def test_the_mode_can_be_forced_to_data_even_with_a_domain(monkeypatch):
    from tools.defi.agent_registration import build_agent_uri
    monkeypatch.setenv("EIP8004_AGENT_URI_MODE", "data")
    uri = build_agent_uri({"name": "acme"}, base_url="https://agent.example.com")
    assert uri.startswith("data:")


def test_the_mode_can_be_forced_to_hosted(monkeypatch):
    from tools.defi.agent_registration import build_agent_uri
    monkeypatch.setenv("EIP8004_AGENT_URI_MODE", "hosted")
    with pytest.raises(ValueError) as exc:
        build_agent_uri({"name": "acme"}, base_url=None)
    assert "hosted" in str(exc.value).lower()


# --- the size guard -------------------------------------------------------

def test_an_oversized_document_REFUSES_rather_than_truncating():
    """⚠️ Truncating would put a corrupt identity on-chain permanently. The
    remedy is named instead."""
    from tools.defi.agent_registration import AgentUriTooLarge, build_agent_uri
    doc = {"name": "acme", "description": "x" * 40_000}
    with pytest.raises(AgentUriTooLarge) as exc:
        build_agent_uri(doc, base_url=None)
    msg = str(exc.value).lower()
    assert "shorten" in msg or "host" in msg


def test_a_normal_registration_file_fits_comfortably(tmp_path, monkeypatch):
    from modules.eip8004.registration import build_registration_file
    from tools.defi.agent_registration import build_agent_uri
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    doc = build_registration_file("http://localhost:9000").model_dump(exclude_none=True)
    uri = build_agent_uri(doc, base_url=None)
    assert len(uri) < 8000, f"a plain registration file should be small: {len(uri)}"


# --- calldata -------------------------------------------------------------

def test_register_encodes_the_single_string_overload():
    from tools.defi.agent_registration import encode_register
    data = encode_register("data:application/json;base64,eyJhIjoxfQ==")
    # keccak("register(string)")[:4]
    from eth_utils import keccak
    assert data.startswith("0x" + keccak(b"register(string)").hex()[:8])


def test_set_agent_uri_encodes_the_update_call():
    from eth_utils import keccak

    from tools.defi.agent_registration import encode_set_agent_uri
    data = encode_set_agent_uri(42, "https://x.test/a.json")
    assert data.startswith("0x" + keccak(b"setAgentURI(uint256,string)").hex()[:8])


# --- the intent -----------------------------------------------------------

def test_the_intent_pins_the_registry_and_declares_the_shape():
    from core.wallet import erc8004
    from tools.defi.agent_registration import build_registration_intent
    intent = build_registration_intent(chain="base", max_spend_usd=5.0,
                                       idempotency_key="k")
    assert intent.is_registration is True
    assert intent.amount_raw == 0
    assert intent.token is None
    pinned = erc8004.resolve_identity_registry("base")
    assert intent.to.lower() == pinned.lower()
    assert intent.expected_registry.lower() == pinned.lower()


def test_an_unsupported_chain_refuses_before_any_transaction():
    from tools.defi.agent_registration import build_registration_intent
    with pytest.raises(ValueError) as exc:
        build_registration_intent(chain="robinhood", max_spend_usd=5.0,
                                  idempotency_key="k")
    assert "robinhood" in str(exc.value)


def test_the_intent_clears_the_real_guard():
    """End to end against tx_guard, not a stub."""
    from core.wallet import erc8004, tx_guard
    from core.wallet.policy import PolicyGate
    from core.wallet.simulation import Deltas
    from tools.defi.agent_registration import build_registration_intent

    reg = erc8004.resolve_identity_registry("base").lower()
    intent = build_registration_intent(chain="base", max_spend_usd=50.0,
                                       idempotency_key="k")
    deltas = Deltas(ok=True, native_delta=0, token_deltas={}, allowance_deltas={},
                    gas_used=300_000,
                    holder_nft_in=((reg, "erc721",
                                    "0x" + "0" * 40, 7, 1),))
    d = tx_guard.authorize(
        intent, {"to": reg, "data": "0x1aa3a008", "value": 0, "chainId": 8453,
                 "nonce": 1, "gas": 400_000, "maxFeePerGas": 10 ** 9},
        holder="0x" + "11" * 20,
        gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0),
        execution_context=None, simulate_fn=lambda **_: deltas,
        price_fn=lambda chain, addr: 3000.0,
        rpc_is_pinned_fn=lambda chain: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False)
    assert d.allowed is True, d.reason
    assert d.agent_id == 7


# --- double registration --------------------------------------------------

def test_an_existing_agent_id_refuses_and_names_it():
    """⚠️ register() is NOT idempotent — a second call mints a SECOND token and
    splits the identity."""
    from tools.defi.agent_registration import check_not_already_registered
    err = check_not_already_registered(existing_agent_id=42, chain="base")
    assert err is not None
    assert "42" in err


def test_no_existing_id_is_clear_to_proceed():
    from tools.defi.agent_registration import check_not_already_registered
    assert check_not_already_registered(existing_agent_id=None, chain="base") is None
