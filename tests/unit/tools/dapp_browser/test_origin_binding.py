"""H03b (2026-09-23): the wallet answers ONE page on ONE origin.

The Playwright binding and the init script are context-wide, so before this any
later page — or a cross-origin iframe inside the dapp — could call
``eth_sendTransaction`` within the envelope. Now the bridge refuses any caller
that is not the armed tab's main frame on the armed origin, revokes on a
cross-origin navigation, uses a random binding name per arming, and refuses a
page transaction while the session is correspondent-tainted.
"""
import json
import types

import pytest

from core.wallet.tx_guard import Decision
from tools.dapp_browser import bridge as B
from tools.dapp_browser.js import BINDING, new_binding_name, provider_script

HOLDER = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"
POOL = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
ORIGIN = "https://app.example"


class _Gate:
    def __init__(self):
        import contextlib
        self.recorded = []

        @contextlib.asynccontextmanager
        async def _reserve():
            yield
        self.reserve = _reserve

    def record(self, **kw):
        self.recorded.append(kw)


class _Signer:
    address = HOLDER


class _Wallet:
    def __init__(self):
        self.policy = _Gate()

    def operational_signer(self):
        return _Signer()


class _Rail:
    def __init__(self, chain, signer, **kw):
        self.sent = False

    def build_call(self, *, to, data, value=0):
        return {"to": to, "data": data, "value": value}

    def size_gas(self, tx, sim_gas_used):
        return tx

    def sign_and_send(self, tx):
        self.sent = True
        return "0x" + "ee" * 32


class _Frame:
    def __init__(self, url):
        self.url = url


class _Page:
    def __init__(self, url=ORIGIN + "/swap"):
        self.main_frame = _Frame(url)
        self.handlers = {}

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)


def _src(page, frame=None):
    return {"page": page, "frame": frame or page.main_frame, "context": None}


def _bridge(*, armed=ORIGIN, taint_probe=None, lane="autonomous", approver=None):
    def _guard(intent, tx, **kw):
        return Decision(allowed=lane == "autonomous", reason="over ceiling", lane=lane,
                        amount_usd=2.0, sim_gas_used=None)
    env = B.Envelope(chain="base", max_spend_usd=50.0, session_budget_usd=100.0,
                     approval_timeout_sec=0.5)
    return B.WalletBridge(
        envelope=env, wallet=_Wallet(), execution_context=None,
        rail_factory=_Rail, guard_fn=_guard, price_fn=lambda c, a: 1.0,
        rpc_fn=lambda chain, method, params: "0x6000" if method == "eth_getCode" else "0x1", approver=approver,
        armed_origin=armed, taint_probe=taint_probe)


async def _ask(bridge, source, method="eth_blockNumber", params=None):
    return json.loads(await bridge.handle(source, json.dumps(
        {"method": method, "params": params or []})))


SEND = [{"to": POOL, "data": "0x12345678" + "00" * 32, "from": HOLDER}]


# --- origin + frame -------------------------------------------------------- #

def test_origin_of_normalises_like_location_origin():
    assert B.origin_of("https://App.Example:443/x?y") == "https://app.example"
    assert B.origin_of("http://app.example:8080/") == "http://app.example:8080"
    assert B.origin_of("https://bücher.de/") == "https://xn--bcher-kva.de"
    for bad in ("about:blank", "data:text/html,x", "https://u:p@app.example/", "", None):
        assert B.origin_of(bad) is None


@pytest.mark.asyncio
async def test_the_armed_main_frame_is_answered():
    out = await _ask(_bridge(), _src(_Page()))
    assert out == {"result": "0x1"}


@pytest.mark.asyncio
async def test_a_cross_origin_iframe_is_refused():
    page = _Page()
    out = await _ask(_bridge(), _src(page, _Frame("https://evil.example/")), "eth_sendTransaction", SEND)
    assert out["error"]["code"] == B.UNAUTHORIZED
    assert "embedded frame" in out["error"]["message"]


@pytest.mark.asyncio
async def test_a_same_origin_iframe_is_refused_too():
    page = _Page()
    out = await _ask(_bridge(), _src(page, _Frame(ORIGIN + "/embed")))
    assert "embedded frame" in out["error"]["message"]


@pytest.mark.asyncio
async def test_another_origin_in_the_main_frame_is_refused():
    out = await _ask(_bridge(), _src(_Page("https://evil.example/")), "eth_sendTransaction", SEND)
    assert "armed for https://app.example" in out["error"]["message"]


@pytest.mark.asyncio
async def test_no_source_or_no_armed_origin_is_refused():
    out = await _ask(_bridge(), None)
    assert out["error"]["code"] == B.UNAUTHORIZED
    out = await _ask(_bridge(armed=None), _src(_Page()))
    assert "not bound to any origin" in out["error"]["message"]


