"""122 W5 / 124 W5 — the ONE standing owner grant (the mandate): an exact scope, a reserved use,
an end; only an autonomous main-agent run uses it; an unreadable store is no mandate."""
import pytest

from core.wallet import standing_grant as G
from core.wallet.token_trust import owner_seat_ctx

COLL = "0x4444444444444444444444444444444444444444"
NOW = 1_000_000.0


@pytest.fixture
def store(tmp_path, monkeypatch):
    import os

    from core.wallet.signer import LocalEoaSigner
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "alice")
    signer = LocalEoaSigner(os.urandom(32))
    monkeypatch.setattr(G, "_signer", lambda: signer)
    return str(tmp_path / "wallet" / G.STORE_NAME)


def _make(store, **kw):
    base = dict(verbs=["agent_nft_collection_mint"], chains=["robinhood"], band_usd=(1, 50),
                count=2, total_usd=60, days=7, assets=[COLL], path=store, now=NOW)
    base.update(kw)
    return G.create(owner_seat_ctx("alice"), **base)


def _act(**kw):
    p = {"chain": "robinhood", "collection": COLL, "max_spend_usd": 30.0, "dry_run": False}
    p.update(kw)
    return p


def test_only_the_owner_makes_or_ends_a_mandate(store):
    with pytest.raises(G.MandateError):
        G.create(owner_seat_ctx("mallory"), verbs=["x"], chains=["base"], band_usd=(0, 1),
                 count=1, total_usd=1, days=1, path=store)
    m = _make(store)
    with pytest.raises(G.MandateError):
        G.end(owner_seat_ctx("mallory"), m.id, path=store)


@pytest.mark.parametrize("kw", [dict(verbs=[]), dict(chains=[]), dict(band_usd=(5, 1)),
                                dict(count=0), dict(total_usd=10), dict(days=31)])
def test_every_field_is_checked(store, kw):
    with pytest.raises(G.MandateError):
        _make(store, **kw)


def test_an_exact_act_is_covered_once_per_use_and_the_count_ends_it(store):
    _make(store)
    a = G.exemption("agent_nft_collection_mint", _act(), autonomous=True, path=store, now=NOW + 1)
    assert a and "1/2 acts" in a
    assert G.exemption("agent_nft_collection_mint", _act(max_spend_usd=20.0), autonomous=True,
                       path=store, now=NOW + 2)
    assert G.exemption("agent_nft_collection_mint", _act(max_spend_usd=1.0), autonomous=True,
                       path=store, now=NOW + 3) is None          # count used: the card returns


@pytest.mark.parametrize("action, params, why", [
    ("agent_nft_buy", _act(), "verb"),
    ("agent_nft_collection_mint", _act(chain="base"), "chain"),
    ("agent_nft_collection_mint", _act(max_spend_usd=51.0), "band"),
    ("agent_nft_collection_mint", _act(collection="0x" + "55" * 20), "asset"),
    ("agent_nft_collection_mint", _act(to="0x" + "66" * 20), "recipient"),
    ("agent_nft_collection_mint", _act(to=COLL), "an asset is not a recipient"),
    ("agent_nft_collection_mint", _act(token_out="0x" + "77" * 20), "every asset in scope"),
    ("agent_nft_collection_mint", _act(args=["0x" + "77" * 20]), "args never covered"),
])
def test_anything_outside_the_scope_keeps_the_card(store, action, params, why):
    _make(store)
    assert G.exemption(action, params, autonomous=True, path=store, now=NOW + 1) is None, why


def test_a_quote_an_owner_turn_and_a_non_autonomous_turn_use_nothing(store):
    _make(store)
    assert G.exemption("agent_nft_collection_mint", _act(dry_run=True), autonomous=True,
                       path=store, now=NOW + 1) is None
    # The hook sees only the params the caller set: an absent dry_run is the default, a quote.
    quote = {k: v for k, v in _act().items() if k != "dry_run"}
    assert G.exemption("agent_nft_collection_mint", quote, autonomous=True, path=store,
                       now=NOW + 1) is None
    assert G.exemption("agent_nft_collection_mint", _act(), autonomous=False, path=store,
                       now=NOW + 1) is None
    assert G.mandates(store)[0].used_count == 0


