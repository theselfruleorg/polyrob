"""124 W1/W2 — the ERC-6551 rules for every collection, read from the chain (core.wallet.nft_rules).

The generic call to a known NFT sale/market selector goes to the typed verb (5.4); an address
that is an agent account on another chain is refused (C1); the account of an unminted id of an
UNPINNED collection is refused (F1); an unreadable pin store removes no rule (5.5).
"""
from core.wallet import erc6551, nft_rules, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

HOLDER = "0x1111111111111111111111111111111111111111"
NFT = "0x4444444444444444444444444444444444444444"
SALE = "0x5151515151515151515151515151515151515151"
ROUTER = "0x6262626262626262626262626262626262626262"
DEST = "0x9999999999999999999999999999999999999999"
WORD_ONE = "0x" + "0" * 63 + "1"


class Rpc:
    """``supportsInterface`` (``erc721``), ``owed`` (``sales``), ``ownerOf`` (``owners``; an
    absent id is unminted), and no ``accountImplementation``."""

    def __init__(self, *, erc721=(), sales=(), owners=None, broken=False):
        self.erc721 = {a.lower() for a in erc721}
        self.sales = {a.lower() for a in sales}
        self.owners = {(c.lower(), i): o for (c, i), o in (owners or {}).items()}
        self.broken = broken

    def __call__(self, method, params):
        if self.broken:
            raise RuntimeError("rpc down")
        if method == "eth_getCode":
            return "0x"
        if method == "eth_blockNumber":
            return hex(0x100)
        if method == "eth_getLogs":
            return []
        assert method == "eth_call", method
        to, data = params[0]["to"].lower(), params[0]["data"]
        if data.startswith("0x01ffc9a7"):
            return WORD_ONE if to in self.erc721 else "0x" + "0" * 64
        if data.startswith("0xdf18e047"):
            if to in self.sales:
                return "0x" + "0" * 64
            raise RuntimeError("execution reverted")
        if data.startswith("0x6352211e"):
            owner = self.owners.get((to, int(data[10:], 16)))
            if owner is None:
                raise RuntimeError("execution reverted: ERC721NonexistentToken")
            return "0x" + "0" * 24 + owner[2:].lower()
        return "0x"


def _auth(intent, tx, rpc, deltas=None):
    deltas = deltas or Deltas(ok=True, native_delta=-int(tx.get("value") or 0), gas_used=50_000)
    tx = dict({"value": 0, "chainId": 4663, "nonce": 1, "gas": 200_000, "maxFeePerGas": 10 ** 8}, **tx)
    return tx_guard.authorize(
        intent, tx, holder=HOLDER, gate=PolicyGate(max_per_tx_usd=500.0, daily_cap_usd=1000.0),
        execution_context=None, simulate_fn=lambda **_: deltas, price_fn=lambda c, a: 3000.0,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda c, t: False, account_rpc=rpc)


def _call(to, data, value=0):
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=to, amount_raw=value,
                               max_spend_usd=500.0, idempotency_key="g")
    return intent, {"to": to, "data": data, "value": value}


# --- 5.4 the generic call goes to the typed verb ---------------------------------------------

def test_a_generic_mint_on_a_sale_names_the_typed_verb():
    data = "0x156e29f6" + "0" * 24 + HOLDER[2:] + f"{1:064x}" + f"{10 ** 16:064x}"
    d = _auth(*_call(SALE, data, 10 ** 15), Rpc(sales=[SALE]))
    assert d.allowed is False and "agent_nft_collection_mint" in d.reason, d.reason


def test_a_generic_reveal_on_a_collection_names_the_typed_verb():
    why = nft_rules.typed_verb_refusal(Rpc(erc721=[NFT]), NFT, "0xb93f208a" + "0" * 128)
    assert "agent_nft_collection_reveal" in why


def test_a_market_buy_is_known_by_its_shape_alone():
    assert "agent_nft_buy" in nft_rules.typed_verb_refusal(Rpc(), ROUTER, "0xe9c3b815" + "00" * 64)


