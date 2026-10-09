"""Exercise real SDK wire/signature construction with a fake HTTP transport."""
from types import SimpleNamespace

import pytest

from core.wallet import submission_journal as journal
from polyrob_markets.order_submission import OrderOutcomeUnknown, submit_order


def hl_client(monkeypatch):
    sdk = pytest.importorskip("hyperliquid.exchange")
    from eth_account import Account
    client = sdk.Exchange.__new__(sdk.Exchange)
    client.info = SimpleNamespace(name_to_asset=lambda name: {"ETH": 1}[name])
    client.wallet = Account.from_key("01" * 32)
    client.vault_address = None
    client.expires_after = None
    client.base_url = "https://api.hyperliquid.xyz"
    return client


def pm_client(monkeypatch):
    sdk = pytest.importorskip("py_clob_client_v2.client")
    from py_clob_client_v2.clob_types import ApiCreds
    client = sdk.ClobClient("https://clob.invalid", 137, key="01" * 32,
                            creds=ApiCreds("test", "c2VjcmV0", "test"))
    monkeypatch.setattr(client, "get_tick_size", lambda token: "0.01")
    monkeypatch.setattr(client, "get_neg_risk", lambda token: False)
    monkeypatch.setattr(client, "get_version", lambda: 2)
    return client


def order_args(venue):
    if venue == "hyperliquid":
        return dict(name="ETH", is_buy=True, sz=0.01, limit_px=100,
                    order_type={"limit": {"tif": "Gtc"}})
    from py_clob_client_v2.clob_types import OrderArgs
    return {"order_args": OrderArgs(token_id="12345", price=0.5, size=2, side="BUY")}


@pytest.mark.parametrize("venue", ["hyperliquid", "polymarket"])
def test_reservation_exists_before_transport_and_client_is_unchanged(monkeypatch, venue):
    client = hl_client(monkeypatch) if venue == "hyperliquid" else pm_client(monkeypatch)
    def transport(*args, **kwargs):
        rows = journal.unresolved()
        assert len(rows) == 1 and rows[0]["chain"] == venue
        return {"accepted": True}
    name = "post" if venue == "hyperliquid" else "_post"
    monkeypatch.setattr(client, name, transport)
    result, ref = submit_order(venue, client, "owner", 10, **order_args(venue))
    assert result == {"accepted": True}
    assert ref == journal.unresolved()[0]["tx_hash"]
    assert getattr(client, name) is transport


@pytest.mark.parametrize("bad", ["coin", "size", "cloid", "hex_cloid"])
def test_bad_hl_input_never_creates_a_reservation(monkeypatch, bad):
    client = hl_client(monkeypatch)
    monkeypatch.setattr(client, "post", lambda *a, **k: pytest.fail("must not send"))
    order = order_args("hyperliquid")
    order.update({"coin": {"name": "UNKNOWN"}, "size": {"sz": 0.123456789},
                  "cloid": {"cloid": "bad"}, "hex_cloid": {"cloid": "0x" + "z" * 32}}[bad])
    with pytest.raises((KeyError, ValueError, TypeError)):
        submit_order("hyperliquid", client, "owner", 10, **order)
    assert journal.unresolved() == []


@pytest.mark.parametrize("bad", ["token", "price", "credentials"])
def test_bad_pm_input_never_creates_a_reservation(monkeypatch, bad):
    client = pm_client(monkeypatch)
    monkeypatch.setattr(client, "_post", lambda *a, **k: pytest.fail("must not send"))
    order = order_args("polymarket")
    if bad == "token":
        order["order_args"].token_id = "not-a-token"
    elif bad == "price":
        order["order_args"].price = 2
    else:
        client.creds = None
    with pytest.raises(Exception) as exc:
        submit_order("polymarket", client, "owner", 10, **order)
    assert not isinstance(exc.value, OrderOutcomeUnknown)
    assert journal.unresolved() == []


@pytest.mark.parametrize("venue", ["hyperliquid", "polymarket"])
def test_timeout_after_send_keeps_reservation_and_refuses_retry(monkeypatch, venue):
    client = hl_client(monkeypatch) if venue == "hyperliquid" else pm_client(monkeypatch)
    calls = []
    def transport(*args, **kwargs):
        calls.append(True)
        raise TimeoutError("lost response")
    monkeypatch.setattr(client, "post" if venue == "hyperliquid" else "_post", transport)
    with pytest.raises(OrderOutcomeUnknown, match="unknown"):
        submit_order(venue, client, "owner", 10, **order_args(venue))
    with pytest.raises(ValueError, match="unaccounted"):
        submit_order(venue, client, "owner", 10, **order_args(venue))
    assert len(calls) == len(journal.unresolved()) == 1


@pytest.mark.parametrize('venue,result', [
    ('hyperliquid', {'status': 'err', 'response': 'invalid order'}),
    ('hyperliquid', {'status': 'ok', 'response': {'type': 'order', 'data': {
        'statuses': [{'error': 'insufficient margin'}]}}}),
    ('polymarket', {'success': False, 'orderID': '', 'errorMsg': 'insufficient funds'}),
])
def test_explicit_rejection_releases_only_that_order_and_keeps_audit(monkeypatch, venue, result):
    from core.wallet.submission_store import connection
    client = hl_client(monkeypatch) if venue == 'hyperliquid' else pm_client(monkeypatch)
    monkeypatch.setattr(client, 'post' if venue == 'hyperliquid' else '_post', lambda *a, **k: result)
    _, ref = submit_order(venue, client, 'owner', 10, **order_args(venue))
    assert journal.unresolved() == []
    with connection(journal.journal_path()) as db:
        row = db.execute('SELECT * FROM submissions WHERE tx_hash=?', (ref,)).fetchone()
        assert row['state'] == 'rejected' and float(row['nonce']) == 10
    # New attempts are possible, but old rejected orders cannot be manually charged.
    with pytest.raises(ValueError):
        journal.operator_release(ref, lambda _: pytest.fail('must not book a rejected order'))
    _, next_ref = submit_order(venue, client, 'owner', 10, **order_args(venue))
    assert next_ref != ref and not journal.unresolved()


@pytest.mark.parametrize('venue,result', [
    ('hyperliquid', {'status': 'err'}),
    ('hyperliquid', {'status': 'ok', 'response': {'type': 'order', 'data': {'statuses': [
        {'error': 'one failed'}, {'filled': {'oid': 1}}]}}}),
    ('polymarket', {'success': False, 'orderID': 'accepted-id', 'errorMsg': 'contradictory'}),
    ('polymarket', {'errorMsg': 'missing status'}),
])
def test_ambiguous_replies_never_release_an_attempt(monkeypatch, venue, result):
    client = hl_client(monkeypatch) if venue == 'hyperliquid' else pm_client(monkeypatch)
    monkeypatch.setattr(client, 'post' if venue == 'hyperliquid' else '_post', lambda *a, **k: result)
    _, ref = submit_order(venue, client, 'owner', 10, **order_args(venue))
    assert journal.unresolved()[0]['tx_hash'] == ref


def test_rejection_cannot_release_another_venue_or_chain_attempt():
    ref = journal.prepare_attempt('x402', 'owner', 10)
    with pytest.raises(ValueError):
        journal.mark_rejected(ref, venue='x402')
    with pytest.raises(ValueError):
        journal.mark_rejected(ref, venue='hyperliquid')
    assert journal.unresolved()[0]['tx_hash'] == ref
