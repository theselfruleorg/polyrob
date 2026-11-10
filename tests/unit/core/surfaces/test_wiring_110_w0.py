"""110 W0: tenant leaks of the owner's addresses, the owner's stored credential, the
Telegram storefront replay, the A2A order billing and the denial vocabulary."""
import inspect
import re
from types import SimpleNamespace

import pytest

OWNER = "owner-principal"


@pytest.fixture
def owner_env(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: OWNER)
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "1000")
    monkeypatch.setenv("POLYROB_OWNER_EMAIL", "owner@example.com")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("OWNER_SURFACE", raising=False)


# -- D5: message(target="owner") ------------------------------------------------------

def test_owner_targets_are_the_owner_tenants_only(owner_env):
    from tools.controller.message_send import build_owner_targets
    mine = build_owner_targets(None, OWNER)
    assert mine.get("telegram") == "1000" and mine.get("email") == "owner@example.com"
    other = build_owner_targets(None, "u_tenant")
    assert "email" not in other and other.get("telegram") != "1000"


# -- D6: a cron email job ---------------------------------------------------------------

def test_cron_email_falls_back_to_the_owner_only_for_the_owner(owner_env):
    from cron.delivery import _owner_email
    agent = SimpleNamespace(container=None)
    assert _owner_email(agent, SimpleNamespace(id="j1", user_id=OWNER)) == "owner@example.com"
    assert _owner_email(agent, SimpleNamespace(id="j2", user_id="u_tenant")) is None


# -- D7: the owner's stored LLM credential --------------------------------------------

def test_llm_store_credential_tenant_gate(owner_env):
    from core.llm_auth.resolve import _tenant_allowed
    assert _tenant_allowed(None) is True
    assert _tenant_allowed(OWNER) is True
    assert _tenant_allowed("u_tenant") is False


# -- D8: a replayed Telegram /buy -------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_storefront_answers_a_replayed_update_once(tmp_path, monkeypatch):
    from surfaces.telegram import storefront_ops
    from surfaces.telegram.dedup import UpdateDedup
    calls, sent = [], []

    async def _reply(container, surface, buyer, text):
        calls.append((surface, buyer, text))
        return "Order o1: pay …"

    monkeypatch.setattr("modules.commerce.storefront.storefront_reply", _reply)

    class _Bot:
        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

    harness = SimpleNamespace(container=None, bot=_Bot(),
                              dedup=UpdateDedup(str(tmp_path / "tg_dedup.db"),
                                                window_seconds=3600))
    update = {"update_id": 77, "message": {"chat": {"id": 5, "type": "private"},
                                           "text": "/buy shirt size=M"}}
    assert await storefront_ops._storefront(harness, update) is True
    assert await storefront_ops._storefront(harness, update) is True   # the replay
    assert len(calls) == 1 and len(sent) == 1


# -- D9: an A2A offer order is not billed as compute ------------------------------------

class _Req:
    def __init__(self, path, body):
        self.url = SimpleNamespace(path=path)
        self.method = "POST"
        self._body = body

    async def body(self):
        import json
        return json.dumps(self._body).encode()


def _mw():
    from modules.x402.middleware import X402PaymentMiddleware
    mw = X402PaymentMiddleware.__new__(X402PaymentMiddleware)
    mw._is_x402_gated = lambda path, method="POST": True
    return mw


def _msg(parts, task_id=None):
    m = {"role": "user", "parts": parts}
    if task_id:
        m["taskId"] = task_id
    return m


ORDER = [{"kind": "data", "data": {"offer_id": "shirt", "inputs": {"size": "M"}}}]
TEXT = [{"kind": "text", "text": "hello"}]


@pytest.mark.asyncio
async def test_rpc_order_is_not_billed_as_compute():
    mw = _mw()
    rpc = lambda parts: {"method": "message/send", "params": {"message": _msg(parts)}}
    assert await mw._charges(_Req("/a2a/rpc", rpc(ORDER))) is False
    assert await mw._charges(_Req("/a2a/rpc", rpc(TEXT + ORDER))) is False
    assert await mw._charges(_Req("/a2a/rpc", rpc(TEXT))) is True


@pytest.mark.asyncio
async def test_stream_order_part_is_still_billed():
    # The stream route has no order path: it makes a compute task of the message.
    mw = _mw()
    assert await mw._charges(_Req("/a2a/message/stream", {"message": _msg(ORDER)})) is True


def test_the_rpc_answers_every_order_part_as_an_order():
    """The billing skip is safe only while the RPC routes EVERY message with an order
    part to ``a2a_order`` before any compute path, and ``a2a_order`` never answers
    None for one."""
    from api.a2a import endpoints
    from api import commerce_endpoints
    src = inspect.getsource(endpoints)
    assert src.index("a2a_order(params, user_id)") < src.index("handler.send_message(")
    body = inspect.getsource(commerce_endpoints.a2a_order)
    assert body.count("return None") == 1   # only for "no order part"


# -- D24: the denial vocabulary --------------------------------------------------------

def test_every_emitted_denial_reason_is_in_the_vocabulary():
    import core.surfaces.dispatcher as disp
    # Only DENIED decisions: an admin turn carries its own reason ("wait").
    emitted = set(re.findall(r'RouteKind\.DENIED[^()]*?reason="([a-z_]+)"',
                             inspect.getsource(disp), flags=re.S))
    assert {"other_bot_command", "local_owner_required"} <= emitted
    missing = emitted - set(disp.DENIAL_REASONS)
    assert not missing, f"denial slugs emitted but not in DENIAL_REASONS: {missing}"
