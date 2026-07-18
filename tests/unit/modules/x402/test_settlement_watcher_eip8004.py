"""Task 15 (Phase 4): the settlement watcher offers an ERC-8004 payment-backed
feedback authorization to an identifiable (correspondent-linked) payer once
their invoice settles — the "anti-sybil signal 8004 was designed for". Gated
`EIP8004_PAYMENT_FEEDBACK` (rides `EIP8004_ENABLED`); default OFF is
byte-identical to the pre-existing settlement path (no 8004 calls at all).

The manager's `create_feedback_auth` is spied via constructor injection
(`SettlementWatcher(..., reputation_manager=...)`, mirroring the existing
`goal_board=` test seam) — this task must NEVER call `submit_feedback` (that
would be auto-submitting feedback on the payer's behalf, i.e. fake reputation).
"""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.eip8004.models import FeedbackAuth
from modules.x402 import invoicing
from modules.x402.settlement_watcher import SettlementWatcher


async def _setup_db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


@pytest.fixture(autouse=True)
def _treasury_env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0xTREASURY")
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")


class _WakeAgent:
    def __init__(self):
        self.wakes = []

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.wakes.append((session_id, user_id, text, metadata))
        return True


class _Registry:
    def __init__(self, state="active"):
        self._state = state

    def resolve(self, *, surface, address, thread_id=None):
        return {"state": self._state} if self._state else None


class _Container:
    def __init__(self, reg):
        self._reg = reg

    def get_service(self, name):
        return self._reg if name == "correspondent_registry" else None


class _CorrAgent:
    def __init__(self, corr_state="active"):
        self.wakes = []
        self.correspondent_deliveries = []
        self.container = _Container(_Registry(corr_state))

    async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
        self.wakes.append((session_id, user_id, text, metadata))
        return True

    async def deliver_correspondent_data(self, session_id, source, text, metadata=None):
        self.correspondent_deliveries.append((session_id, source, text, metadata))
        return True


class _SpyReputationManager:
    """Records create_feedback_auth calls; submit_feedback must NEVER be
    called by this hook (that would be auto-submitted, fake feedback)."""

    def __init__(self):
        self.auth_calls = []
        self.submit_calls = []

    async def create_feedback_auth(self, client_address, task_id=None, expires_in_seconds=86400):
        self.auth_calls.append({"client_address": client_address, "task_id": task_id})
        return FeedbackAuth(
            agentId=42, clientAddress=client_address, expiresAt=9999999999,
            nonce="n", signature="0x" + "ab" * 65,
        )

    async def submit_feedback(self, *args, **kwargs):
        self.submit_calls.append((args, kwargs))
        raise AssertionError("submit_feedback must never be auto-called")


async def _settled_correspondent_invoice(db, *, tx_hash="0xfeed"):
    inv = await invoicing.create_payment_request(
        user_id="rob", session_id="sess_c", amount_usd=5.0, purpose="svc", db=db,
        correspondent_ref={"surface": "email", "address": "x@y.z", "thread_id": ""})
    await invoicing.settle_payment_request(inv["request_id"], transaction_hash=tx_hash, db=db)
    return inv