@pytest.mark.asyncio
async def test_another_tab_is_refused_once_the_page_is_attached():
    bridge = _bridge()
    armed_page = _Page()
    bridge.attach_page(armed_page)
    assert (await _ask(bridge, _src(armed_page))) == {"result": "0x1"}
    out = await _ask(bridge, _src(_Page()))
    assert "another tab" in out["error"]["message"]


@pytest.mark.asyncio
async def test_frame_refusals_do_not_flood_the_envelope():
    bridge = _bridge()
    for _ in range(50):
        await _ask(bridge, _src(_Page("https://evil.example/")))
    assert len(bridge.envelope.refused) == 1


# --- navigation revokes -------------------------------------------------- #

def test_navigating_the_main_frame_away_revokes_the_envelope():
    bridge = _bridge()
    page = _Page()
    bridge.attach_page(page)
    (handler,) = page.handlers["framenavigated"]
    handler(_Frame("https://evil.example/"))     # a sub-frame: ignored
    assert bridge.envelope.revoked is False
    page.main_frame.url = ORIGIN + "/pool"       # same origin: still armed
    handler(page.main_frame)
    assert bridge.envelope.revoked is False
    page.main_frame.url = "https://evil.example/"
    handler(page.main_frame)
    assert bridge.envelope.revoked is True
    assert bridge.envelope.refused[-1]["kind"] == "navigated-away"


# --- taint ---------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_tainted_session_cannot_sign_for_a_page_but_can_read():
    bridge = _bridge(taint_probe=lambda: True)
    out = await _ask(bridge, _src(_Page()), "eth_sendTransaction", SEND)
    assert "correspondent-tainted" in out["error"]["message"]
    assert bridge.envelope.sent == []
    assert (await _ask(bridge, _src(_Page()))) == {"result": "0x1"}


@pytest.mark.asyncio
async def test_a_failing_taint_probe_reads_as_tainted():
    def _boom():
        raise RuntimeError("probe down")
    out = await _ask(_bridge(taint_probe=_boom), _src(_Page()), "eth_sendTransaction", SEND)
    assert "correspondent-tainted" in out["error"]["message"]


@pytest.mark.asyncio
async def test_an_untainted_session_signs():
    bridge = _bridge(taint_probe=lambda: False)
    out = await _ask(bridge, _src(_Page()), "eth_sendTransaction", SEND)
    assert out["result"].startswith("0x")


def test_the_tool_probe_reads_the_orchestrator_flag(monkeypatch):
    from tools.dapp_browser.tool import DappBrowserTool
    import tools.ship_common as sc
    orch = types.SimpleNamespace(_correspondent_tainted=False)
    monkeypatch.setattr(sc, "resolve_orchestrator", lambda c, sid, resolver=None: orch)
    probe = DappBrowserTool(wallet=_Wallet())._taint_probe_for(
        types.SimpleNamespace(session_id="s1"))
    assert probe() is False
    orch._correspondent_tainted = True
    assert probe() is True


# --- the owner ask is keyed on the WHOLE call ----------------------------- #

class _Approver:
    def __init__(self):
        self.calls = []

    async def request(self, name, summary, ctx, *, hash_params=None):
        self.calls.append((summary, hash_params))
        return False


@pytest.mark.asyncio
async def test_the_grant_key_covers_full_calldata_and_value():
    approver = _Approver()
    bridge = _bridge(lane="owner_queue", approver=approver)
    page = _Page()
    a = "0x12345678" + "00" * 31 + "01"
    b = "0x12345678" + "00" * 31 + "02"
    await _ask(bridge, _src(page), "eth_sendTransaction", [{"to": POOL, "data": a}])
    await _ask(bridge, _src(page), "eth_sendTransaction", [{"to": POOL, "data": b}])
    await _ask(bridge, _src(page), "eth_sendTransaction",
               [{"to": POOL, "data": a, "value": "0x5"}])
    keys = [json.dumps(h, sort_keys=True) for _, h in approver.calls]
    assert len(set(keys)) == 3, "same selector, different call must not share a grant"
    summary, key = approver.calls[0]
    assert summary["args"] == ["00" * 31 + "01"]          # every word, untruncated
    assert key["calldata_sha256"] and key["value_wei"] == "0"
    assert approver.calls[2][1]["value_wei"] == "5"


def test_the_grant_card_shows_every_argument_word():
    from tools.controller.grant_card import render_grant_card
    words = ["ab" * 32, "cd" * 32]
    card = render_grant_card("dapp_browser_dapp_connect", {
        "to": POOL, "usd": 3.0, "selector": "0x12345678", "args": words,
        "calldata_sha256": "f" * 64, "value_wei": "0"}, "tap-1")
    for w in words:
        assert w in card
    assert "f" * 64 in card


