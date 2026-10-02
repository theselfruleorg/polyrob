"""Fixtures for the 066 P2 signer tests.

``rig`` builds a real :class:`core.signer.server.SignerService` over a real
seed-derived wallet in a temp state dir. Only the network is faked: the
simulation (a native send of 0.005 ETH), the price ($2000/ETH), the chain's
pending nonce and the broadcast (``FakeRail`` signs with the REAL key and
returns the real keccak hash, never touching a node).

``LoopbackClient`` is a ``SignerClient`` that hands each request to
``service.handle`` in process, as a given UID — so an agent-side test runs the
whole chain: ``tx_guard.authorize`` → attestation → ``EvmRail`` → remote signer
→ the signer's own guard → signature.
"""
import pytest

from core.signer import protocol
from core.signer.caps import parse_signer_config
from core.signer.client import SignerRefused, SignerUnavailable

SEED = "ab" * 24
from eth_utils import to_checksum_address

RELAY = to_checksum_address("0x" + "a" * 40)
OTHER = to_checksum_address("0x" + "b" * 40)
AMOUNT = 5 * 10 ** 15          # 0.005 ETH = $10 at $2000
ETH_PRICE = 2000.0
AGENT_UID = 4242


class FakeRail:
    """Signs with the real key; returns the real hash; sends nothing."""
    sent = []

    def __init__(self, chain, signer, **kw):
        self.chain = chain
        self._signer = signer
        self.kw = kw

    def sign_and_send(self, tx):
        from eth_utils import keccak
        raw = self._signer.sign_transaction(tx)
        tx_hash = "0x" + keccak(bytes(raw)).hex()
        FakeRail.sent.append({"chain": self.chain, "tx": dict(tx), "raw": raw,
                              "from": self._signer.address, "hash": tx_hash})
        return tx_hash

    def _rpc(self, method, params, timeout=8.0):
        raise AssertionError("no RPC in this test")


class LoopbackClient:
    def __init__(self, service, uid=AGENT_UID, *, down=False, sent_then_down=False):
        self.service = service
        self.uid = uid
        self.down = down
        self.sent_then_down = sent_then_down
        self.calls = []

    def call(self, op, body=None, *, timeout=None):
        self.calls.append((op, body))
        if self.down:
            raise SignerUnavailable("loopback is down", sent=False)
        resp = self.service.handle(protocol.request(op, body), self.uid)
        if self.sent_then_down:
            raise SignerUnavailable("loopback lost the answer", sent=True)
        if resp.get("ok"):
            return resp["result"]
        extra = {k: v for k, v in resp.items() if k not in ("v", "ok", "code", "reason")}
        raise SignerRefused(resp["code"], resp["reason"], extra)


def make_config(state_dir, **over):
    data = {
        "caps": {"per_tx_usd": over.pop("per_tx_usd", 50.0),
                 "daily_usd": over.pop("daily_usd", 100.0)},
        "policy": {"chains": ["base"],
                   "hyperliquid_orders": over.pop("hyperliquid_orders", False)},
        "server": {"state_dir": str(state_dir), "client_uids": [AGENT_UID],
                   "socket": str(state_dir / "signer.sock")},
        "identity": {"network": "mainnet"},
    }
    for table, values in over.items():
        data.setdefault(table, {}).update(values)
    return parse_signer_config(data)


def native_deltas(native_delta=-AMOUNT):
    from core.wallet.simulation import Deltas
    return Deltas(ok=True, native_delta=native_delta, token_deltas={},
                  allowance_deltas={}, gas_used=90_000)


def native_intent(**kw):
    from core.wallet import tx_guard
    base = dict(chain="base", token=None, to=RELAY, amount_raw=AMOUNT, max_spend_usd=100.0,
                expected_allowance_grants=(), idempotency_key="native-1")
    base.update(kw)
    return tx_guard.TxIntent(**base)


def native_tx(nonce=7, **kw):
    tx = {"to": RELAY, "value": AMOUNT, "data": "0xdeadbeef", "nonce": nonce, "gas": 100_000,
          "maxFeePerGas": 10 ** 9, "maxPriorityFeePerGas": 10 ** 8, "chainId": 8453, "type": 2}
    tx.update(kw)
    return tx


class Rig:
    def __init__(self, service, state_dir, nonces):
        self.service = service
        self.state_dir = state_dir
        self.nonces = nonces
        self.deltas = native_deltas()

    def body(self, intent=None, tx=None):
        return {"intent": protocol.intent_to_wire(intent or native_intent()),
                "tx": protocol.tx_to_wire(tx or native_tx())}

    def call(self, op, body=None, uid=AGENT_UID):
        return self.service.handle(protocol.request(op, body), uid)


@pytest.fixture
def signer_home(tmp_path, monkeypatch):
    state = tmp_path / "signer"
    state.mkdir()
    monkeypatch.setenv("POLYROB_DATA_DIR", str(state))
    monkeypatch.setenv("AGENT_WALLET_DERIVATION", "legacy")
    monkeypatch.delenv("WALLET_SIGNER", raising=False)
    FakeRail.sent = []
    from core.signer import attest
    attest._reset_for_tests()
    return state


@pytest.fixture
def make_rig(signer_home):
    def _make(**over):
        from core.signer.runtime import build_signer_wallet
        from core.signer.server import SignerService
        from core.signer.store import SignerStore
        cfg = make_config(signer_home, **over)
        wallet = build_signer_wallet(cfg, SEED)
        nonces = {"next": 7}
        holder = {}
        rig = Rig(None, signer_home, nonces)
        secrets = {"EIP8004_AGENT_PRIVATE_KEY": "0x" + "11" * 32,
                   "PAYMENT_MASTER_SEED": "p" * 40}
        service = SignerService(
            cfg, wallet, store=SignerStore(str(signer_home)),
            simulate_fn=lambda **_: rig.deltas, price_fn=lambda chain, addr: ETH_PRICE,
            rpc_is_pinned_fn=lambda chain: True, rail_factory=FakeRail,
            pending_nonce_fn=lambda chain, h: nonces["next"],
            secret_fn=lambda name: secrets.get(name))
        rig.service = service
        rig.secrets = secrets
        rig.holder = holder
        return rig
    return _make


@pytest.fixture
def rig(make_rig):
    return make_rig()


@pytest.fixture(autouse=True)
def _clean_signer_latches():
    yield
    from core.signer import attest, remote
    attest._reset_for_tests()
    remote._reset_for_tests()
