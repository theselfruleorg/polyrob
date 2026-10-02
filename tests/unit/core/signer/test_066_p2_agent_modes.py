"""066 P2 — the agent side: ``WALLET_SIGNER`` = local | shadow | remote.

* local  — nothing changes (no attestation, the plain AgentWallet).
* shadow — the local key signs as today; the signer's verdict is logged; a
  disagreement or a dead signer NEVER blocks.
* remote — no key material in the agent process; every EVM send goes
  ``tx_guard.authorize`` → attestation → ``EvmRail`` → the signer, which runs
  the guard again and signs.
"""
import json
import os

import pytest

from core.signer import protocol
from tests.unit.core.signer.conftest import (AGENT_UID, AMOUNT, ETH_PRICE, RELAY, SEED, FakeRail,
                                             LoopbackClient, native_deltas, native_intent,
                                             native_tx)


def _authorize(intent, tx, gate):
    from core.wallet import tx_guard
    return tx_guard.authorize(
        intent, tx, holder="0x" + "2" * 40, gate=gate, execution_context=None,
        simulate_fn=lambda **_: native_deltas(), price_fn=lambda c, a: ETH_PRICE,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False)


def _gate():
    from core.wallet.policy import PolicyGate
    return PolicyGate(max_per_tx_usd=500.0, daily_cap_usd=1000.0)


class _RailNoRpc:
    """An EvmRail with its preflight answered locally (no node)."""

    @staticmethod
    def make(signer):
        from core.wallet.broadcast.evm import EvmRail
        rail = EvmRail("base", signer)
        rail.preflight = lambda: (True, "")
        return rail


# -- local: zero change ------------------------------------------------------------

def test_local_mode_records_no_attestation(signer_home):
    from core.signer import attest
    tx = native_tx()
    assert _authorize(native_intent(), tx, _gate()).allowed
    assert attest.peek(tx) is None


def test_local_mode_builds_the_plain_wallet(signer_home, monkeypatch):
    import core.wallet.factory as factory
    from core.wallet.agent_wallet import AgentWallet
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    factory.reset_agent_wallet_cache()
    try:
        w = factory.get_agent_wallet()
        assert type(w) is AgentWallet
        assert type(w.operational_signer()).__name__ == "LocalEoaSigner"
    finally:
        factory.reset_agent_wallet_cache()


# -- remote --------------------------------------------------------------------------

@pytest.fixture
def remote(rig, monkeypatch):
    """The agent process in remote mode, wired to the in-process signer."""
    import core.wallet.factory as factory
    from core.security import custody_env
    monkeypatch.setenv("WALLET_SIGNER", "remote")
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    # The agent was (wrongly) given the seed: remote must drop it.
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    monkeypatch.setenv("EIP8004_AGENT_PRIVATE_KEY", "0x" + "11" * 32)
    client = LoopbackClient(rig.service)
    import core.signer.client as client_mod
    monkeypatch.setattr(client_mod, "SignerClient", lambda *a, **k: client)
    factory.reset_agent_wallet_cache()
    wallet = factory.get_agent_wallet()
    yield wallet, client, rig
    factory.reset_agent_wallet_cache()
    custody_env._reset_for_tests()


def test_remote_mode_holds_no_key_material(remote):
    from core.security import custody_env
    from core.signer.remote import RemoteWallet
    from core.wallet.signer import LocalEoaSigner
    wallet, _client, rig = remote
    assert isinstance(wallet, RemoteWallet)
    for name in custody_env.CUSTODY_SECRET_ENV:
        assert name not in os.environ
        assert custody_env.custody_secret(name) is None
    assert not custody_env.holds_custody_secret()
    assert not wallet._seed
    assert not wallet.config.master_seed
    for venue in ("treasury", "x402", "polymarket", "hyperliquid"):
        wallet.signer_for(venue)
    assert not any(isinstance(s, LocalEoaSigner) for s in wallet._signers.values())
    # and the addresses are the signer's, i.e. the wallet of record
    assert wallet.address == rig.service.wallet.address