def test_the_end_and_the_expiry_stop_it(store):
    m = _make(store)
    assert G.exemption("agent_nft_collection_mint", _act(), autonomous=True, path=store,
                       now=NOW + 8 * 86_400) is None             # expired
    G.end(owner_seat_ctx("alice"), m.id, path=store, now=NOW + 1)
    assert G.exemption("agent_nft_collection_mint", _act(), autonomous=True, path=store,
                       now=NOW + 2) is None


def test_the_total_bounds_the_declared_sum(store):
    _make(store, count=10, total_usd=60)
    assert G.exemption("agent_nft_collection_mint", _act(max_spend_usd=40.0), autonomous=True,
                       path=store, now=NOW + 1)
    assert G.exemption("agent_nft_collection_mint", _act(max_spend_usd=30.0), autonomous=True,
                       path=store, now=NOW + 2) is None          # 40 + 30 > 60


def test_an_unreadable_store_is_no_mandate(store):
    import os
    os.makedirs(os.path.dirname(store), exist_ok=True)
    with open(store, "w") as fh:
        fh.write("{not json")
    assert G.exemption("agent_nft_collection_mint", _act(), autonomous=True, path=store,
                       now=NOW + 1) is None
    assert "unreadable" in G.status_lines(store)[0]


def test_a_changed_collection_drops_the_act_back_to_the_card(store, monkeypatch):
    from core.wallet import collection_registry
    _make(store, facts={COLL: {"runtime_sha256": "aa" * 32}})
    monkeypatch.setattr(collection_registry, "runtime_sha256_of", lambda rpc, a: "bb" * 32)
    assert G.exemption("agent_nft_collection_mint", _act(), autonomous=True, path=store,
                       now=NOW + 1, rpc_fn=lambda chain: object()) is None
    monkeypatch.setattr(collection_registry, "runtime_sha256_of", lambda rpc, a: "aa" * 32)
    assert G.exemption("agent_nft_collection_mint", _act(), autonomous=True, path=store,
                       now=NOW + 2, rpc_fn=lambda chain: object())


def test_the_turn_decides_who_may_use_it(store, monkeypatch):
    _make(store)
    monkeypatch.setattr(G, "store_path", lambda data_home=None: store)
    monkeypatch.setattr("core.security.owner_turn.is_owner_standing_run", lambda ctx: False)
    assert G.exemption_for_turn("agent_nft_collection_mint", _act(), object()) is None
    monkeypatch.setattr("core.security.owner_turn.is_owner_standing_run", lambda ctx: True)
    monkeypatch.setattr(G.time, "time", lambda: NOW + 1)
    assert G.exemption_for_turn("agent_nft_collection_mint", _act(), object())


def test_the_controller_stamps_the_owner_grant_for_the_guard(store, monkeypatch):
    import types

    from core.wallet.tx_guard import OWNER_GRANT_KEY
    from tools.controller import self_transfer
    _make(store)
    monkeypatch.setattr(G, "store_path", lambda data_home=None: store)
    monkeypatch.setattr(G.time, "time", lambda: NOW + 1)
    monkeypatch.setattr("core.security.owner_turn.is_owner_standing_run", lambda ctx: True)
    ctx = types.SimpleNamespace(metadata={})
    why = self_transfer.spend_exemption_with_turn("agent_nft_collection_mint", _act(), ctx)
    assert why and why.startswith("mandate ")
    mid = why.split()[1]
    # audit 2026-11-21 L-3: the grant names the mandate (the why rule, the act's source)
    assert ctx.metadata[OWNER_GRANT_KEY] == {"approved": True, "max_spend_usd": 30.0,
                                             "mandate": mid}


