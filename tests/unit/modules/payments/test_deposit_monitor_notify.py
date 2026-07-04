"""C8: DepositMonitor must (a) use the live price oracle instead of the
hardcoded 3000.0 placeholder, and (b) notify the user when a deposit is
credited (persist a row + best-effort invoke an injected callback).

Also covers the money-safety review fixes on top of C8:
  - ETH dedup must key off a price-independent on-chain amount (wei), never
    amount_usd, or a live oracle re-credits the same un-swept balance every
    tick (CRITICAL double-credit bug).
  - A failed add_credits must leave the deposit re-processable, not
    permanently marked done (credit-then-record ordering).
  - The price oracle must reject an implausibly high price rather than
    credit a wild amount.

And the follow-up re-review (residual MEDIUM/LOW after the CRITICAL fix):
  - Crediting and recording the crypto_payments dedup row must be atomic —
    a crash / failed INSERT between them must not leave a committed credit
    with no dedup row (which would let the next tick re-credit).
  - The dedup key ('amount') must be a hard requirement, never a silent
    price-derived fallback.
  - The wild-oracle-price test must prove no credit is granted end-to-end,
    not just that `_get_eth_price` raises in isolation.
"""
import copy
import sys
import types

import pytest

from modules.payments.deposit_monitor import DepositMonitor
from modules.credits.balance_manager import CreditBalanceManager


def _fake_web3_module(eth_balance_wei: int):
    """Build a fake `web3` module so `_check_chain_deposits` — the actual
    code that constructs the ETH deposit dict — can be exercised without a
    real RPC endpoint. Mirrors the subset of the web3.py interface the
    module uses: `Web3(Web3.HTTPProvider(url))`, `w3.eth.get_balance(addr)`,
    `w3.eth.contract(address=..., abi=...).functions.balanceOf(addr).call()`.
    """

    class _FakeBalanceOfCall:
        def call(self):
            return 0  # no stablecoin balance; keep this test ETH-only

    class _FakeFunctions:
        def balanceOf(self, address):
            return _FakeBalanceOfCall()

    class _FakeContract:
        def __init__(self, address, abi):
            self.functions = _FakeFunctions()

    class _FakeEth:
        def get_balance(self, address):
            return eth_balance_wei

        def contract(self, address, abi):
            return _FakeContract(address, abi)

    class _FakeHTTPProvider:
        def __init__(self, url):
            self.url = url

    class _FakeWeb3:
        HTTPProvider = _FakeHTTPProvider

        def __init__(self, provider):
            self.eth = _FakeEth()

    return types.SimpleNamespace(Web3=_FakeWeb3)


class _FakeConnection:
    """Minimal stand-in for `DatabaseConnection`'s transaction control API
    (`begin_transaction`/`commit`/`rollback`) so `_process_deposit`'s
    localized transaction wrapper has something to call. Tracks call counts
    for assertions; the legacy `_FakeDB` below doesn't itself participate in
    transactional rollback (its writes are applied immediately, matching the
    old behaviour those tests were written against) — the real atomicity
    guarantee is covered separately by `_TxFakeDB` below, which layers real
    commit/rollback semantics on top of this same interface.
    """

    def __init__(self):
        self.in_transaction = False
        self.began = 0
        self.committed = 0
        self.rolled_back = 0

    async def begin_transaction(self):
        self.in_transaction = True
        self.began += 1

    async def commit(self):
        self.in_transaction = False
        self.committed += 1

    async def rollback(self):
        self.in_transaction = False
        self.rolled_back += 1


class _FakeDB:
    """Simulates enough of a real DB for the dedup guard to be exercised.

    `execute` records every INSERT into `crypto_payments` in-memory so a
    subsequent `fetch_one` dedup SELECT can actually match it — the original
    fake unconditionally returned None, so the dedup path was never tested.
    """

    def __init__(self):
        self.executed = []
        self._crypto_payments = []  # list of dicts: user_id/chain/amount/token_symbol
        self.connection = _FakeConnection()

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "INSERT INTO crypto_payments" in sql and params:
            user_id, chain, _address, token_symbol, amount, _amount_usd, _credits = params
            self._crypto_payments.append({
                "id": len(self._crypto_payments) + 1,
                "user_id": user_id,
                "chain": chain,
                "amount": amount,
                "token_symbol": token_symbol,
            })

    async def fetch_one(self, sql, params=None):
        if "FROM crypto_payments" in sql and params:
            user_id, chain, amount, token_symbol = params
            for row in self._crypto_payments:
                if (
                    row["user_id"] == user_id
                    and row["chain"] == chain
                    and row["amount"] == amount
                    and row["token_symbol"] == token_symbol
                ):
                    return {"id": row["id"]}
        return None  # "not already processed"