def test_remote_mode_refuses_every_generic_signature(remote):
    from core.wallet.agent_wallet import WalletSigningUnavailable
    wallet, _client, _rig = remote
    signer = wallet.operational_signer()
    for attempt in (lambda: signer.sign_message(b"x"),
                    lambda: signer.sign_typed_data({}, {}, {}),
                    lambda: signer.sign_transaction(native_tx()),
                    lambda: signer.account.key,
                    lambda: signer.account.sign_message(b"x"),
                    lambda: signer.account.sign_typed_data({}, {"Permit": []}, {}),
                    lambda: wallet.solana_signer().sign_transaction(object())):
        with pytest.raises(WalletSigningUnavailable):
            attempt()


def test_remote_send_runs_the_guard_on_both_sides(remote):
    from core.wallet import submission_journal
    wallet, client, rig = remote
    intent, tx = native_intent(), native_tx()
    assert _authorize(intent, tx, _gate()).allowed          # the agent's own guard
    tx_hash = _RailNoRpc.make(wallet.operational_signer()).sign_and_send(tx)
    assert [op for op, _ in client.calls if op == "evm.send"] == ["evm.send"]
    assert FakeRail.sent[0]["hash"] == tx_hash                # the signer signed it
    assert FakeRail.sent[0]["from"] == wallet.address
    rows = submission_journal.unresolved()
    assert [r["tx_hash"] for r in rows] == [tx_hash]           # agent interlock until booked


def test_remote_send_without_a_guard_decision_is_refused(remote):
    from core.wallet.broadcast.evm import BroadcastError
    wallet, client, _rig = remote
    with pytest.raises(BroadcastError, match="no tx_guard authorization"):
        _RailNoRpc.make(wallet.operational_signer()).sign_and_send(native_tx())
    assert FakeRail.sent == []
    assert not [op for op, _ in client.calls if op == "evm.send"]


def test_remote_send_the_signer_refuses_is_not_sent(remote, monkeypatch):
    from core.wallet.broadcast.evm import BroadcastError
    wallet, _client, rig = remote
    rig.deltas = native_deltas(native_delta=-2 * AMOUNT)       # the signer's simulation disagrees
    intent, tx = native_intent(), native_tx()
    assert _authorize(intent, tx, _gate()).allowed            # the agent's sim said fine
    with pytest.raises(BroadcastError, match="polyrob-signer refused"):
        _RailNoRpc.make(wallet.operational_signer()).sign_and_send(tx)
    assert FakeRail.sent == []


def test_remote_send_with_a_lost_answer_holds_the_interlock(remote):
    from core.wallet import submission_journal
    wallet, client, _rig = remote
    client.sent_then_down = True
    intent, tx = native_intent(), native_tx()
    assert _authorize(intent, tx, _gate()).allowed
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        _RailNoRpc.make(wallet.operational_signer()).sign_and_send(tx)
    assert submission_journal.unresolved()                   # no fresh send until reconciled


def test_a_remote_signer_on_another_wallet_is_not_verified(rig, monkeypatch, signer_home):
    import core.wallet.factory as factory
    from core.signer import remote as remote_mod
    from core.wallet import public_identity
    from core.wallet.agent_wallet import WalletSigningUnavailable
    monkeypatch.setenv("WALLET_SIGNER", "remote")
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setattr(public_identity, "read_public_identity",
                        lambda *a, **k: {"evm": {"treasury": "0x" + "1" * 40}, "scheme": "legacy"})
    client = LoopbackClient(rig.service)
    wallet = remote_mod.build_remote_wallet(factory.load_wallet_config(), [], client=client)
    assert not remote_mod.remote_verified()
    with pytest.raises(WalletSigningUnavailable, match="not VERIFIED"):
        wallet.operational_signer().send_transaction(native_tx(), chain="base", intent=native_intent())