def test_nft_trust_autonomous_names_every_field():
    from tools.defi.owner_verbs.nft import _pull_grant
    toks = "0xabc autonomous mint,reveal band 1-50 count 3 total 100 days 7".split()
    grant = _pull_grant(toks)
    assert grant == {"acts": ["mint", "reveal"], "band": (1.0, 50.0), "count": 3,
                     "total": 100.0, "days": 7.0} and toks == ["0xabc"]
    assert "every field" in _pull_grant("0xabc autonomous mint band 1-50".split())
    assert "takes acts" in _pull_grant("0xabc autonomous steal band 1-2 count 1 total 2 days 1".split())
    assert _pull_grant(["0xabc"]) is None


def test_a_mandate_a_shell_wrote_or_reset_grants_nothing(store):
    """A record that does not verify against the wallet's key — forged, or its count reset —
    is no mandate."""
    import json
    _make(store, count=1)
    assert G.exemption("agent_nft_collection_mint", _act(), autonomous=True, path=store, now=NOW + 1)
    raw = json.load(open(store))
    raw["mandates"][0]["used_count"] = 0          # reset by hand
    json.dump(raw, open(store, "w"))
    assert G.exemption("agent_nft_collection_mint", _act(), autonomous=True, path=store,
                       now=NOW + 2) is None
    raw["mandates"][0].update(sig="", verbs=["agent_nft_buy"])
    json.dump(raw, open(store, "w"))
    assert G.exemption("agent_nft_buy", _act(), autonomous=True, path=store, now=NOW + 3) is None


def test_no_sale_act_is_standing():
    assert "sale" not in G.NFT_ACTS


def test_the_mandate_prefix_is_never_signed_for_a_site():
    from core.wallet.sign_policy import SignContext, SignRequest, classify
    v = classify(SignRequest(scheme="eip191", payload="polyrob owner mandate v1:" + "ab" * 32,
                             bound_origin="https://example.invalid", fetch_bound=True),
                 SignContext(holder="0x" + "11" * 20))
    assert not v.signable and "signing prefix" in v.reason


def test_a_reveal_that_names_no_fee_cap_is_bounded_by_the_operators_cap(store, monkeypatch):
    """The rig (CW2, 2026-11-20): ``agent_nft_collection_reveal`` declares no ``max_spend_usd``
    (its fee cap is AGENT_NFT_REVEAL_MAX_GAS_USD), so a reveal mandate never covered a reveal."""
    monkeypatch.setenv("AGENT_NFT_REVEAL_MAX_GAS_USD", "0.75")
    _make(store, verbs=["agent_nft_collection_reveal"], band_usd=(0.5, 1), total_usd=2)
    act = {"chain": "robinhood", "collection": COLL, "dry_run": False}
    a = G.exemption("agent_nft_collection_reveal", act, autonomous=True, path=store, now=NOW + 1)
    assert a and "1/2 acts" in a and "$0.75" in a
    monkeypatch.setenv("AGENT_NFT_REVEAL_MAX_GAS_USD", "5")      # above the band: the card
    assert G.exemption("agent_nft_collection_reveal", act, autonomous=True, path=store,
                       now=NOW + 2) is None


def test_only_the_owner_seat_makes_or_ends_a_mandate_never_an_owner_tenant_run(store):
    """Audit 2026-11-21 L-4: an autonomous owner-tenant run passes owner_turn_refusal; it is
    still not the owner seat (the income rule's check)."""
    from types import SimpleNamespace
    agent = SimpleNamespace(user_id="alice", role="orchestrator", is_sub_agent=False,
                            session_id="goal-1", metadata={"turn_id": "t"})
    with pytest.raises(G.MandateError, match="owner seat"):
        _make_ctx = dict(verbs=["agent_nft_collection_mint"], chains=["robinhood"],
                         band_usd=(1, 50), count=2, total_usd=60, days=7, assets=[COLL],
                         path=store, now=NOW)
        G.create(agent, **_make_ctx)
    m = _make(store)
    with pytest.raises(G.MandateError, match="owner seat"):
        G.end(agent, m.id, path=store)
    assert G.mandates(store)[0].ended_at is None


