"""122 W3: ``defi_trade.launch_token`` — deploy, declare, grant, seed, in one act.

The sub-verbs (deploy, approve, lp_add) are faked: their own rules are in their own tests. Here:
the refusals before anything is sent, the order, that the declaration is the 117 row with
origin ``launch`` confirmed by THIS act's approval, that the token's launch price and the
Permit2 grants are bound only inside the call, and that a stop names what is left."""
import time
from types import SimpleNamespace

import pytest

from core.wallet import dex_registry, launch_policy as L, lp_seed as S
from core.wallet.tx_guard import OWNER_GRANT_KEY
from tools.defi import approve_verb, deploy_verb, launch_verb as LV, lp_v4_seed, lp_verbs
from tools.defi.trade_params import LaunchTokenParams

pytestmark = pytest.mark.filterwarnings("ignore")

ROW = dex_registry.row_for("robinhood", "v4")
Q = "0x" + "ab" * 20
NEW = "0x" + "11" * 20


class _Result:
    def __init__(self, content="", error=None):
        self.extracted_content, self.error = content, error


class _Gate:
    audit_log: list = []

    def _sync_shared_ledger(self):
        return True


class _Wallet:
    policy = _Gate()

    def operational_signer(self):
        return SimpleNamespace(address="0x" + "99" * 20)


class _Tool:
    _rail_factory = None

    def __init__(self):
        self._lp_rpc = lambda *a, **k: None

    def _ar(self, content=None, error=None):
        return _Result(content or "", error)

    def _get_wallet(self):
        return _Wallet()

    def _price(self, chain, addr):
        return 2.0          # the quote: $2


@pytest.fixture
def world(monkeypatch):
    calls = {"deploy": [], "approve": [], "lp_add": [], "prefs": {}}
    monkeypatch.setenv(deploy_verb.FLAG, "true")
    monkeypatch.setenv(lp_verbs.FLAG, "true")
    monkeypatch.setattr(LV, "settings", lambda: L.read_settings(calls["prefs"].get))
    monkeypatch.setattr(LV, "_owner_approved", lambda ctx, tool, usd: calls.get("approved", True))
    monkeypatch.setattr(lp_v4_seed, "_has_grant", lambda *a: False)
    monkeypatch.setattr(lp_v4_seed, "_uid", lambda ctx: "owner")
    # A launch's pool does not exist yet: the grants are sized at the anchor (L-10).
    monkeypatch.setattr(lp_v4_seed.V, "pool_state",
                        lambda *a, **k: SimpleNamespace(sqrt_price_x96=0))

    async def deploy(tool, p, ctx, *, launch=(), out=None):
        calls["deploy"].append((p, launch))
        if not p.dry_run:
            out["address"] = NEW
        return _Result("  RESULT: DEPLOYED AND CONFIRMED\n" if not p.dry_run else "  DRY RUN\n")
    monkeypatch.setattr(deploy_verb, "perform_deploy_token", deploy)

    def build_fields(ns, rpc, *, source):
        calls["fields"] = ns
        return {"chain": "robinhood", "currency0": NEW, "currency1": Q, "fee": 10000,
                "tick_spacing": 200, "hooks": "0x" + "0" * 40,
                "tick_lower": calls.get("ticks", (200, 887200))[0],
                "tick_upper": calls.get("ticks", (200, 887200))[1], "amount0_raw": 10 ** 24,
                "amount1_raw": int(ns.amount_b * 10 ** 18), "dec0": 18,
                "dec1": 18, "sym0": "NEW", "sym1": "Q", "sqrt_price_x96": 2 ** 96,
                "price_source": source, "gap_bps": 100}
    monkeypatch.setattr(lp_v4_seed, "build_fields", build_fields)

    async def approve(tool, p, ctx):
        # inside the launch: the new token has its launch price
        calls["approve"].append((p.token, p.spender, p.via, LV.bound_price(p.chain, NEW)))
        return _Result(calls.get("approve_text", "  RESULT: CONFIRMED\n"))
    monkeypatch.setattr(approve_verb, "perform_approve_token", approve)

    # The real lp_add runs (it makes the Permit2 grants); only its send is faked.
    async def send(tool, p, ctx, verb):
        row = S.get(p.seed, user_id="owner")
        calls["lp_add"].append((p, row["state"], row["origin"]))
        return _Result("declaration SEEDED")
    monkeypatch.setattr(lp_verbs, "_perform", send)
    monkeypatch.setattr(lp_v4_seed, "is_move", lambda p, ctx: False)
    return calls


def _p(**kw):
    return LaunchTokenParams(**({"chain": "robinhood", "name": "Demo", "symbol": "DEMO",
                                 "supply": "1000000000", "quote_token": Q,
                                 "token_amount": 1_000_000, "quote_amount": 0,
                                 "initial_price": 0.00001, "range": "ticks:0,887200",
                                 "max_spend_usd": 50.0, "dry_run": False} | kw))