def test_the_same_selector_on_an_ordinary_contract_passes():
    """mint(address,uint256,uint256) is also an ERC-1155 shape: only an NFT sale is routed."""
    data = "0x156e29f6" + "0" * 24 + HOLDER[2:] + f"{1:064x}" + f"{1:064x}"
    assert nft_rules.typed_verb_refusal(Rpc(), ROUTER, data) is None


def test_an_unreadable_target_refuses_a_known_selector_only():
    assert "failing closed" in nft_rules.typed_verb_refusal(Rpc(broken=True), SALE, "0x1e83409a" + "00" * 32)
    assert nft_rules.typed_verb_refusal(Rpc(broken=True), SALE, "0xa9059cbb" + "00" * 64) is None


# --- C1 the account address on another chain ----------------------------------------------------

def test_a_send_to_an_agent_account_of_another_chain_refuses(monkeypatch):
    acct = erc6551.account_address(8453, NFT, 7, implementation=erc6551.ACCOUNT_V3_IMPL).lower()
    held = [nft_rules.HeldAccount(chain="base", collection=NFT.lower(), token_id=7, account=acct,
                                  label="X")]
    monkeypatch.setattr(nft_rules, "held_accounts", lambda: held)
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=acct, amount_raw=10 ** 15,
                               max_spend_usd=500.0, idempotency_key="c1")
    d = _auth(intent, {"to": acct, "data": "0x", "value": 10 ** 15}, Rpc())
    assert d.allowed is False and "nobody controls that address" in d.reason, d.reason
    assert nft_rules.cross_chain_refusal("base", [acct], held) is None   # its own chain


def test_an_unreadable_holdings_state_blocks_no_send(monkeypatch):
    monkeypatch.setattr(nft_rules, "held_accounts", lambda: None)
    assert nft_rules.cross_chain_refusal("robinhood", [DEST]) is None


# --- F1 the account of an unminted id, any collection --------------------------------------------

def test_an_nft_into_the_future_account_of_an_unpinned_collection_refuses():
    """NFT #42 goes to the account of NFT #43 (not minted): whoever mints #43 owns it."""
    future = erc6551.account_address(4663, NFT, 43, implementation=erc6551.ACCOUNT_V3_IMPL)
    data = "0x23b872dd" + "0" * 24 + HOLDER[2:] + "0" * 24 + future[2:].lower() + f"{43:064x}"
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=NFT, amount_raw=0,
                               max_spend_usd=5.0, idempotency_key="f1", is_nft_op=True,
                               nft_out=((NFT, "erc721", 42, 1),))
    deltas = Deltas(ok=True, native_delta=0, gas_used=90_000,
                    holder_nft_out=((NFT.lower(), "erc721", future.lower(), 42, 1),))
    d = _auth(intent, {"to": NFT, "data": data}, Rpc(owners={(NFT, 42): HOLDER}), deltas)
    assert d.allowed is False and "deployed or not" in d.reason, d.reason   # N5 answers first
    # the F1 rule alone: ETH to that account while an NFT of the collection arrives
    eth = tx_guard.TxIntent(chain="robinhood", token=None, to=future, amount_raw=1,
                            max_spend_usd=5.0, idempotency_key="f1b")
    arrives = Deltas(ok=True, native_delta=-1, gas_used=1,
                     holder_nft_in=((NFT.lower(), "erc721", HOLDER.lower(), 43, 1),))
    why = nft_rules.future_account_refusal(Rpc(), 4663, eth, {"to": future, "data": "0x"},
                                           arrives, HOLDER)
    assert why and "not minted yet" in why, why


def test_the_ids_in_the_transaction_are_read_from_its_calldata():
    assert {1, 43} <= nft_rules.calldata_ids({"data": "0x23b872dd" + f"{1:064x}" + f"{43:064x}"})
    assert nft_rules.calldata_addresses({"data": "0xa9059cbb" + "0" * 24 + DEST[2:] + f"{5:064x}"}) \
        == {DEST.lower()}


# --- R1 a deposit into a held account that may keep an earlier owner's grant ---------------------