def test_a_duplicated_mandate_row_is_refused_never_multiplied(store):
    """Audit 2026-11-21 M-5: a signed row copied N times verifies N times; the store with a
    duplicate id is refused as a whole (no mandate, every act keeps its card)."""
    import json
    _make(store, count=1, total_usd=50)
    with open(store) as fh:
        raw = json.load(fh)
    raw["mandates"] = raw["mandates"] * 3
    with open(store, "w") as fh:
        json.dump(raw, fh)
    with pytest.raises(ValueError, match="duplicate"):
        G.mandates(store)
    assert G.exemption("agent_nft_collection_mint", _act(), autonomous=True, path=store,
                       now=NOW + 1) is None
    assert "unreadable" in G.status_lines(store, now=NOW + 1)[0]


def test_a_signer_that_refuses_names_the_remote_signer(store, monkeypatch):
    """Audit 2026-11-21 L-5: under WALLET_SIGNER=remote the owner reads why no mandate."""
    class Refuses:
        address = "0x" + "1" * 40

        def sign_message(self, data):
            raise RuntimeError("cannot sign a message here")
    monkeypatch.setattr(G, "_signer", lambda: Refuses())
    with pytest.raises(G.MandateError, match="WALLET_SIGNER=remote"):
        _make(store)


def test_a_guard_refusal_before_anything_signed_gives_the_use_back_once(store):
    """A goal run refused at the guard (the trading flag off, a cap) spent a use for nothing."""
    import types

    from core.wallet.tx_guard import OWNER_GRANT_KEY, Decision
    m = _make(store)
    assert G.exemption("agent_nft_collection_mint", _act(), autonomous=True, path=store,
                       now=NOW + 1)
    ctx = types.SimpleNamespace(metadata={OWNER_GRANT_KEY: {
        "approved": True, "max_spend_usd": 30.0, "mandate": m.id}})
    G.settle_guard(ctx, Decision(False, "refused: cap"), path=store)
    G.settle_guard(ctx, Decision(False, "refused: cap"), path=store)    # once, not twice
    row, = G.mandates(store)
    assert (row.used_count, row.used_usd) == (0, 0.0) and G._verified(row)


def test_a_refusal_after_an_allowed_leg_keeps_the_use(store):
    import types

    from core.wallet.tx_guard import OWNER_GRANT_KEY, Decision
    m = _make(store)
    assert G.exemption("agent_nft_collection_mint", _act(), autonomous=True, path=store,
                       now=NOW + 1)
    ctx = types.SimpleNamespace(metadata={OWNER_GRANT_KEY: {
        "approved": True, "max_spend_usd": 30.0, "mandate": m.id}})
    G.settle_guard(ctx, Decision(True, "ok"), path=store)        # the first leg was signed
    G.settle_guard(ctx, Decision(False, "refused"), path=store)
    assert G.mandates(store)[0].used_count == 1
    G.settle_guard(types.SimpleNamespace(metadata={}), Decision(False, "x"), path=store)
    assert G.mandates(store)[0].used_count == 1                   # no mandate named: nothing


def test_the_guard_calls_the_settle_after_a_live_decision_only(monkeypatch):
    from core.wallet import tx_guard
    assert G.settle_guard in tx_guard.AFTER_DECISION_HOOKS
    seen = []
    monkeypatch.setattr(tx_guard, "AFTER_DECISION_HOOKS", [lambda c, d: seen.append(d)])
    monkeypatch.setattr(tx_guard, "_authorize",
                        lambda *a, **k: tx_guard.Decision(False, "refused: x"))
    monkeypatch.setattr("core.act_reason.with_bound_why", lambda i: i)
    tx_guard.authorize(object(), {}, holder="0x1", gate=None, dry_run=True)
    assert not seen
    tx_guard.authorize(object(), {}, holder="0x1", gate=None)
    assert len(seen) == 1 and not seen[0].allowed