# -- shadow ------------------------------------------------------------------------

@pytest.fixture
def shadow(signer_home, monkeypatch):
    monkeypatch.setenv("WALLET_SIGNER", "shadow")

    def _make(service, **client_kw):
        from core.signer.shadow import ShadowEvmSigner
        from core.wallet.signer import LocalEoaSigner
        local = service.wallet.operational_signer()   # the same key in both, as on prod
        assert isinstance(local, LocalEoaSigner)
        return ShadowEvmSigner(local, client=LoopbackClient(service, **client_kw))
    return _make


def _shadow_log():
    from core.signer.shadow import shadow_log_path
    with open(shadow_log_path()) as fh:
        return [json.loads(line) for line in fh]


def test_shadow_agree_is_logged_and_the_local_key_signs(rig, shadow):
    signer = shadow(rig.service)
    intent, tx = native_intent(), native_tx()
    assert _authorize(intent, tx, _gate()).allowed
    raw = signer.sign_transaction(tx)
    assert raw and FakeRail.sent == []                        # the signer did not sign
    assert [e["kind"] for e in _shadow_log()] == ["agree"]


def test_shadow_disagreement_is_logged_and_never_blocks(make_rig, shadow):
    rig = make_rig(per_tx_usd=5.0)                            # the signer's hard cap is lower
    signer = shadow(rig.service)
    intent, tx = native_intent(), native_tx()
    assert _authorize(intent, tx, _gate()).allowed
    raw = signer.sign_transaction(tx)                         # still signed locally
    assert raw
    log = _shadow_log()
    assert [e["kind"] for e in log] == ["disagree"]
    assert log[0]["code"] == protocol.APPROVAL_REQUIRED


def test_shadow_with_the_signer_down_logs_unreachable_and_signs(rig, shadow):
    signer = shadow(rig.service, down=True)
    intent, tx = native_intent(), native_tx()
    assert _authorize(intent, tx, _gate()).allowed
    assert signer.sign_transaction(tx)
    assert [e["kind"] for e in _shadow_log()] == ["unreachable"]


def test_shadow_flags_a_send_that_skipped_the_guard(rig, shadow):
    signer = shadow(rig.service)
    assert signer.sign_transaction(native_tx())
    assert [e["kind"] for e in _shadow_log()] == ["no_intent"]


def test_the_cutover_criterion(signer_home, monkeypatch):
    import time
    from core.signer import shadow as sh
    now = time.time()
    assert sh.cutover_ready(now=now)[0] is False               # nothing compared yet
    monkeypatch.setattr(sh.time, "time", lambda: now - 7 * 86400 + 60)
    sh.log_comparison("agree", chain="base")
    monkeypatch.setattr(sh.time, "time", lambda: now)
    sh.log_comparison("agree", chain="base")
    ok, why, counts = sh.cutover_ready(now=now)
    assert ok, why
    sh.log_comparison("disagree", code="guard_refused", reason="x")
    assert sh.cutover_ready(now=now)[0] is False


def test_the_shadow_factory_wraps_only_the_evm_signers(signer_home, monkeypatch):
    import core.wallet.factory as factory
    from core.signer.shadow import ShadowEvmSigner
    monkeypatch.setenv("WALLET_SIGNER", "shadow")
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    factory.reset_agent_wallet_cache()
    try:
        w = factory.get_agent_wallet()
        assert isinstance(w.operational_signer(), ShadowEvmSigner)
        assert w.address == w.signer_for("treasury").address
    finally:
        factory.reset_agent_wallet_cache()


# -- 066 §5.6: host execution ----------------------------------------------------------

