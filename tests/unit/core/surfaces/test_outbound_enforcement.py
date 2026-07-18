"""013 T6 — outbound enforcement at the send gates: policy threading into the
tier gate, the open-tier daily-send cap, the seed-before-send rail (existing,
now exercised under "open" too), and the first-contact telemetry+owner-notice
report. Covers the plan's 8-case matrix (keyword-adjusted per the T6
corrections addendum, which OVERRIDES the plan text)."""
import asyncio
import os
import tempfile

from core.surfaces.conversations import ConversationStore
from core.surfaces.correspondents import CorrespondentRegistry
from core.surfaces.outbound_allowlist import OutboundAllowlist
from tools.controller.message_send import perform_message_send


class _Router:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, surface_id="telegram", media=None):
        self.sent.append((surface_id, chat_id, text))
        return True

    def capabilities(self, surface_id):
        return None


class _Container:
    def __init__(self, corr=None, convo=None):
        self._svc = {"correspondent_registry": corr, "conversation_store": convo}

    def get_service(self, name):
        return self._svc.get(name)


def _fixtures():
    tmp = tempfile.mkdtemp()
    corr = CorrespondentRegistry(os.path.join(tmp, "corr.db"))
    allowlist = OutboundAllowlist(os.path.join(tmp, "a.db"))
    convo = ConversationStore(os.path.join(tmp, "conversations.db"))
    return corr, allowlist, convo


def _enable_corr(monkeypatch, require_approval=False):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL",
                       "true" if require_approval else "false")


# --- case 1: open policy + unknown recipient -----------------------------------

def test_open_policy_unknown_recipient_sends_seeds_and_reports(monkeypatch):
    monkeypatch.setenv("OUTBOUND_POLICY", "open")
    _enable_corr(monkeypatch)
    corr, allowlist, convo = _fixtures()
    router = _Router()
    notified = []

    async def _fake_deliver(container, user_id, text, **kw):
        notified.append((user_id, text, kw.get("source")))
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _fake_deliver)

    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="new-guy", text="hi",
        session_id="s1", container=_Container(corr, convo)))

    assert res["success"] is True
    assert res["tier"] == "open"
    assert router.sent == [("telegram", "new-guy", "hi")]
    assert corr.resolve(surface="telegram", address="new-guy") is not None, \
        "correspondent must be seeded on a first-contact open send"
    assert convo.get("rob", "telegram", "new-guy") is not None, \
        "the outbound must be recorded in the conversation store"
    assert notified and notified[0][0] == "rob"
    assert notified[0][2] == "outbound_open_send"


# --- case 2: open policy + seed refused (new-recipient cap) blocks the send ----

def test_open_policy_seed_refused_blocks_send(monkeypatch):
    monkeypatch.setenv("OUTBOUND_POLICY", "open")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_MAX_NEW_PER_DAY", "0")
    corr, allowlist, convo = _fixtures()
    router = _Router()

    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={},
        user_id="rob", surface="telegram", target="new-guy", text="hi",
        session_id="s1", container=_Container(corr, convo)))

    assert res["success"] is False
    assert "cap" in res["error"].lower()
    assert router.sent == [], "a refused seed must never be orphaned by a send"
    assert convo.get("rob", "telegram", "new-guy") is None, \
        "a blocked send must not be recorded"


# --- case 3: open policy + daily send cap reached ------------------------------

def test_open_policy_daily_send_cap_blocks(monkeypatch):
    monkeypatch.setenv("OUTBOUND_POLICY", "open")
    monkeypatch.setenv("OUTBOUND_DAILY_SEND_CAP", "1")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    corr, allowlist, convo = _fixtures()
    convo.record_outbound("rob", "telegram", "someone-else", "prior send")
    router = _Router()

    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={},
        user_id="rob", surface="telegram", target="new-guy", text="hi",
        session_id="s1", container=_Container(corr, convo)))

    assert res["success"] is False
    assert "outbound.daily_send_cap" in res["error"]
    assert "1" in res["error"]
    assert router.sent == []


def test_open_policy_daily_send_cap_not_reached_still_sends(monkeypatch):
    """Regression companion to the cap-blocks test: below the cap, the open
    send still proceeds (the cap check must not false-positive)."""
    monkeypatch.setenv("OUTBOUND_POLICY", "open")
    monkeypatch.setenv("OUTBOUND_DAILY_SEND_CAP", "5")
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    corr, allowlist, convo = _fixtures()
    convo.record_outbound("rob", "telegram", "someone-else", "prior send")
    router = _Router()

    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={},
        user_id="rob", surface="telegram", target="new-guy", text="hi",
        session_id="s1", container=_Container(corr, convo)))

    assert res["success"] is True


# --- case 4: allowlist policy (supervised default) -> byte-identical denial ---