def _v3_clone(contract, token_id):
    footer = (0).to_bytes(32, "big") + (4663).to_bytes(32, "big") + b"\x00" * 12 \
        + bytes.fromhex(contract[2:]) + token_id.to_bytes(32, "big")
    return ("0x363d3d373d3d3d363d73" + erc6551.ACCOUNT_V3_IMPL[2:].lower()
            + "5af43d82803e903d91602b57fd5bf3" + footer.hex())


def _deposit(monkeypatch, logs):
    acct = erc6551.account_address(4663, NFT, 7, implementation=erc6551.ACCOUNT_V3_IMPL).lower()
    held = [nft_rules.HeldAccount(chain="robinhood", collection=NFT.lower(), token_id=7,
                                  account=acct, label="X")]

    def rpc(method, params):
        if method == "eth_getCode":
            return _v3_clone(NFT, 7) if params[0].lower() == acct else "0x"
        if method == "eth_blockNumber":
            return hex(0x100)
        if method == "eth_getBalance":
            return "0x0"
        if method == "eth_getLogs":
            return list(logs(acct))
        if method == "eth_call":
            return "0x" + f"{7:064x}"            # the allowance read confirms the row
        raise AssertionError(method)
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=acct, amount_raw=10 ** 15,
                               max_spend_usd=5.0, idempotency_key="r1")
    return nft_rules.deposit_refusal(rpc, "robinhood", 4663, intent,
                                     {"to": acct, "value": 10 ** 15, "data": "0x"},
                                     Deltas(ok=True), HOLDER, held)


def test_a_deposit_into_a_v3_account_with_an_open_grant_refuses(monkeypatch):
    def logs(acct):
        return [{"address": ROUTER, "blockNumber": hex(0x50), "logIndex": "0x0",
                 "topics": [erc6551.TOPIC_APPROVAL, "0x" + "0" * 24 + acct[2:],
                            "0x" + "0" * 24 + DEST[2:].lower()],
                 "data": "0x" + f"{7:064x}"}]
    why = _deposit(monkeypatch, logs)
    assert why and "agent_nft_revoke_all" in why and "earlier owner" in why, why


def test_a_deposit_into_a_clean_v3_account_passes(monkeypatch):
    assert _deposit(monkeypatch, lambda acct: []) is None


def test_the_arrival_notice_names_the_kind():
    from core.wallet.nft_holdings import arrived_text
    row = {"label": "X #7", "chain": "robinhood", "account": "0xabc", "token_id": 7}
    assert "scan them before any deposit" in arrived_text(dict(row, kind="tokenbound_v3"))
    assert "R-1" in arrived_text(dict(row, kind="erc6551p"))


# --- M1 an internal move ---------------------------------------------------------------------------

def test_an_account_paying_its_owner_is_internal():
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=HOLDER, amount_raw=1,
                               max_spend_usd=5.0, idempotency_key="m1", via_account="0x" + "ac" * 20)
    assert nft_rules.internal_move("robinhood", intent, {}, Deltas(ok=True), "0x" + "ac" * 20,
                                   sender=HOLDER) is True


def _pinned_held(monkeypatch, acct, owner=HOLDER):
    from tests.collection_pins import pin, profile
    pin(monkeypatch, profile(NFT))
    held = [nft_rules.HeldAccount(chain="robinhood", collection=NFT.lower(), token_id=3,
                                  account=acct, label="X")]
    return held, Rpc(owners={(NFT, 3): owner})


def test_a_deposit_into_a_pinned_held_account_is_internal(monkeypatch):
    acct = erc6551.account_address(4663, NFT, 3, implementation=erc6551.ACCOUNT_V3_IMPL).lower()
    held, rpc = _pinned_held(monkeypatch, acct)
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=acct, amount_raw=1,
                               max_spend_usd=5.0, idempotency_key="m1")
    tx = {"to": acct, "data": "0x", "value": 1}
    assert nft_rules.internal_move("robinhood", intent, tx, Deltas(ok=True), HOLDER, held=held,
                                   rpc=rpc) is True
    assert nft_rules.internal_move("robinhood", intent, {"to": DEST, "data": "0x", "value": 1},
                                   Deltas(ok=True), HOLDER, held=held, rpc=rpc) is False