class _FakeBalanceManager:
    def __init__(self, fail=False):
        self.add_calls = []
        self._fail = fail

    async def add_credits(self, user_id, amount, reason):
        if self._fail:
            raise RuntimeError("simulated ledger failure")
        self.add_calls.append((user_id, amount, reason))
        return True


class _TxFakeDB:
    """A faithful-enough fake of single-connection SQLite transaction
    semantics (same as `modules.database.connection.DatabaseConnection`):
    writes are visible to reads on the SAME connection immediately, whether
    or not a transaction is open, and `rollback()` restores state to
    whatever it was at the matching `begin_transaction()`. Wired up to the
    REAL `CreditBalanceManager` (not `_FakeBalanceManager`, which has no
    persisted state to roll back) so the atomicity test below exercises the
    actual `add_credits` writes, not a mock that can't fail partway.
    """

    def __init__(self):
        self.user_credits = {}       # user_id -> {balance, lifetime_earned, lifetime_spent}
        self.credit_transactions = []
        self.crypto_payments = []
        self.user_notifications = []
        self.connection = self       # .connection.begin_transaction() etc.
        self._snapshot = None
        self.fail_crypto_payments_insert = False

    # -- transaction control (mirrors DatabaseConnection) --
    async def in_transaction(self):
        return self._snapshot is not None

    async def begin_transaction(self):
        self._snapshot = (
            copy.deepcopy(self.user_credits),
            copy.deepcopy(self.credit_transactions),
            copy.deepcopy(self.crypto_payments),
        )

    async def commit(self):
        self._snapshot = None

    async def rollback(self):
        if self._snapshot is not None:
            self.user_credits, self.credit_transactions, self.crypto_payments = self._snapshot
        self._snapshot = None

    # -- query surface used by CreditBalanceManager + DepositMonitor --
    async def fetch_one(self, sql, params=None):
        params = params or ()
        if "FROM user_credits" in sql:
            row = self.user_credits.get(params[0])
            return dict(row) if row else None
        if "FROM crypto_payments" in sql:
            user_id, chain, amount, token_symbol = params
            for row in self.crypto_payments:
                if (
                    row["user_id"] == user_id
                    and row["chain"] == chain
                    and row["amount"] == amount
                    and row["token_symbol"] == token_symbol
                ):
                    return {"id": row["id"]}
        return None

    async def execute(self, sql, params=None):
        params = params or ()
        if "CREATE TABLE" in sql:
            return
        if "INSERT INTO user_credits" in sql:
            user_id = params[0]
            self.user_credits.setdefault(
                user_id, {"balance": 0, "lifetime_earned": 0, "lifetime_spent": 0}
            )
            return
        if "UPDATE user_credits" in sql:
            new_balance, amount_delta, user_id = params
            row = self.user_credits.setdefault(
                user_id, {"balance": 0, "lifetime_earned": 0, "lifetime_spent": 0}
            )
            row["balance"] = new_balance
            row["lifetime_earned"] += amount_delta
            return
        if "INSERT INTO credit_transactions" in sql:
            self.credit_transactions.append(params)
            return
        if "INSERT INTO crypto_payments" in sql:
            if self.fail_crypto_payments_insert:
                raise RuntimeError("simulated INSERT failure (crash/disk error)")
            user_id, chain, _address, token_symbol, amount, _amount_usd, _credits = params
            self.crypto_payments.append({
                "id": len(self.crypto_payments) + 1,
                "user_id": user_id,
                "chain": chain,
                "amount": amount,
                "token_symbol": token_symbol,
            })
            return
        if "INSERT INTO user_notifications" in sql:
            self.user_notifications.append(params)
            return


class _Config:
    deposit_check_interval = 60