@pytest.mark.asyncio
async def test_flag_off_makes_no_eip8004_calls(tmp_path, monkeypatch):
    monkeypatch.delenv("EIP8004_ENABLED", raising=False)
    monkeypatch.delenv("EIP8004_PAYMENT_FEEDBACK", raising=False)
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    db = await _setup_db(tmp_path)
    try:
        inv = await _settled_correspondent_invoice(db)
        spy = _SpyReputationManager()
        agent = _CorrAgent()
        watcher = SettlementWatcher(agent, db=db, reputation_manager=spy)
        out = await watcher.tick_once()
        assert out["settled_notified"] == 1
        assert spy.auth_calls == []
        assert spy.submit_calls == []
        # settlement notice still delivered as before (byte-identical path)
        assert len(agent.correspondent_deliveries) == 1
        assert inv["request_id"] in agent.correspondent_deliveries[0][2]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_flag_on_correspondent_settled_creates_feedback_authorization(tmp_path, monkeypatch):
    monkeypatch.setenv("EIP8004_ENABLED", "true")
    monkeypatch.setenv("EIP8004_PAYMENT_FEEDBACK", "true")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    db = await _setup_db(tmp_path)
    try:
        inv = await _settled_correspondent_invoice(db, tx_hash="0xfeed123")
        spy = _SpyReputationManager()
        agent = _CorrAgent()
        watcher = SettlementWatcher(agent, db=db, reputation_manager=spy)
        out = await watcher.tick_once()
        assert out["settled_notified"] == 1

        # the authorization was created, referencing the identifiable payer —
        # NEVER a feedback submission on their behalf
        assert len(spy.auth_calls) == 1
        assert spy.auth_calls[0]["client_address"] == "x@y.z"
        assert spy.auth_calls[0]["task_id"] == inv["request_id"]
        assert spy.submit_calls == []

        # an offer was ALSO delivered to the payer as correspondent DATA,
        # distinct from (in addition to) the ordinary settlement notice
        kinds = [d[3].get("kind_hint") for d in agent.correspondent_deliveries]
        assert "payment_feedback_authorization" in kinds
        assert not agent.wakes  # never the owner "obey" rail
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_flag_on_but_eip8004_disabled_makes_no_calls(tmp_path, monkeypatch):
    """EIP8004_PAYMENT_FEEDBACK alone, without EIP8004_ENABLED, must be inert
    — it rides EIP8004_ENABLED."""
    monkeypatch.delenv("EIP8004_ENABLED", raising=False)
    monkeypatch.setenv("EIP8004_PAYMENT_FEEDBACK", "true")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    db = await _setup_db(tmp_path)
    try:
        await _settled_correspondent_invoice(db)
        spy = _SpyReputationManager()
        agent = _CorrAgent()
        watcher = SettlementWatcher(agent, db=db, reputation_manager=spy)
        await watcher.tick_once()
        assert spy.auth_calls == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_no_correspondent_ref_no_identifiable_payer_skips_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("EIP8004_ENABLED", "true")
    monkeypatch.setenv("EIP8004_PAYMENT_FEEDBACK", "true")
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_w", amount_usd=2.0, purpose="p", db=db)
        await invoicing.settle_payment_request(
            inv["request_id"], transaction_hash="0xabc", db=db)
        spy = _SpyReputationManager()
        agent = _WakeAgent()
        watcher = SettlementWatcher(agent, db=db, reputation_manager=spy)
        out = await watcher.tick_once()
        assert out["settled_notified"] == 1
        assert spy.auth_calls == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_settled_no_tx_skips_hook_no_verifiable_proof(tmp_path, monkeypatch):
    """A settlement with no on-chain transaction hash has nothing to prove —
    the hook must not fabricate a hollow ProofOfPayment."""
    monkeypatch.setenv("EIP8004_ENABLED", "true")
    monkeypatch.setenv("EIP8004_PAYMENT_FEEDBACK", "true")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    db = await _setup_db(tmp_path)
    try:
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="sess_c2", amount_usd=2.0, purpose="p", db=db,
            correspondent_ref={"surface": "email", "address": "x@y.z"})
        await invoicing.settle_payment_request(inv["request_id"], db=db)  # no tx hash
        spy = _SpyReputationManager()
        agent = _CorrAgent()
        watcher = SettlementWatcher(agent, db=db, reputation_manager=spy)
        out = await watcher.tick_once()
        assert out["settled_notified"] == 1
        assert spy.auth_calls == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_hook_error_never_blocks_settlement_wake(tmp_path, monkeypatch):
    """Fail-open: an 8004 error must NEVER block the settlement wake/notify."""
    monkeypatch.setenv("EIP8004_ENABLED", "true")
    monkeypatch.setenv("EIP8004_PAYMENT_FEEDBACK", "true")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    db = await _setup_db(tmp_path)
    try:
        await _settled_correspondent_invoice(db)

        class _BoomManager:
            async def create_feedback_auth(self, *a, **kw):
                raise RuntimeError("boom — e.g. no EIP8004_AGENT_PRIVATE_KEY configured")

        agent = _CorrAgent()
        watcher = SettlementWatcher(agent, db=db, reputation_manager=_BoomManager())
        out = await watcher.tick_once()
        assert out["settled_notified"] == 1
        assert len(agent.correspondent_deliveries) == 1  # settlement notice unaffected
    finally:
        await db.close()
