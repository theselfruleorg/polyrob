"""066 P2/P3 — the typed schemas, the socket, the policy file.

x402 (EIP-3009), Hyperliquid L1 orders, the EIP-8004 feedback authorization:
each signs ONLY its exact shape; anything near it refuses. The socket refuses a
peer by its kernel UID before reading a request.
"""
import os
import socket
import tempfile
import threading
import time

import pytest

from core.signer import protocol
from tests.unit.core.signer.conftest import AGENT_UID, make_config


def _ok(resp):
    assert resp["ok"], resp
    return resp["result"]


def _refused(resp, code):
    assert resp["ok"] is False, resp
    assert resp["code"] == code, resp
    return resp


# -- x402 --------------------------------------------------------------------

def _x402(rig, **over):
    from core.wallet import chains
    base = chains.get("base")
    now = int(time.time())
    msg = {"from": rig.service.wallet.operational_signer().address,
           "to": "0x" + "c" * 40, "value": 1_500_000, "validAfter": now - 5,
           "validBefore": now + 300, "nonce": "0x" + "ab" * 32}
    msg.update(over.pop("message", {}))
    body = {"domain": {"name": "USD Coin", "version": "2", "chainId": base.chain_id,
                       "verifyingContract": base.usdc},
            "types": {"TransferWithAuthorization": [
                {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]},
            "primary_type": "TransferWithAuthorization", "message": msg}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(body.get(k), dict):
            body[k] = dict(body[k], **v)
        else:
            body[k] = v
    return body


def test_x402_signs_eip3009_and_it_recovers_to_the_operational_address(rig):
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    body = _x402(rig)
    sig = _ok(rig.call("x402.authorize", body))["signature"]
    msg = dict(body["message"], nonce=bytes.fromhex(body["message"]["nonce"][2:]))
    signable = encode_typed_data(domain_data=body["domain"], message_types=body["types"],
                                 message_data=msg)
    assert Account.recover_message(signable, signature=sig) == \
        rig.service.wallet.operational_signer().address
    assert rig.service.gate.audit_log[-1]["venue"] == "x402"


@pytest.mark.parametrize("over,code", [
    ({"domain": {"name": "Tether"}}, protocol.UNKNOWN_SHAPE),
    ({"domain": {"verifyingContract": "0x" + "d" * 40}}, protocol.UNKNOWN_SHAPE),
    ({"message": {"validBefore": int(time.time()) + 3600}}, protocol.UNKNOWN_SHAPE),
    ({"message": {"from": "0x" + "e" * 40}}, protocol.UNKNOWN_SHAPE),
    ({"message": {"value": 500_000_000}}, protocol.APPROVAL_REQUIRED),   # $500 > $50 cap
    ({"primary_type": "Permit"}, protocol.UNKNOWN_SHAPE),
])
def test_x402_refuses_anything_but_a_bounded_usdc_authorization(rig, over, code):
    _refused(rig.call("x402.authorize", _x402(rig, **over)), code)


def test_x402_replay_of_the_same_nonce_is_refused(rig):
    _ok(rig.call("x402.authorize", _x402(rig)))
    _refused(rig.call("x402.authorize", _x402(rig)), protocol.GUARD_REFUSED)


def test_x402_through_the_sdk_adapter_and_a_remote_account(rig):
    """The x402 SDK wraps an account in EthAccountSigner; a RemoteAccount works there."""
    pytest.importorskip("x402")
    from x402.mechanisms.evm.signers import EthAccountSigner
    from core.signer.remote import RemoteAccount
    from tests.unit.core.signer.conftest import LoopbackClient
    body = _x402(rig)
    address = rig.service.wallet.operational_signer().address
    signer = EthAccountSigner(RemoteAccount(LoopbackClient(rig.service), address, "treasury"))
    msg = dict(body["message"], nonce=bytes.fromhex(body["message"]["nonce"][2:]))
    sig = signer.sign_typed_data(body["domain"], body["types"], "TransferWithAuthorization", msg)
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    signable = encode_typed_data(domain_data=body["domain"], message_types=body["types"],
                                 message_data=msg)
    assert Account.recover_message(signable, signature=sig) == address


# -- 066 P3: Hyperliquid orders ------------------------------------------------

def _hl_order():
    return {"type": "order", "orders": [{"a": 0, "b": True, "p": "100", "s": "0.1", "r": False,
                                        "t": {"limit": {"tif": "Gtc"}}}], "grouping": "na"}


def test_hyperliquid_cancellations_sign_and_recover_to_the_hl_key(make_rig):
    signing = pytest.importorskip("hyperliquid.utils.signing")
    rig = make_rig(hyperliquid_orders=True)
    action, nonce = {"type": "cancel", "cancels": [{"a": 0, "o": 1}]}, 1_700_000_000_000
    sig = _ok(rig.call("venue.sign", {"venue": "hyperliquid", "action": action,
                                      "nonce": nonce, "is_mainnet": True}))["signature"]
    who = signing.recover_agent_or_user_from_l1_action(action, sig, None, nonce, None, True)
    assert who == rig.service.wallet.signer_for("hyperliquid").address


@pytest.mark.parametrize("action", [
    _hl_order(),
    {"type": "modify", "oid": 1, "order": _hl_order()["orders"][0]},
    {"type": "batchModify", "modifies": []},
    {"type": "updateLeverage", "asset": 0, "isCross": True, "leverage": 100},
    {"type": "updateIsolatedMargin", "asset": 0, "isBuy": True, "ntli": 1000000000},
    {"type": "withdraw3", "destination": "0x" + "1" * 40, "amount": "100"},
    {"type": "usdSend", "destination": "0x" + "1" * 40, "amount": "100"},
    {"type": "approveAgent", "agentAddress": "0x" + "1" * 40},
    {"type": "vaultTransfer", "vaultAddress": "0x" + "1" * 40, "isDeposit": True, "usd": 1},
])
def test_hyperliquid_non_order_actions_are_refused(make_rig, action):
    rig = make_rig(hyperliquid_orders=True)
    _refused(rig.call("venue.sign", {"venue": "hyperliquid", "action": action,
                                     "nonce": 1, "is_mainnet": True}), protocol.UNKNOWN_SHAPE)


def test_hyperliquid_vault_and_network_mismatch_are_refused(make_rig):
    rig = make_rig(hyperliquid_orders=True)
    base = {"venue": "hyperliquid", "action": _hl_order(), "nonce": 1}
    _refused(rig.call("venue.sign", dict(base, is_mainnet=False)), protocol.UNKNOWN_SHAPE)
    _refused(rig.call("venue.sign", dict(base, is_mainnet=True, vault_address="0x" + "1" * 40)),
             protocol.UNKNOWN_SHAPE)


def test_hyperliquid_signing_is_off_unless_signer_toml_arms_it(rig):
    _refused(rig.call("venue.sign", {"venue": "hyperliquid", "action": _hl_order(),
                                     "nonce": 1, "is_mainnet": True}), protocol.NOT_CONFIGURED)


def test_the_hl_sdk_hook_routes_a_remote_account_to_the_signer(make_rig):
    pytest.importorskip("hyperliquid.exchange")
    import hyperliquid.exchange as ex
    from core.signer.remote import RemoteAccount
    from tests.unit.core.signer.conftest import LoopbackClient
    remote_signing = pytest.importorskip("polyrob_markets.hyperliquid.remote_signing")
    rig = make_rig(hyperliquid_orders=True)
    assert remote_signing.install()
    account = RemoteAccount(LoopbackClient(rig.service),
                            rig.service.wallet.signer_for("hyperliquid").address, "hyperliquid")
    sig = ex.sign_l1_action(account, {"type": "cancel", "cancels": [{"a": 0, "o": 1}]},
                            None, 5, None, True)
    assert set(sig) == {"r", "s", "v"}
    # A user-signed action (a withdrawal) reaches sign_message and refuses.
    from core.wallet.agent_wallet import WalletSigningUnavailable
    with pytest.raises(WalletSigningUnavailable):
        ex.sign_withdraw_from_bridge_action(account, {"destination": "0x" + "1" * 40,
                                                     "amount": "1", "time": 1}, True)


# -- 066 P3: EIP-8004 ----------------------------------------------------------

def _feedback(expires):
    return {"types": {"EIP712Domain": [
        {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
        "FeedbackAuth": [
        {"name": "agentId", "type": "uint256"}, {"name": "clientAddress", "type": "address"},
        {"name": "expiresAt", "type": "uint256"}, {"name": "nonce", "type": "string"}]},
        "primaryType": "FeedbackAuth",
        "domain": {"name": "EIP8004ReputationRegistry", "version": "1", "chainId": 8453,
                   "verifyingContract": "0x" + "0" * 40},
        "message": {"agentId": 7, "clientAddress": "0x" + "c" * 40, "expiresAt": expires,
                    "nonce": "n1"}}


def test_eip8004_feedback_auth_signs_with_the_held_key(rig):
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    typed = _feedback(int(time.time()) + 86400)
    sig = _ok(rig.call("eip8004.feedback_auth", {"typed": typed}))["signature"]
    want = Account.from_key(rig.secrets["EIP8004_AGENT_PRIVATE_KEY"]).address
    assert Account.recover_message(encode_typed_data(full_message=typed), signature=sig) == want


def test_eip8004_refuses_another_domain_or_a_long_expiry(rig):
    typed = _feedback(int(time.time()) + 86400)
    typed["domain"]["name"] = "SomethingElse"
    _refused(rig.call("eip8004.feedback_auth", {"typed": typed}), protocol.UNKNOWN_SHAPE)
    _refused(rig.call("eip8004.feedback_auth",
                      {"typed": _feedback(int(time.time()) + 90 * 86400)}), protocol.UNKNOWN_SHAPE)


def test_eip8004_without_the_key_is_not_configured(rig):
    rig.secrets.pop("EIP8004_AGENT_PRIVATE_KEY")
    _refused(rig.call("eip8004.feedback_auth", {"typed": _feedback(int(time.time()) + 60)}),
             protocol.NOT_CONFIGURED)


# -- the socket --------------------------------------------------------------

@pytest.fixture
def short_sock_dir():
    d = tempfile.mkdtemp(prefix="sg", dir="/tmp")   # AF_UNIX paths are short on macOS
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _serve(service, path):
    from core.signer.server import SignerServer
    server = SignerServer(service, path)
    server.bind()
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def test_the_socket_refuses_a_uid_that_is_not_a_client(rig, short_sock_dir):
    from core.signer.client import SignerClient, SignerRefused
    assert os.getuid() != 0 and os.getuid() not in rig.service.config.client_uids
    path = os.path.join(short_sock_dir, "s.sock")
    server = _serve(rig.service, path)
    try:
        assert oct(os.stat(path).st_mode & 0o777) == "0o660"
        with pytest.raises(SignerRefused) as exc:
            SignerClient(path, timeout=5).call("identity")
        assert exc.value.code == protocol.PEER_REFUSED
    finally:
        server.stop()


def test_the_socket_serves_a_configured_client_uid(make_rig, short_sock_dir):
    import dataclasses
    from core.signer.client import SignerClient
    rig = make_rig()
    rig.service.config = dataclasses.replace(rig.service.config, client_uids=(os.getuid(),))
    path = os.path.join(short_sock_dir, "s.sock")
    server = _serve(rig.service, path)
    try:
        ident = SignerClient(path, timeout=5).call("identity")
        assert ident["evm"]["treasury"] == rig.service.wallet.address
    finally:
        server.stop()


def test_peer_uid_reads_the_kernel_credentials():
    from core.signer.server import peer_uid_of
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert peer_uid_of(a) == os.getuid()
    finally:
        a.close()
        b.close()


def test_a_frame_above_the_limit_is_refused():
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_frame({"x": "a" * (protocol.MAX_FRAME + 1)})


def test_the_client_says_whether_the_request_left_the_process(short_sock_dir):
    from core.signer.client import SignerClient, SignerUnavailable
    with pytest.raises(SignerUnavailable) as exc:
        SignerClient(os.path.join(short_sock_dir, "absent.sock"), timeout=1).call("ping")
    assert exc.value.sent is False


# -- signer.toml ---------------------------------------------------------------

def test_signer_toml_round_trips(signer_home, tmp_path):
    from core.signer.caps import load_signer_config, render_signer_toml
    cfg = make_config(signer_home, identity={"treasury": "0x" + "1" * 40, "solana": "So1"},
                      env={"DEFI_EVM_RPC_BASE": "https://rpc.example/key"})
    path = tmp_path / "signer.toml"
    path.write_text(render_signer_toml(cfg))
    again = load_signer_config(str(path))
    assert again == cfg


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d["caps"].pop("per_tx_usd"), "per_tx_usd is required"),
    (lambda d: d["caps"].update(daily_usd=float("inf")), "finite"),
    (lambda d: d["caps"].update(extra=1), "unknown key"),
    (lambda d: d.update(loosen={"x": 1}), "unknown table"),
    (lambda d: d.update(env={"DEFI_AUTONOMOUS_TURN_TRADING": "true"}), "RPC endpoints"),
    (lambda d: d["server"].update(client_uids=[0]), "positive UIDs"),
])
def test_signer_toml_is_strict(mutate, match):
    from core.signer.caps import SignerConfigError, parse_signer_config
    data = {"caps": {"per_tx_usd": 1.0, "daily_usd": 2.0}, "policy": {"chains": ["base"]},
            "server": {"client_uids": [AGENT_UID]}}
    mutate(data)
    with pytest.raises(SignerConfigError, match=match):
        parse_signer_config(data)


def test_eip8004_refuses_a_domain_that_is_not_a_pinned_reputation_registry(rig):
    from core.wallet import erc8004
    typed = _feedback(int(time.time()) + 86400)
    typed["domain"]["verifyingContract"] = "0x" + "9" * 40          # some other contract
    _refused(rig.call("eip8004.feedback_auth", {"typed": typed}), protocol.UNKNOWN_SHAPE)
    typed["domain"]["verifyingContract"] = erc8004.registry_for("base").reputation
    typed["domain"]["chainId"] = 1                                  # the pinned address, wrong chain row
    typed["domain"]["verifyingContract"] = erc8004.registry_for("base-sepolia").reputation
    _refused(rig.call("eip8004.feedback_auth", {"typed": typed}), protocol.UNKNOWN_SHAPE)
    typed["domain"]["chainId"] = 84532
    assert _ok(rig.call("eip8004.feedback_auth", {"typed": typed}))["signature"]