def test_an_unpinned_or_lost_held_account_is_charged_in_full(monkeypatch):
    """The 2026-11-20 review: a collection nobody pinned can lie in ownerOf — never internal;
    a pinned NFT the chain no longer shows as the treasury's — never internal."""
    acct = erc6551.account_address(4663, NFT, 3, implementation=erc6551.ACCOUNT_V3_IMPL).lower()
    held, rpc = _pinned_held(monkeypatch, acct, owner=DEST)
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=acct, amount_raw=1,
                               max_spend_usd=5.0, idempotency_key="m1")
    tx = {"to": acct, "data": "0x", "value": 1}
    assert nft_rules.internal_move("robinhood", intent, tx, Deltas(ok=True), HOLDER, held=held,
                                   rpc=rpc) is False
    from tests.collection_pins import pin
    pin(monkeypatch)
    assert nft_rules.internal_move("robinhood", intent, tx, Deltas(ok=True), HOLDER, held=held,
                                   rpc=Rpc(owners={(NFT, 3): HOLDER})) is False


def test_an_internal_move_is_charged_its_fee_only(monkeypatch):
    acct = erc6551.account_address(4663, NFT, 3, implementation=erc6551.ACCOUNT_V3_IMPL).lower()
    held, rpc = _pinned_held(monkeypatch, acct)
    monkeypatch.setattr(nft_rules, "held_accounts", lambda: held)
    monkeypatch.setattr(nft_rules, "deposit_refusal", lambda *a, **k: None)
    value = 10 ** 18   # 1 ETH at $3000
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=acct, amount_raw=value,
                               max_spend_usd=5000.0, idempotency_key="m1")
    d = _auth(intent, {"to": acct, "data": "0x", "value": value}, rpc,
              Deltas(ok=True, native_delta=-value, gas_used=21_000))
    assert d.allowed is True, d.reason
    assert d.amount_usd < 1.0 and "internal move" in d.reason


def test_an_unreadable_holdings_record_puts_a_value_move_on_the_card(monkeypatch):
    monkeypatch.setattr(nft_rules, "held_accounts", lambda: None)
    intent = tx_guard.TxIntent(chain="robinhood", token=None, to=DEST, amount_raw=10 ** 15,
                               max_spend_usd=500.0, idempotency_key="u1")
    d = _auth(intent, {"to": DEST, "data": "0x", "value": 10 ** 15}, Rpc(),
              Deltas(ok=True, native_delta=-10 ** 15, gas_used=21_000))
    assert d.allowed is False and d.lane == "owner_queue" and "HOLDINGS UNREADABLE" in d.reason


def test_a_clone_of_an_unowned_token_is_no_destination():
    """The review: an account whose token nobody owns (not minted, burned) is refused."""
    acct = "0x" + "c1" * 20
    clone = ("0x363d3d373d3d3d363d73" + erc6551.ACCOUNT_V3_IMPL[2:].lower()
             + "5af43d82803e903d91602b57fd5bf3" + ((0).to_bytes(32, "big") + (4663).to_bytes(32, "big")
             + b"\x00" * 12 + bytes.fromhex(ROUTER[2:]) + (5).to_bytes(32, "big")).hex())

    class R(Rpc):
        def __call__(self, method, params):
            if method == "eth_getCode":
                return clone if params[0].lower() == acct else "0x"
            return super().__call__(method, params)
    got = nft_rules.nesting(R(), "robinhood", 4663, {"data": "0x"},
                            [(NFT, "erc721", acct, 42, 1)], HOLDER)
    assert got.refusal and "has no owner" in got.refusal


# --- S3 a signature that speaks for an account ------------------------------------------------------

def test_a_v3_account_is_named_and_an_erc6551p_account_is_not():
    held = [nft_rules.HeldAccount(chain="base", collection=NFT.lower(), token_id=1,
                                  account="0x" + "a1" * 20, label="V3 #1"),
            nft_rules.HeldAccount(chain="base", collection=NFT.lower(), token_id=2,
                                  account="0x" + "a2" * 20, label="P #2")]
    lines = nft_rules.signature_speaks_for(held, kinds={"0x" + "a2" * 20: "erc6551p"})
    assert len(lines) == 1 and "0x" + "a1" * 20 in lines[0] and "ERC-1271" in lines[0]
