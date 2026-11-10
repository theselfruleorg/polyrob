"""109 W5: the run names the function (no hand encoding); a tx proof is read on chain."""
import pytest

from core.commerce import orders as ords
from core.commerce import store as st
from modules.commerce import service
from tests.unit.core.commerce.test_offer_store_orders import MANIFEST
from tools.defi.call_verb import encode_call

C = "0x" + "c0" * 20
TX = "0x" + "aa" * 32
ME = "0x" + "e0" * 20
BUYER = "0x" + "ab" * 20
MINT_LOG = {"address": C, "topics": ["0x" + "dd" * 32, "0x" + "0" * 64,
                                     "0x" + "0" * 24 + BUYER[2:]]}


def good(**over):
    r = {"transactionHash": TX, "status": "0x1", "to": C, "from": ME, "logs": [MINT_LOG]}
    r.update(over)
    return r


def test_encode_call_is_selector_plus_abi():
    data = encode_call("mint(address,string)", ["0x" + "ab" * 20, "ipfs://a"])
    assert data.startswith("0xd0def521") and len(data) > 10 + 64 * 3
    assert encode_call("set(uint256,bool)", ["7", "true"]) == encode_call("set(uint256,bool)",
                                                                           [7, True])
    for sig, args, needle in (("mint(address)", [], "takes 1"),
                              ("not a sig", [], "not a function signature"),
                              ("f(uint256)", ["abc"], "do not fit")):
        with pytest.raises(ValueError, match=needle):
            encode_call(sig, args)


def test_the_run_text_lists_the_pinned_functions_and_how_to_call():
    from types import SimpleNamespace
    from modules.commerce.fulfillment import _resources_text
    r = SimpleNamespace(kind="contract", chain="robinhood", address=C,
                        data={"functions": ["mint(address,string)"], "runtime_hash": "0x1"})
    text = _resources_text([r])
    assert "mint(address,string)" in text and 'function="<signature>"' in text
    assert "runtime_hash" not in text


@pytest.fixture
def order(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "db_path", lambda data_dir=None: str(tmp_path / "commerce.db"))
    o = st.save_draft("owner", MANIFEST)
    st.add_resource(o.id, "owner", "contract", {"runtime_hash": "0x" + "11" * 32,
                                                "functions": ["mint(address,string)"]},
                    chain="robinhood", address=C)
    st.go_live(o.id, "owner", actor=st.OWNER_SEAT, expect_hash=st.card_hash(st.get(o.id)))
    od = ords.create(offer_id=o.id, offer_version=1, user_id="owner", channel="a2a", buyer="b",
                     inputs={"delivery_address": "0x" + "ab" * 20}, amount_usd="5.00",
                     spend_budget=0.5)
    ords.attach_invoice(od.id, "inv_w5", "5.00")
    ords.mark_paid("inv_w5")
    ords.set_goal(od.id, ords.claim_fulfillment(od.id), "g1")
    from types import SimpleNamespace
    monkeypatch.setattr("core.wallet.factory.get_agent_wallet",
                        lambda: SimpleNamespace(address=ME))
    return ords.get(od.id)


def _rpc(receipt, chain_id=4663):
    def call(chain, method, params):
        if method == "eth_chainId":
            return hex(chain_id)
        return receipt
    return call


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt,chain_id,needle", [
    (None, 4663, "no receipt"),
    (good(status="0x0"), 4663, "FAILED"),
    (good(to="0x" + "dd" * 20), 4663, "not a call"),
    (good(transactionHash="0x" + "bb" * 32), 4663, "another transaction"),
    (good(), 1, "another chain"),
    (good(**{"from": "0x" + "99" * 20}), 4663, "not sent by this agent"),
    (good(logs=[]), 4663, "no event to the order's recipient"),
    (good(logs=[dict(MINT_LOG, address="0x" + "99" * 20)]), 4663, "no event"),
])
async def test_a_proof_that_the_chain_does_not_back_is_refused(order, monkeypatch, receipt,
                                                               chain_id, needle):
    monkeypatch.setattr(service, "_rpc_call", _rpc(receipt, chain_id))
    with pytest.raises(service.OrderRefused) as e:
        await service.deliver(order.id, {"tx_hash": TX}, user_id="owner")
    assert needle in str(e.value)
    assert ords.get(order.id).status == ords.FULFILLING


@pytest.mark.asyncio
async def test_an_unreadable_chain_refuses_and_a_good_receipt_delivers(order, monkeypatch):
    def boom(chain, method, params):
        raise OSError("rpc down")
    monkeypatch.setattr(service, "_rpc_call", boom)
    with pytest.raises(service.OrderRefused, match="again in a minute"):
        await service.deliver(order.id, {"tx_hash": TX}, user_id="owner")
    monkeypatch.setattr(service, "_rpc_call", _rpc(good(to=C.upper())))
    done = await service.deliver(order.id, {"tx_hash": TX}, user_id="owner")
    assert done.status in (ords.DELIVERED, ords.ACCEPTED)


@pytest.mark.asyncio
async def test_one_tx_delivers_one_order(order, monkeypatch):
    monkeypatch.setattr(service, "_rpc_call", _rpc(good()))
    await service.deliver(order.id, {"tx_hash": TX}, user_id="owner")
    two = ords.create(offer_id=order.offer_id, offer_version=1, user_id="owner", channel="a2a",
                      buyer="c", inputs={"delivery_address": BUYER}, amount_usd="5.00",
                      spend_budget=0.5)
    ords.attach_invoice(two.id, "inv_w5b", "5.00")
    ords.mark_paid("inv_w5b")
    ords.set_goal(two.id, ords.claim_fulfillment(two.id), "g2")
    with pytest.raises(service.OrderRefused, match="already delivered order"):
        await service.deliver(two.id, {"tx_hash": TX}, user_id="owner")


@pytest.mark.asyncio
async def test_a_thread_order_never_posts_the_runs_own_words(order, monkeypatch):
    from core.sqlite_util import execute_retry
    execute_retry(st.db_path(), "UPDATE offer_orders SET thread_ref='101', channel='x' "
                                "WHERE id=?", (order.id,))
    sent, told = [], []

    async def _tell(container, o, text, media=None):
        sent.append(text)
        return True

    async def _owner(container, uid, text):
        told.append(text)
    monkeypatch.setattr(service, "tell_buyer", _tell)
    monkeypatch.setattr(service, "owner_notice", _owner)
    monkeypatch.setattr(service, "_rpc_call", _rpc(good()))
    await service.deliver(order.id, {"tx_hash": TX}, user_id="owner",
                          message="visit evil.example now")
    assert "evil" not in sent[0] and "delivered" in sent[0]
    assert "evil" in told[0]