@pytest.mark.asyncio
async def test_a_launch_runs_deploy_declaration_grants_and_seed_in_order(world):
    res = await LV.perform_launch_token(_Tool(), _p(), SimpleNamespace(metadata={}))
    assert not res.error, res.error
    (dp, mark), = world["deploy"]
    assert mark == ("deploy", Q) and not dp.dry_run and dp.max_spend_usd == 50.0
    ns = world["fields"]
    assert (ns.token_a, ns.token_b, ns.amount_a, ns.initial_price) == (NEW, Q, 1_000_000, 0.00001)
    # one ERC-20 leg (a one-sided seed): the allowance to Permit2, then the Permit2 grant
    assert [(t, s, v) for t, s, v, _p in world["approve"]] == [
        (NEW, ROW.permit2, "erc20"), (NEW, ROW.position_manager, "permit2")]
    # the token's price is the launch price (0.00001 quote x $2)
    assert all(px == pytest.approx(0.00002) for *_x, px in world["approve"])
    (lp, state, origin), = world["lp_add"]
    assert (lp.protocol, lp.dry_run, state, origin) == ("v4", False, S.CONFIRMED, "launch")
    assert "SEEDED" in res.extracted_content
    # nothing stays bound after the call
    assert LV.bound_price("robinhood", NEW) is None


@pytest.mark.asyncio
async def test_an_erc20_quote_leg_gets_its_grants_too(world):
    world["ticks"] = (-887200, 887200)   # the range holds the price: both legs are used
    await LV.perform_launch_token(_Tool(), _p(quote_amount=5.0), SimpleNamespace(metadata={}))
    assert [t for t, *_r in world["approve"]] == [NEW, NEW, Q, Q]


@pytest.mark.asyncio
async def test_a_quote_leg_the_mint_does_not_pull_gets_no_grant(world):
    """Audit 2026-11-21 L-10: above the price the range takes token0 only; a standing
    ERC-20 allowance on the quote would be a claim the mint never uses."""
    await LV.perform_launch_token(_Tool(), _p(quote_amount=5.0), SimpleNamespace(metadata={}))
    assert [t for t, *_r in world["approve"]] == [NEW, NEW]


@pytest.mark.asyncio
async def test_the_pin_refuses_another_pair_before_anything_is_sent(world):
    world["prefs"][L.PAIR_KEY] = f"robinhood:{'0x' + 'cd' * 20}"
    res = await LV.perform_launch_token(_Tool(), _p(), None)
    assert "launch.pair" in res.error and "Nothing was sent" in res.error
    assert not world["deploy"]


@pytest.mark.asyncio
async def test_the_day_limit_refuses_before_the_deploy(world, monkeypatch):
    monkeypatch.setattr(_Gate, "audit_log", [{"action": "deploy_token", "ts": time.time()}])
    res = await LV.perform_launch_token(_Tool(), _p(), None)
    assert "launch.per_day" in res.error and not world["deploy"]


@pytest.mark.asyncio
async def test_the_seed_limit_and_the_declared_bound(world):
    world["prefs"][L.POOL_KEY] = 10
    res = await LV.perform_launch_token(_Tool(), _p(), None)    # seed = 1e6 x $0.00002 = $20
    assert "launch.max_pool_usd" in res.error and not world["deploy"]
    world["prefs"].pop(L.POOL_KEY)
    res = await LV.perform_launch_token(_Tool(), _p(max_spend_usd=5.0), None)
    assert "above max_spend_usd" in res.error and not world["deploy"]


@pytest.mark.asyncio
async def test_a_live_launch_needs_the_owners_approval(world):
    world["approved"] = False
    res = await LV.perform_launch_token(_Tool(), _p(), SimpleNamespace(metadata={}))
    assert "the owner's act" in res.error and not world["deploy"]


@pytest.mark.asyncio
async def test_a_dry_run_deploys_nothing_and_declares_nothing(world):
    world["approved"] = False                    # a dry run needs no approval
    res = await LV.perform_launch_token(_Tool(), _p(dry_run=True), None)
    assert not res.error and "DRY RUN" in res.extracted_content
    assert world["deploy"][0][0].dry_run and not world["approve"] and not world["lp_add"]


@pytest.mark.asyncio
async def test_a_native_quote_is_held_to_lp_eth_cap(world, monkeypatch):
    monkeypatch.setenv("LP_ETH_CAP", "0")
    monkeypatch.setattr("core.wallet.onchain._rpc", lambda *a, **k: hex(10 ** 20))
    monkeypatch.setattr("core.wallet.onchain.rpc_url_for_chain", lambda c: "http://x")
    res = await LV.perform_launch_token(_Tool(), _p(quote_token=L.NATIVE, quote_amount=1.0),
                                        None)
    assert "LP_ETH_CAP" in res.error and not world["deploy"]