# --- js: random binding, top-frame gate ----------------------------------- #

def test_each_arming_gets_a_fresh_binding_name():
    a, b = new_binding_name(), new_binding_name()
    assert a != b and a != BINDING
    script = provider_script(address=HOLDER, chain_id_hex="0x2105", binding=a,
                             origin=ORIGIN)
    assert f"window.{a}(" in script
    assert json.dumps(ORIGIN) in script and "window.top !== window" in script


def test_a_binding_name_that_is_not_an_identifier_is_refused():
    with pytest.raises(ValueError):
        provider_script(address=HOLDER, chain_id_hex="0x2105", binding="x);alert(1")


# --- the tool arms one origin on one tab ---------------------------------- #

class _RawContext:
    def __init__(self):
        self.bindings = []
        self.scripts = []

    async def expose_binding(self, name, fn):
        self.bindings.append(name)

    async def add_init_script(self, script):
        self.scripts.append(script)


class _BrowserContext:
    def __init__(self, landed):
        self.raw = _RawContext()
        self.page = _Page(landed)

    async def get_session(self):
        return types.SimpleNamespace(context=self.raw)

    async def navigate_to(self, url):
        return None

    async def get_current_page(self):
        return self.page


def _arm(monkeypatch):
    import core.money.authority as auth
    import core.wallet.chains as chains
    monkeypatch.setenv("DAPP_BROWSER_ENABLED", "true")
    # 067 P1b: the connect gate is the kernel's authorize_spend.
    monkeypatch.setattr(auth, "delegated_refusal", lambda ctx, verb: None)
    monkeypatch.setattr(auth, "turn_refusal", lambda ctx: None)
    monkeypatch.setattr(auth, "spend_pause_refusal", lambda **_k: None)
    monkeypatch.setattr(chains, "money_capable", lambda chain: (True, ""))


def _connect_params(url):
    from tools.dapp_browser.tool import ConnectParams
    return ConnectParams(url=url, chain="base", max_spend_usd=5, session_budget_usd=10)


@pytest.mark.asyncio
async def test_connect_arms_the_url_origin_with_a_random_binding(monkeypatch):
    from tools.dapp_browser.tool import DappBrowserTool
    _arm(monkeypatch)
    tool = DappBrowserTool(wallet=_Wallet())
    tool.container = types.SimpleNamespace()   # no task_agent → probe reads False
    tool._persist_bridge = lambda b: None
    bc = _BrowserContext(ORIGIN + "/swap")
    ctx = types.SimpleNamespace(session_id="s1", user_id="u1", browser_context=bc)
    res = await tool.dapp_connect(_connect_params(ORIGIN + "/swap"), execution_context=ctx)
    assert not res.error, res.error
    bridge = tool._bridges["s1"]
    assert bridge.armed_origin == ORIGIN
    (name,) = bc.raw.bindings
    assert name != BINDING and f"window.{name}(" in bc.raw.scripts[0]
    assert "framenavigated" in bc.page.handlers
    # A second arming gets a DIFFERENT binding, and the first envelope is revoked.
    await tool.dapp_connect(_connect_params(ORIGIN + "/swap"), execution_context=ctx)
    assert len(set(bc.raw.bindings)) == 2 and bridge.envelope.revoked


@pytest.mark.asyncio
async def test_connect_revokes_when_the_page_lands_on_another_origin(monkeypatch):
    from tools.dapp_browser.tool import DappBrowserTool
    _arm(monkeypatch)
    tool = DappBrowserTool(wallet=_Wallet())
    tool.container = types.SimpleNamespace()   # no task_agent → probe reads False
    tool._persist_bridge = lambda b: None
    bc = _BrowserContext("https://elsewhere.example/")
    ctx = types.SimpleNamespace(session_id="s2", user_id="u1", browser_context=bc)
    res = await tool.dapp_connect(_connect_params(ORIGIN), execution_context=ctx)
    assert res.error and "redirected" in res.error
    assert tool._bridges["s2"].envelope.revoked


@pytest.mark.asyncio
async def test_connect_refuses_a_non_web_url(monkeypatch):
    from tools.dapp_browser.tool import DappBrowserTool
    _arm(monkeypatch)
    tool = DappBrowserTool(wallet=_Wallet())
    bc = _BrowserContext(ORIGIN)
    ctx = types.SimpleNamespace(session_id="s3", user_id="u1", browser_context=bc)
    res = await tool.dapp_connect(_connect_params("javascript:alert(1)"), execution_context=ctx)
    assert res.error and "Nothing was connected" in res.error
    assert bc.raw.bindings == []