@pytest.mark.asyncio
async def test_get_eth_price_uses_live_oracle_not_hardcoded_3000(monkeypatch):
    monkeypatch.setenv("ETH_PRICE_USD_OVERRIDE", "4200.0")
    monitor = DepositMonitor(_FakeDB(), _FakeBalanceManager(), _Config())
    price = await monitor._get_eth_price()
    assert price == 4200.0  # not the old hardcoded 3000.0


@pytest.mark.asyncio
async def test_process_deposit_notifies_via_callback_and_persists_row():
    db = _FakeDB()
    balance_mgr = _FakeBalanceManager()
    notify_calls = []

    async def _notify(user_id, message):
        notify_calls.append((user_id, message))

    monitor = DepositMonitor(db, balance_mgr, _Config(), notify_callback=_notify)

    await monitor._process_deposit("user_1", {
        "chain": "sepolia",
        "token_symbol": "USDC",
        "amount": "10.0",
        "amount_usd": 10.0,
    })

    # Credited.
    assert len(balance_mgr.add_calls) == 1
    assert balance_mgr.add_calls[0][0] == "user_1"
    assert balance_mgr.add_calls[0][1] == 1000  # $10.00 / $0.01 per credit

    # Callback invoked.
    assert len(notify_calls) == 1
    assert notify_calls[0][0] == "user_1"
    assert "10.00" in notify_calls[0][1] or "10.0" in notify_calls[0][1]

    # DB row persisted for the notification (durable, pollable later).
    insert_sqls = [sql for sql, _ in db.executed if "user_notifications" in sql and "INSERT" in sql]
    assert insert_sqls, f"expected a user_notifications INSERT, got: {db.executed}"


@pytest.mark.asyncio
async def test_process_deposit_without_callback_still_persists_row():
    db = _FakeDB()
    balance_mgr = _FakeBalanceManager()
    monitor = DepositMonitor(db, balance_mgr, _Config())  # no notify_callback

    await monitor._process_deposit("user_2", {
        "chain": "sepolia", "token_symbol": "USDC", "amount": "5.0", "amount_usd": 5.0,
    })

    assert len(balance_mgr.add_calls) == 1
    insert_sqls = [sql for sql, _ in db.executed if "user_notifications" in sql and "INSERT" in sql]
    assert insert_sqls


@pytest.mark.asyncio
async def test_eth_deposit_dedups_on_price_independent_amount_not_usd(monkeypatch):
    """CRITICAL regression (C8 money-safety review): with a live oracle,
    amount_usd = eth_price * balance changes almost every tick. The dedup
    guard must key off a stable on-chain amount (wei), never amount_usd, or
    the same un-swept ETH balance re-credits on every monitor tick.
    """
    db = _FakeDB()
    balance_mgr = _FakeBalanceManager()
    monitor = DepositMonitor(db, balance_mgr, _Config())

    eth_balance_wei = 2_000_000_000_000_000_000  # 2 ETH, un-swept the whole time

    # Tick 1: oracle price = 3000.
    monkeypatch.setenv("ETH_PRICE_USD_OVERRIDE", "3000.0")
    price1 = await monitor._get_eth_price()
    await monitor._process_deposit("user_eth", {
        "chain": "ethereum",
        "token_symbol": "ETH",
        "amount": str(eth_balance_wei),
        "amount_usd": price1 * (eth_balance_wei / 10**18),
    })
    assert len(balance_mgr.add_calls) == 1

    # Tick 2: SAME un-swept balance, but the live oracle price moved ->
    # amount_usd is different from tick 1, yet the on-chain amount (wei)
    # did not change.
    monkeypatch.setenv("ETH_PRICE_USD_OVERRIDE", "3123.45")
    price2 = await monitor._get_eth_price()
    assert price2 != price1
    await monitor._process_deposit("user_eth", {
        "chain": "ethereum",
        "token_symbol": "ETH",
        "amount": str(eth_balance_wei),  # unchanged -> dedup key unchanged
        "amount_usd": price2 * (eth_balance_wei / 10**18),
    })

    # Must NOT credit again — same on-chain funds, exactly one credit.
    assert len(balance_mgr.add_calls) == 1, (
        "same un-swept ETH balance was re-credited after the oracle price changed"
    )