@pytest.mark.asyncio
async def test_a_stopped_grant_names_what_is_done_and_what_is_left(world):
    world["approve_text"] = "  RESULT: NOT SENT — nothing was broadcast."
    res = await LV.perform_launch_token(_Tool(), _p(), SimpleNamespace(metadata={}))
    text = res.error                      # an error: a card of it never reads done
    assert "STOPPED at the seed" in text and "erc20 grant" in text and NEW in text
    assert "seed='s" in text and "RESULT: NOT SENT" in text
    assert not world["lp_add"] and len(world["approve"]) == 1   # each grant made once


@pytest.mark.asyncio
async def test_a_declaration_that_raises_is_an_error_not_a_done_launch(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("rpc down")
    monkeypatch.setattr(lp_v4_seed, "build_fields", boom)
    res = await LV.perform_launch_token(_Tool(), _p(), SimpleNamespace(metadata={}))
    # An error result: the call card's runner reads the step as STOPPED, never done.
    assert res.error and "STOPPED at the pool declaration" in res.error and NEW in res.error
    assert not world["lp_add"] and not world["approve"]


def test_the_owner_grant_of_this_call_is_the_approval(monkeypatch):
    monkeypatch.setattr("core.money.authority.owner_direct_turn", lambda *a, **k: False)
    ctx = SimpleNamespace(metadata={OWNER_GRANT_KEY: {"approved": True, "max_spend_usd": 50.0}})
    assert LV._owner_approved(ctx, None, 50.0)
    assert not LV._owner_approved(ctx, None, 51.0)
    assert not LV._owner_approved(None, None, 1.0)


def test_the_card_shows_the_pin_the_limits_and_lp_eth_cap(monkeypatch):
    monkeypatch.setattr(LV, "settings", lambda: L.read_settings(
        {L.PAIR_KEY: f"base:{'0x' + 'cd' * 20}"}.get))
    monkeypatch.setattr("core.wallet.factory.get_agent_wallet", lambda: None)
    lines = "\n".join(LV.card_context({"chain": "robinhood", "quote_token": Q}))
    assert "LP_ETH_CAP" in lines and "launches: ? of 1 in 24 h" in lines
    assert "launch.pair: base:" in lines and "refused" in lines


def test_a_mandate_names_the_quote_token_as_its_asset():
    from core.wallet import standing_grant as G
    m = G.Mandate(id="m_1", created_at=0, ends_at=time.time() + 999, verbs=[L.ACTION],
                  chains=["robinhood"], assets=[Q], band_usd=(0.0, 50.0), count=1,
                  total_usd=50.0)
    assert G.covers(m, L.ACTION, {"chain": "robinhood", "quote_token": Q,
                                  "max_spend_usd": 50.0}, time.time()) is None
    assert G.covers(m, L.ACTION, {"chain": "robinhood", "quote_token": "0x" + "cd" * 20,
                                  "max_spend_usd": 50.0}, time.time())
    assert G.covers(m, L.ACTION, {"chain": "robinhood", "quote_token": Q,
                                  "max_spend_usd": 60.0}, time.time())


def test_the_owner_verb_shows_the_launch_facts_and_writes_one_mandate(monkeypatch):
    from tools.defi.owner_verbs import lp
    monkeypatch.setattr(LV, "settings", lambda: L.read_settings({}.get))
    monkeypatch.setattr("core.wallet.factory.get_agent_wallet", lambda: None)
    text = lp.launch_reply("owner", [])
    assert "launch.pair: not set" in text and "LP_ETH_CAP" in text
    made = {}

    def create(ctx, **kw):
        made.update(kw)
        return SimpleNamespace(id="m_x", line=lambda: "line")
    monkeypatch.setattr("core.wallet.standing_grant.create", create)
    words = ["mandate", Q, "seed", "40", "count", "2", "days", "7"]
    assert "Add `go`" in lp.launch_reply("owner", words) and not made
    assert "Mandate m_x" in lp.launch_reply("owner", words + ["go"])
    assert made["verbs"] == [L.ACTION] and made["assets"] == [Q]
    assert made["band_usd"] == (0.0, 40.0) and made["count"] == 2 and made["total_usd"] == 80.0
    with pytest.raises(ValueError):
        lp.launch_reply("owner", ["mandate", Q, "seed", "40"])


@pytest.mark.asyncio
async def test_the_deploy_of_a_launch_carries_the_launch_mark(monkeypatch):
    """The guard counts a launch at its deploy: ``perform_deploy_token(launch=…)`` puts the
    mark on the intent it hands the guard."""
    from tests.unit.tools.defi.test_deploy_and_call_verbs import _tool as deploy_tool
    from tools.defi.trade_params import DeployTokenParams
    monkeypatch.setenv(deploy_verb.FLAG, "true")
    captured = []
    tool, _ = deploy_tool(captured=captured)
    params = DeployTokenParams(chain="base", name="Demo", symbol="DEMO", supply=1_000,
                               max_spend_usd=5.0, dry_run=True)
    await deploy_verb.perform_deploy_token(tool, params, None, launch=("deploy", Q))
    await deploy_verb.perform_deploy_token(tool, params, None)
    assert captured[0].launch == ("deploy", Q) and captured[1].launch == ()