def test_allowlist_policy_denied_text_byte_identical(monkeypatch):
    monkeypatch.delenv("OUTBOUND_POLICY", raising=False)
    corr, allowlist, convo = _fixtures()
    router = _Router()

    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={},
        user_id="rob", surface="telegram", target="stranger", text="hi",
        session_id="s1", container=_Container(corr, convo)))

    assert res["success"] is False
    assert res["error"] == (
        "target not on owner allowlist; ask the owner to run "
        "`/allow telegram stranger` (or `polyrob owner allow telegram stranger`)")


# --- case 5: off policy -> owner only ------------------------------------------

def test_off_policy_owner_only(monkeypatch):
    monkeypatch.setenv("OUTBOUND_POLICY", "off")
    corr, allowlist, convo = _fixtures()
    allowlist.allow("rob", "telegram", "friend")
    router = _Router()

    denied = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="friend", text="hi",
        session_id="s1", container=_Container(corr, convo)))
    assert denied["success"] is False, "off policy denies even an allowlisted target"

    allowed = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="999", text="hi",
        session_id="s1", container=_Container(corr, convo)))
    assert allowed["success"] is True, "the owner is always reachable"


# --- case 6: autonomous turn + open policy -> the blanket refusal falls through --

def test_autonomous_turn_open_policy_falls_through(monkeypatch):
    from tools.controller.action_registration import _autonomous_message_refusal

    monkeypatch.setenv("OUTBOUND_POLICY", "open")
    monkeypatch.delenv("MESSAGE_AUTONOMOUS_ALLOWLISTED", raising=False)

    class _Ctx:
        is_sub_agent = True
        role = "leaf"
        metadata = {}
        session_id = "s1"
        user_id = "rob"

    class _Controller:
        _is_sub_agent = False
        session_id = "s1"

    assert _autonomous_message_refusal(_Ctx(), _Controller()) is None


# --- case 7: supervised mode + autonomous turn -> blanket refusal unchanged ----

def test_supervised_autonomous_turn_still_refused(monkeypatch):
    from tools.controller.action_registration import _autonomous_message_refusal

    monkeypatch.delenv("OUTBOUND_POLICY", raising=False)
    monkeypatch.delenv("MESSAGE_AUTONOMOUS_ALLOWLISTED", raising=False)

    class _Ctx:
        is_sub_agent = True
        role = "leaf"
        metadata = {}
        session_id = "s1"
        user_id = "rob"

    class _Controller:
        _is_sub_agent = False
        session_id = "s1"

    res = _autonomous_message_refusal(_Ctx(), _Controller())
    assert res is not None
    assert "not permitted" in res.extracted_content


# --- case 8: correspondent taint blocks BEFORE any policy logic ----------------

def test_correspondent_taint_blocks_message_before_policy(monkeypatch):
    from agents.task.agent.core.correspondent_gate import make_correspondent_gate_hook

    # Even a fully-open policy must never matter — the pre-tool-call hook fires
    # before perform_message_send (or the policy resolver) is ever reached.
    monkeypatch.setenv("OUTBOUND_POLICY", "open")
    hook = make_correspondent_gate_hook(get_tainted=lambda: True)

    result = hook("message", {"surface": "telegram", "target": "anyone"}, None)

    assert result is not None
    assert "blocked" in result.lower()


def test_correspondent_gate_allows_message_when_untainted(monkeypatch):
    """Sanity companion: the gate itself is scoped to taint, not to policy."""
    from agents.task.agent.core.correspondent_gate import make_correspondent_gate_hook

    monkeypatch.setenv("OUTBOUND_POLICY", "open")
    hook = make_correspondent_gate_hook(get_tainted=lambda: False)

    assert hook("message", {"surface": "telegram", "target": "anyone"}, None) is None


# --- case 9: supervised/allowlist + first-contact to allowlisted address --------
# (T6 fix: first-contact report must NOT fire for allowlisted sends)

def test_allowlist_policy_first_contact_allowlisted_no_open_report(monkeypatch):
    """First-contact send to an allowlisted address under supervised policy
    should succeed WITHOUT emitting outbound_open_send event or owner notice."""
    monkeypatch.delenv("OUTBOUND_POLICY", raising=False)  # supervised (default)
    _enable_corr(monkeypatch)
    corr, allowlist, convo = _fixtures()
    allowlist.allow("rob", "telegram", "friend")
    router = _Router()
    notified = []

    async def _fake_deliver(container, user_id, text, **kw):
        notified.append((user_id, text, kw.get("source")))
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _fake_deliver)

    res = asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="friend", text="hello",
        session_id="s1", container=_Container(corr, convo)))

    assert res["success"] is True
    assert res["tier"] == "allowlisted"
    assert router.sent == [("telegram", "friend", "hello")]
    assert convo.get("rob", "telegram", "friend") is not None, \
        "the outbound must be recorded in the conversation store"
    # KEY ASSERTIONS: no outbound_open_send event, no owner notice
    assert not any(n[2] == "outbound_open_send" for n in notified), \
        "outbound_open_send must NOT fire for supervised/allowlisted sends"
    assert notified == [], \
        "no owner delivery should occur for supervised/allowlisted sends"