@pytest.mark.asyncio
async def test_check_chain_deposits_eth_amount_field_is_price_independent(monkeypatch):
    """Root-cause regression: `_check_chain_deposits` is what actually
    constructs the ETH deposit dict fed into `_process_deposit`'s dedup
    guard. Before the fix it had NO 'amount' key at all, so the guard fell
    back to `str(amount_usd)` — which is `eth_price * balance` and moves
    every tick under the live oracle. Assert the dict now carries a stable
    price-independent 'amount' (wei) across two calls with different
    oracle prices but the SAME un-swept on-chain balance.
    """
    eth_balance_wei = 2_000_000_000_000_000_000  # 2 ETH, unchanged between calls
    monkeypatch.setitem(sys.modules, "web3", _fake_web3_module(eth_balance_wei))

    monitor = DepositMonitor(_FakeDB(), _FakeBalanceManager(), _Config())
    chain_config = {"rpc_url": "http://fake-rpc.invalid", "chain_id": 1}

    monkeypatch.setenv("ETH_PRICE_USD_OVERRIDE", "3000.0")
    deposits1 = await monitor._check_chain_deposits("0xabc", "ethereum", chain_config)

    monkeypatch.setenv("ETH_PRICE_USD_OVERRIDE", "3123.45")
    deposits2 = await monitor._check_chain_deposits("0xabc", "ethereum", chain_config)

    eth1 = next(d for d in deposits1 if d["token_symbol"] == "ETH")
    eth2 = next(d for d in deposits2 if d["token_symbol"] == "ETH")

    assert "amount" in eth1 and "amount" in eth2, "ETH deposit dict is missing a stable dedup key"
    assert eth1["amount"] == eth2["amount"] == str(eth_balance_wei)
    assert eth1["amount_usd"] != eth2["amount_usd"], "test setup sanity: price did move between calls"


@pytest.mark.asyncio
async def test_wildly_high_oracle_price_is_rejected_no_credit(monkeypatch):
    """MEDIUM (C8 money-safety review): a schema hiccup or an
    ETH_PRICE_USD_OVERRIDE typo (extra digit) must not translate into a
    wildly-inflated credit grant. Drive the real detection path
    (`_check_chain_deposits`, which is what `_check_all_deposits` actually
    calls) end-to-end with a real on-chain ETH balance present, and assert
    NO deposit reaches `_process_deposit` / `add_credits` — not just that
    `_get_eth_price()` happens to raise in isolation.
    """
    eth_balance_wei = 2_000_000_000_000_000_000  # 2 ETH sitting on-chain
    monkeypatch.setitem(sys.modules, "web3", _fake_web3_module(eth_balance_wei))
    monkeypatch.setenv("ETH_PRICE_USD_OVERRIDE", "5000000")  # implausible, likely a typo

    db = _FakeDB()
    balance_mgr = _FakeBalanceManager()
    monitor = DepositMonitor(db, balance_mgr, _Config())
    chain_config = {"rpc_url": "http://fake-rpc.invalid", "chain_id": 1}

    deposits = await monitor._check_chain_deposits("0xabc", "ethereum", chain_config)

    # The wild-price ETH deposit must never be queued for processing at all.
    assert not any(d["token_symbol"] == "ETH" for d in deposits), (
        "a wildly-high oracle price still produced an ETH deposit entry"
    )

    for deposit in deposits:
        await monitor._process_deposit("user_wild_price", deposit)

    assert balance_mgr.add_calls == [], (
        "a wildly-high oracle price resulted in a credit being granted"
    )


@pytest.mark.asyncio
async def test_failed_add_credits_leaves_deposit_reprocessable():
    """A failed add_credits must never permanently mark the deposit done.
    Credit-then-record ordering: if the credit call raises, no
    crypto_payments row is written, so the SAME deposit is retried (and can
    succeed) on the next monitor tick instead of being silently lost.
    """
    db = _FakeDB()
    failing_balance_mgr = _FakeBalanceManager(fail=True)
    monitor = DepositMonitor(db, failing_balance_mgr, _Config())

    deposit = {
        "chain": "sepolia", "token_symbol": "USDC", "amount": "20.0", "amount_usd": 20.0,
    }

    # Tick 1: add_credits fails.
    await monitor._process_deposit("user_3", deposit)
    assert db._crypto_payments == [], "a row was recorded despite the credit failing"

    # Tick 2: same deposit, but this time the ledger call succeeds -> it
    # must NOT be treated as already-processed, and must actually credit.
    working_balance_mgr = _FakeBalanceManager()
    monitor2 = DepositMonitor(db, working_balance_mgr, _Config())
    await monitor2._process_deposit("user_3", deposit)

    assert len(working_balance_mgr.add_calls) == 1, "deposit was not re-processable after the earlier failure"