def test_host_execution_relaxes_only_when_remote_is_active_and_verified(signer_home, monkeypatch):
    from core.security import custody_env
    from core.security.host_execution import host_execution_refusal
    from core.signer import remote as remote_mod
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    assert host_execution_refusal()                           # local custody: refused
    monkeypatch.setenv("WALLET_SIGNER", "remote")
    assert host_execution_refusal()                           # remote but not verified
    remote_mod._set_verified(True, "test")
    assert host_execution_refusal() is None                   # remote + verified + no key
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)
    assert host_execution_refusal()                           # a key is back in the env
    monkeypatch.delenv("AGENT_WALLET_MASTER_SEED")
    monkeypatch.setenv("WALLET_SIGNER", "shadow")
    assert host_execution_refusal()                           # shadow keeps the key: refused
    custody_env._reset_for_tests()


def test_a_later_env_load_cannot_hand_a_remote_agent_the_seed_back(signer_home, monkeypatch):
    from core.security import custody_env
    monkeypatch.setenv("WALLET_SIGNER", "remote")
    custody_env.discard_custody_secrets()
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", SEED)     # a dotenv layered it back
    custody_env.repop_after_env_load()
    assert "AGENT_WALLET_MASTER_SEED" not in os.environ
    assert custody_env.custody_secret("AGENT_WALLET_MASTER_SEED") is None
    custody_env._reset_for_tests()


# -- D5: approvals ride the ONE owner queue -------------------------------------------

def test_signer_approvals_join_the_one_queue_and_need_root(make_rig, monkeypatch, tmp_path):
    from core.signer import approvals
    from tools.controller import approval_queue
    rig = make_rig(per_tx_usd=5.0)
    resp = rig.call("evm.send", rig.body())
    aid = resp["approval_id"]
    agent = LoopbackClient(rig.service)                       # a Telegram /approve = agent UID
    owner = LoopbackClient(rig.service, uid=0)                # sudo polyrob owner promote
    import core.signer.client as client_mod
    monkeypatch.setattr(client_mod, "SignerClient", lambda *a, **k: agent)
    monkeypatch.setenv("WALLET_SIGNER", "shadow")
    items = approvals.pending_items()
    assert [(i["kind"], i["id"]) for i in items] == [("signer_approval", aid)]
    assert approval_queue.needs_individual_decision(items[0])
    ok, msg = approval_queue.decide_pending("signer_approval", aid, approve=True, user_id="u",
                                            home_dir=str(tmp_path), instance_id="i")
    assert not ok and "sudo polyrob owner promote signer_approval" in msg
    monkeypatch.setattr(client_mod, "SignerClient", lambda *a, **k: owner)
    ok, msg = approval_queue.decide_pending("signer_approval", aid, approve=True, user_id="u",
                                            home_dir=str(tmp_path), instance_id="i")
    assert ok, msg
    assert rig.call("evm.send", rig.body())["ok"]


# -- status --------------------------------------------------------------------------------

def test_custody_status_names_an_unverified_remote_signer(signer_home, monkeypatch):
    from core.status_custody import custody_section
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("WALLET_SIGNER", "remote")
    import core.signer.client as client_mod
    from core.signer.client import SignerUnavailable

    class Down:
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            raise SignerUnavailable("down")
    monkeypatch.setattr(client_mod, "SignerClient", Down)
    sec = custody_section()
    keys = {h.key for h in sec.health}
    assert {"signer_unverified", "signer_unreachable"} <= keys
    assert any("REMOTE" in line for line in sec.lines)


def test_custody_status_counts_shadow_comparisons(rig, signer_home, monkeypatch):
    from core.signer import shadow as sh
    from core.status_custody import custody_section
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    monkeypatch.setenv("WALLET_SIGNER", "shadow")
    import core.signer.client as client_mod
    monkeypatch.setattr(client_mod, "SignerClient", lambda *a, **k: LoopbackClient(rig.service))
    sh.log_comparison("agree")
    sh.log_comparison("disagree", code="guard_refused", reason="sim differs")
    sec = custody_section()
    text = "\n".join(sec.lines)
    assert "1 agree · 1 disagree" in text and "reachable" in text
    assert "signer_shadow_disagree" in {h.key for h in sec.health}