@pytest.mark.asyncio
async def test_credit_and_dedup_record_are_atomic_across_a_crash():
    """MEDIUM (C8 residual re-review): crediting and recording the
    crypto_payments dedup row must be ONE atomic transaction. Before this
    fix, `add_credits`'s writes and the `crypto_payments` INSERT were two
    separate autocommitted operations — if the process crashed (or the
    INSERT itself failed) between them, the credit was already durable on
    disk but no dedup row existed, so the NEXT tick would find nothing and
    re-credit the same on-chain deposit a second time.

    Simulates exactly that: `add_credits` succeeds (its writes land) but the
    `crypto_payments` INSERT then fails on tick 1. Tick 2 must NOT
    double-credit — the transaction wrapper must have rolled the tick-1
    credit back along with the failed INSERT, so tick 2 is a clean,
    single, fresh credit.
    """
    db = _TxFakeDB()
    balance_mgr = CreditBalanceManager(db=db)
    monitor = DepositMonitor(db, balance_mgr, _Config())

    deposit = {
        "chain": "sepolia", "token_symbol": "USDC", "amount": "20.0", "amount_usd": 20.0,
    }

    # Tick 1: add_credits' writes succeed, but the crypto_payments INSERT
    # that follows fails (simulated crash / disk error mid-transaction).
    db.fail_crypto_payments_insert = True
    await monitor._process_deposit("user_tx", deposit)

    assert db.crypto_payments == [], "dedup row was recorded despite the simulated failure"
    balance_after_tick1 = db.user_credits.get("user_tx", {}).get("balance", 0)
    assert balance_after_tick1 == 0, (
        "add_credits' writes survived the crypto_payments INSERT failure "
        "instead of being rolled back with it -- a retry will double-credit "
        f"(balance after tick 1 = {balance_after_tick1}, expected 0)"
    )

    # Tick 2: same deposit, nothing fails this time.
    db.fail_crypto_payments_insert = False
    await monitor._process_deposit("user_tx", deposit)

    assert len(db.crypto_payments) == 1, "deposit should be recorded exactly once after tick 2"
    final_balance = db.user_credits["user_tx"]["balance"]
    assert final_balance == 2000, (
        f"expected exactly one deposit's worth of credits ($20.00 / $0.01 = 2000), "
        f"got {final_balance}"
    )


@pytest.mark.asyncio
async def test_process_deposit_requires_amount_no_price_derived_fallback():
    """LOW (C8 residual re-review): the dedup key must be a hard
    requirement, never `deposit.get('amount', str(deposit['amount_usd']))`
    -- that silent fallback is exactly what reintroduced the CRITICAL
    price-derived-dedup-key bug in the first place. A deposit dict missing
    'amount' must be rejected outright rather than silently falling back to
    a price-derived dedup key and processing anyway.

    `_process_deposit` is fail-open at its outer boundary (one bad deposit
    must not kill the whole monitor loop), so the missing-key error is
    caught and logged rather than raised to the caller -- the observable
    contract is that nothing gets credited or recorded. That's also the
    distinguishing signal from the old buggy behaviour: with the removed
    `.get(..., fallback)` default this deposit WOULD have been credited;
    with the fix it must NOT be.
    """
    db = _FakeDB()
    balance_mgr = _FakeBalanceManager()
    monitor = DepositMonitor(db, balance_mgr, _Config())

    deposit_missing_amount = {
        "chain": "sepolia", "token_symbol": "USDC", "amount_usd": 10.0,
    }

    await monitor._process_deposit("user_no_amount", deposit_missing_amount)

    assert balance_mgr.add_calls == [], (
        "deposit missing 'amount' was still credited -- the price-derived "
        "fallback is still present"
    )
    assert db._crypto_payments == [], (
        "deposit missing 'amount' still produced a crypto_payments row"
    )
